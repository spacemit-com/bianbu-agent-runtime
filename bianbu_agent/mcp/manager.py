"""Descriptor-bound package management and migration for standalone MCP servers."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from bianbu_agent._internal.locking import CapabilityFilesystemLock
from bianbu_agent.home import AgentHome
from bianbu_agent.mcp.config import validate_mcp_config_bytes


_MCP_ID = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)+")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)


@dataclass(frozen=True, slots=True)
class InstalledMCP:
    id: str
    root: Path
    content_digest: str



class MCPPackageManager:
    def __init__(self, home: AgentHome) -> None:
        self._home = home

    def install(
        self,
        source: str | Path,
        *,
        upgrade: bool = False,
    ) -> InstalledMCP:
        source_input = Path(source).expanduser()
        if source_input.is_symlink():
            raise ValueError("MCP package must be a regular directory")
        source_root = source_input.absolute()
        mcp_id = self._validate_id(source_root.name)
        self._home.prepare()
        self._home.ensure(self._home.mcps_dir)
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            mcps_fd = self._open_directory(self._home.mcps_dir, "MCP catalog")
            staging_fd = self._open_directory(self._home.staging_dir, "staging")
            root_bindings = (
                (self._home.mcps_dir, os.fstat(mcps_fd), "MCP catalog"),
                (self._home.staging_dir, os.fstat(staging_fd), "staging"),
            )
            try:
                self._require_path_bindings(root_bindings)
                self._reject_casefold_collision_at(
                    mcps_fd,
                    mcp_id,
                    allow_exact=upgrade,
                )
                payload = self._snapshot_path(source_root)
                validate_mcp_config_bytes(payload, workspace=self._home.workspace_dir)
                stage_name = (
                    f"retired-mcp-{mcp_id}-{uuid.uuid4().hex}"
                    if upgrade
                    else f"mcp-{mcp_id}-{uuid.uuid4().hex}"
                )
                os.mkdir(stage_name, 0o700, dir_fd=staging_fd)
                stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=staging_fd)
                stage_stat = os.fstat(stage_fd)
                committed = False
                destination_won_by_other = False
                try:
                    self._write_config(stage_fd, payload)
                    try:
                        self._require_path_bindings(root_bindings)
                        self._move_verified(
                            staging_fd,
                            stage_name,
                            mcps_fd,
                            mcp_id,
                            expected=stage_stat,
                            label="MCP install stage",
                        )
                        self._require_path_bindings(root_bindings)
                    except FileExistsError:
                        if not upgrade:
                            destination_won_by_other = True
                            raise
                        current_fd, _current_stat = self._open_package_at(
                            mcps_fd,
                            mcp_id,
                        )
                        try:
                            self._snapshot_open_package(
                                current_fd,
                                parent_fd=mcps_fd,
                                entry_name=mcp_id,
                                expected_root=_current_stat,
                            )
                            self._rename_exchange(
                                staging_fd,
                                stage_name,
                                mcps_fd,
                                mcp_id,
                            )
                        finally:
                            os.close(current_fd)
                    except BaseException:
                        self._quarantine_named_entry(
                            mcps_fd,
                            mcp_id,
                            staging_fd,
                            prefix=f"mcp-rejected-{mcp_id}",
                        )
                        raise
                    published = self._snapshot_open_package(
                        stage_fd,
                        parent_fd=mcps_fd,
                        entry_name=mcp_id,
                        expected_root=stage_stat,
                    )
                    if published != payload:
                        raise ValueError("published MCP stage changed after validation")
                    os.fsync(mcps_fd)
                    self._require_path_bindings(root_bindings)
                    if not self._entry_matches(mcps_fd, mcp_id, stage_stat):
                        raise ValueError("published MCP destination changed after commit")
                    committed = True
                finally:
                    if not committed:
                        if not destination_won_by_other:
                            if self._entry_matches(mcps_fd, mcp_id, stage_stat):
                                self._quarantine_named_entry(
                                    mcps_fd,
                                    mcp_id,
                                    staging_fd,
                                    prefix=f"mcp-rejected-{mcp_id}",
                                    expected=stage_stat,
                                )
                            elif self._entry_exists(mcps_fd, mcp_id):
                                self._quarantine_named_entry(
                                    mcps_fd,
                                    mcp_id,
                                    staging_fd,
                                    prefix=f"mcp-rejected-{mcp_id}",
                                )
                        self._quarantine_named_entry(
                            staging_fd,
                            stage_name,
                            staging_fd,
                            prefix=f"mcp-rejected-{mcp_id}",
                            expected=stage_stat,
                        )
                    os.close(stage_fd)
            finally:
                os.close(staging_fd)
                os.close(mcps_fd)
        return InstalledMCP(
            id=mcp_id,
            root=self._home.mcps_dir / mcp_id,
            content_digest=self._digest(payload),
        )

    def _read_roots(self) -> tuple[Path, ...]:
        from bianbu_agent.mcp.system import system_mcp_root

        return (system_mcp_root(), self._home.mcps_dir)

    def list(self) -> tuple[InstalledMCP, ...]:
        self._home.prepare()
        records: dict[str, InstalledMCP] = {}
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            for root in self._read_roots():
                if not root.exists():
                    continue
                mcps_fd = self._open_directory(root, "MCP catalog")
                try:
                    for name in sorted(os.listdir(mcps_fd)):
                        if name.startswith("."):
                            continue
                        entry = os.stat(name, dir_fd=mcps_fd, follow_symlinks=False)
                        if stat.S_ISLNK(entry.st_mode):
                            raise ValueError("installed MCP package is a symbolic link or unsafe")
                        if stat.S_ISDIR(entry.st_mode):
                            record = self._record_at(mcps_fd, name)
                            records[name] = InstalledMCP(record.id, root / name, record.content_digest)
                finally:
                    os.close(mcps_fd)
        return tuple(records[key] for key in sorted(records))

    def inspect(self, mcp_id: str) -> InstalledMCP | None:
        normalized_id = self._validate_id(mcp_id)
        self._home.prepare()
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            for root in reversed(self._read_roots()):
                if not root.exists():
                    continue
                mcps_fd = self._open_directory(root, "MCP catalog")
                try:
                    try:
                        record = self._record_at(mcps_fd, normalized_id)
                        return InstalledMCP(record.id, root / normalized_id, record.content_digest)
                    except FileNotFoundError:
                        continue
                finally:
                    os.close(mcps_fd)
        return None

    def verify(self, mcp_id: str) -> bool:
        try:
            return self.inspect(mcp_id) is not None
        except (OSError, ValueError):
            return False

    def assert_deletable(self, mcp_id: str) -> None:
        normalized_id = self._validate_id(mcp_id)
        if (self._read_roots()[0] / normalized_id).exists():
            raise ValueError("system MCP cannot be uninstalled")

    def uninstall(self, mcp_id: str) -> bool:
        self.assert_deletable(mcp_id)
        return self._quarantine_user_package(mcp_id)

    def discard_install(self, installed: InstalledMCP) -> bool:
        """Roll back the exact user declaration whose activation failed."""
        return self._quarantine_user_package(installed.id, expected_digest=installed.content_digest)

    def _quarantine_user_package(self, mcp_id: str, *, expected_digest: str | None = None) -> bool:
        normalized_id = self._validate_id(mcp_id)
        if not (self._home.mcps_dir / normalized_id).exists():
            return False
        self._home.prepare()
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            mcps_fd = self._open_directory(self._home.mcps_dir, "MCP catalog")
            staging_fd = self._open_directory(self._home.staging_dir, "staging")
            try:
                try:
                    root_fd, root_stat = self._open_package_at(mcps_fd, normalized_id)
                except FileNotFoundError:
                    return False
                try:
                    payload = self._snapshot_open_package(
                        root_fd,
                        parent_fd=mcps_fd,
                        entry_name=normalized_id,
                        expected_root=root_stat,
                    )
                    if expected_digest is not None and self._digest(payload) != expected_digest:
                        raise ValueError("MCP changed before installation rollback")
                    quarantine = (
                        f"retired-mcp-{normalized_id}-{uuid.uuid4().hex}"
                    )
                    self._rename_noreplace(
                        mcps_fd,
                        normalized_id,
                        staging_fd,
                        quarantine,
                    )
                    if not self._entry_matches(staging_fd, quarantine, root_stat):
                        raise ValueError("MCP package changed during uninstall")
                    if expected_digest is not None:
                        quarantined = self._snapshot_open_package(
                            root_fd, parent_fd=staging_fd, entry_name=quarantine,
                            expected_root=root_stat,
                        )
                        if self._digest(quarantined) != expected_digest:
                            self._rename_noreplace(staging_fd, quarantine, mcps_fd, normalized_id)
                            raise ValueError("MCP changed during installation rollback")
                    os.fsync(mcps_fd)
                    os.fsync(staging_fd)
                    os.fsync(staging_fd)
                finally:
                    os.close(root_fd)
            finally:
                os.close(staging_fd)
                os.close(mcps_fd)
        return True

    def _record_at(self, parent_fd: int, name: str) -> InstalledMCP:
        normalized_id = self._validate_id(name)
        root_fd, root_stat = self._open_package_at(parent_fd, normalized_id)
        try:
            payload = self._snapshot_open_package(
                root_fd,
                parent_fd=parent_fd,
                entry_name=normalized_id,
                expected_root=root_stat,
            )
            validate_mcp_config_bytes(payload, workspace=self._home.workspace_dir)
        finally:
            os.close(root_fd)
        return InstalledMCP(
            id=normalized_id,
            root=self._home.mcps_dir / normalized_id,
            content_digest=self._digest(payload),
        )

    def _snapshot_path(self, root: Path) -> bytes:
        try:
            initial = os.stat(root, follow_symlinks=False)
            root_fd = os.open(root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise ValueError("MCP package must be a regular directory or is unsafe") from exc
        try:
            opened = os.fstat(root_fd)
            if not stat.S_ISDIR(opened.st_mode) or not self._same_inode(initial, opened):
                raise ValueError("MCP package changed while it was being opened")
            return self._snapshot_open_package(
                root_fd,
                root_path=root,
                expected_root=opened,
            )
        finally:
            os.close(root_fd)

    def _snapshot_open_package(
        self,
        root_fd: int,
        *,
        expected_root: os.stat_result,
        parent_fd: int | None = None,
        entry_name: str | None = None,
        root_path: Path | None = None,
    ) -> bytes:
        if set(os.listdir(root_fd)) != {"mcp.json"}:
            raise ValueError("MCP packages may only contain mcp.json")
        try:
            initial = os.stat("mcp.json", dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("MCP mcp.json must be a regular file") from exc
        if not stat.S_ISREG(initial.st_mode):
            raise ValueError("MCP mcp.json must be a regular file")
        try:
            descriptor = os.open("mcp.json", _FILE_FLAGS, dir_fd=root_fd)
        except OSError as exc:
            raise ValueError("MCP mcp.json is a symbolic link or unsafe") from exc
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not self._same_inode(initial, before):
                raise ValueError("MCP mcp.json changed while it was being opened")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1024 * 1024):
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if self._mutable_signature(before) != self._mutable_signature(after):
                raise ValueError("MCP mcp.json changed while it was being read")
        finally:
            os.close(descriptor)
        try:
            final = os.stat("mcp.json", dir_fd=root_fd, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("MCP mcp.json changed during snapshot") from exc
        if not self._same_inode(initial, final):
            raise ValueError("MCP mcp.json changed during snapshot")
        if parent_fd is not None and entry_name is not None:
            if not self._entry_matches(parent_fd, entry_name, expected_root):
                raise ValueError("installed MCP package changed during snapshot")
        elif root_path is not None:
            try:
                final_root = os.stat(root_path, follow_symlinks=False)
            except OSError as exc:
                raise ValueError("MCP package changed during snapshot") from exc
            if not self._same_inode(expected_root, final_root):
                raise ValueError("MCP package changed during snapshot")
        return b"".join(chunks)

    @staticmethod
    def _write_config(directory_fd: int, payload: bytes) -> None:
        descriptor = os.open(
            "mcp.json",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("could not write MCP snapshot")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)

    @staticmethod
    def _digest(payload: bytes) -> str:
        digest = hashlib.sha256()
        relative = b"mcp.json"
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        return digest.hexdigest()

    @staticmethod
    def _open_directory(path: Path, label: str) -> int:
        try:
            return os.open(path, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise ValueError(f"{label} directory is unsafe") from exc

    @classmethod
    def _require_path_bindings(
        cls,
        bindings: tuple[tuple[Path, os.stat_result, str], ...],
    ) -> None:
        for path, expected, label in bindings:
            try:
                current = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"{label} root changed during operation") from exc
            if not cls._same_inode(expected, current):
                raise ValueError(f"{label} root changed during operation")

    @classmethod
    def _open_package_at(
        cls,
        parent_fd: int,
        name: str,
    ) -> tuple[int, os.stat_result]:
        initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(initial.st_mode):
            raise ValueError("installed MCP package is a symbolic link or unsafe")
        try:
            root_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("installed MCP package is a symbolic link or unsafe") from exc
        opened = os.fstat(root_fd)
        if not cls._same_inode(initial, opened):
            os.close(root_fd)
            raise ValueError("installed MCP package changed while it was being opened")
        return root_fd, opened

    @staticmethod
    def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
        return (
            first.st_dev,
            first.st_ino,
            stat.S_IFMT(first.st_mode),
        ) == (
            second.st_dev,
            second.st_ino,
            stat.S_IFMT(second.st_mode),
        )

    @staticmethod
    def _mutable_signature(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    @classmethod
    def _entry_matches(
        cls,
        parent_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> bool:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return cls._same_inode(expected, current)

    @staticmethod
    def _entry_exists(parent_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def _move_verified(
        self,
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
        *,
        expected: os.stat_result,
        label: str,
    ) -> None:
        if not self._entry_matches(old_directory_fd, old_name, expected):
            raise ValueError(f"{label} changed before move")
        self._rename_noreplace(
            old_directory_fd,
            old_name,
            new_directory_fd,
            new_name,
        )
        if not self._entry_matches(new_directory_fd, new_name, expected):
            raise ValueError(f"{label} changed during move")
        os.fsync(old_directory_fd)
        if old_directory_fd != new_directory_fd:
            os.fsync(new_directory_fd)

    def _quarantine_named_entry(
        self,
        source_fd: int,
        source_name: str,
        staging_fd: int,
        *,
        prefix: str,
        expected: os.stat_result | None = None,
    ) -> str | None:
        try:
            observed = os.stat(
                source_name,
                dir_fd=source_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if expected is not None and not self._same_inode(expected, observed):
            return None
        quarantine = f"{prefix}-{uuid.uuid4().hex}"
        self._rename_noreplace(
            source_fd,
            source_name,
            staging_fd,
            quarantine,
        )
        if not self._entry_matches(staging_fd, quarantine, observed):
            raise ValueError("MCP quarantine entry changed during move")
        os.fsync(source_fd)
        if source_fd != staging_fd:
            os.fsync(staging_fd)
        return quarantine

    @staticmethod
    def _rename_noreplace(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        if _RENAMEAT2 is None:
            raise OSError(errno.ENOSYS, "renameat2 with RENAME_NOREPLACE is required")
        result = _RENAMEAT2(
            old_directory_fd,
            os.fsencode(old_name),
            new_directory_fd,
            os.fsencode(new_name),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(f"MCP server already exists: {new_name}")
        raise OSError(error, os.strerror(error), new_name)

    @staticmethod
    def _rename_exchange(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        if _RENAMEAT2 is None:
            raise OSError(errno.ENOSYS, "renameat2 with RENAME_EXCHANGE is required")
        result = _RENAMEAT2(
            old_directory_fd,
            os.fsencode(old_name),
            new_directory_fd,
            os.fsencode(new_name),
            _RENAME_EXCHANGE,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), new_name)

    def _reject_casefold_collision_at(
        self,
        parent_fd: int,
        mcp_id: str,
        *,
        allow_exact: bool = False,
    ) -> None:
        folded = mcp_id.casefold()
        for child in os.listdir(parent_fd):
            if child.casefold() == folded:
                if allow_exact and child == mcp_id:
                    continue
                raise FileExistsError(f"MCP server already exists: {child}")

    @staticmethod
    def _validate_id(value: object) -> str:
        mcp_id = str(value).strip()
        if _MCP_ID.fullmatch(mcp_id) is None:
            raise ValueError("MCP ID must be a stable dotted lowercase identifier")
        return mcp_id

__all__ = [
    "InstalledMCP",
    "MCPPackageManager",
]
