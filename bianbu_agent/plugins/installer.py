"""Safe local-directory installation for standalone Tools, Skills, and Hooks."""

from __future__ import annotations

import ctypes
import errno
import logging
import os
from pathlib import Path
import secrets
import shutil
import stat
from collections.abc import Callable
from typing import Literal, TypeVar
from packaging.version import Version

from bianbu_agent._internal.filesystem import (
    UnsafeFilesystemEntry,
    digest_directory,
    regular_files,
)
from bianbu_agent._internal.locking import CapabilityFilesystemLock
from bianbu_agent.home import AgentHome
from bianbu_agent.hooks import DirectoryHookLoader
from bianbu_agent.plugins.ownership import (
    InstalledCapabilityRecord,
    installed_content_files,
    reject_reserved_ownership_metadata,
    write_ownership_record,
)
from bianbu_agent.plugins.manifest import (
    PLUGIN_MANIFEST_FILENAME,
    load_installed_plugin_manifest,
)
from bianbu_agent.plugins.models import (
    InstalledPlugin,
    PluginMember,
    PluginPackageIssue,
)
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.tools import DirectoryToolLoader
from bianbu_agent.mcp.config import validate_mcp_config


logger = logging.getLogger(__name__)

CapabilityKind = Literal["skill"]
T = TypeVar("T")
_MANIFESTS = {"skill": "SKILL.md"}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int


class CapabilityInstaller:
    """Install validated local capability directories into one prepared Home."""

    def __init__(self, home: AgentHome, *, file_only: bool = False) -> None:
        self._home = home
        self._file_only = file_only
        self._skill_loader = DirectorySkillLoader()

    def install_skill(
        self,
        source: Path,
        *,
        upgrade: bool = False,
    ) -> InstalledCapabilityRecord:
        return self._install("skill", source, upgrade=upgrade)

    def list(
        self, kind: CapabilityKind | str | None = None
    ) -> tuple[InstalledCapabilityRecord, ...]:
        kinds = (
            (self._normalize_kind(kind),)
            if kind is not None
            else tuple(sorted(_MANIFESTS))
        )
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            records = [
                record
                for selected_kind in kinds
                for target in self._targets_for(selected_kind)
                if (record := self._read_owned_target(
                    selected_kind, target, declared=True
                )) is not None
            ]
        return tuple(sorted(records, key=lambda record: (record.kind, record.capability_id)))

    def inspect(
        self, kind: CapabilityKind | str, capability_id: str
    ) -> InstalledCapabilityRecord | None:
        normalized_kind = self._normalize_kind(kind)
        target = self._target(normalized_kind, capability_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            return self._read_owned_target(
                normalized_kind, target, capability_id=target.name, declared=True
            )

    def verify(self, kind: CapabilityKind | str, capability_id: str) -> bool:
        normalized_kind = self._normalize_kind(kind)
        target = self._target(normalized_kind, capability_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            record = self._read_owned_target(
                normalized_kind, target, capability_id=target.name, declared=True
            )
            return record is not None and self._digest_matches(target, record)

    def uninstall(self, kind: CapabilityKind | str, capability_id: str) -> bool:
        normalized_kind = self._normalize_kind(kind)
        return self._uninstall(
            normalized_kind,
            capability_id,
            expected_origin="standalone",
            expected_owner_plugin_id=None,
            declared=True,
        )

    def _uninstall(
        self,
        kind: CapabilityKind,
        capability_id: str,
        *,
        expected_origin: Literal["standalone", "plugin"],
        expected_owner_plugin_id: str | None,
        expected_record: InstalledCapabilityRecord | None = None,
        declared: bool = False,
    ) -> bool:
        normalized_id = self._validate_id(capability_id)
        if not self._root_for(kind).is_dir():
            return False
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            home_fd = self._open_home_directory()
            try:
                self._prepare_home_at(home_fd, self._directory_name_for(kind))
                staging_fd = self._open_directory_at(home_fd, ".staging")
                kind_fd = self._open_directory_at(
                    home_fd, self._directory_name_for(kind)
                )
                try:
                    try:
                        target_fd = os.open(
                            normalized_id,
                            _DIRECTORY_FLAGS,
                            dir_fd=kind_fd,
                        )
                    except FileNotFoundError:
                        return False
                    except OSError as exc:
                        raise ValueError(
                            "installed capability target is not a regular directory"
                        ) from exc
                    try:
                        initial_record = self._verify_expected_opened_target(
                            kind,
                            normalized_id,
                            target_fd,
                            phase="installed",
                            expected_origin=expected_origin,
                            expected_owner_plugin_id=expected_owner_plugin_id,
                            declared=declared,
                        )
                        if initial_record is None:
                            return False
                        if expected_record is not None and initial_record != expected_record:
                            raise ValueError("installed capability does not match Plugin record")
                        initial_stat = os.fstat(target_fd)
                        tombstone_name = self._new_name(
                            staging_fd,
                            f"retired-{kind}-",
                        )
                        try:
                            self._rename_noreplace(
                                kind_fd,
                                normalized_id,
                                staging_fd,
                                tombstone_name,
                            )
                        except FileNotFoundError:
                            return False
                        if not self._entry_matches(
                            staging_fd,
                            tombstone_name,
                            initial_stat,
                        ):
                            recovery = self._restore_quarantine_if_safe(
                                staging_fd,
                                tombstone_name,
                                kind_fd,
                                normalized_id,
                                initial_stat,
                            )
                            raise ValueError(
                                "quarantine name changed after atomic move; "
                                f"{recovery}"
                            )
                        try:
                            quarantined_record = self._verify_expected_opened_target(
                                kind,
                                normalized_id,
                                target_fd,
                                phase="quarantined",
                                expected_origin=expected_origin,
                                expected_owner_plugin_id=expected_owner_plugin_id,
                                declared=declared,
                            )
                            if quarantined_record is None:
                                raise ValueError(
                                    "quarantined capability has no standalone ownership metadata"
                                )
                        except BaseException as exc:
                            recovery = self._restore_quarantine_if_safe(
                                staging_fd,
                                tombstone_name,
                                kind_fd,
                                normalized_id,
                                initial_stat,
                            )
                            raise ValueError(f"{exc}; {recovery}") from exc
                    finally:
                        os.close(target_fd)
                finally:
                    os.close(kind_fd)
                    os.close(staging_fd)
            finally:
                os.close(home_fd)
        return True

    def _install(
        self,
        kind: CapabilityKind,
        source: Path,
        *,
        origin: Literal["standalone", "plugin"] = "standalone",
        owner_plugin_id: str | None = None,
        upgrade: bool = False,
    ) -> InstalledCapabilityRecord:
        source_root = self._source_root(source)
        capability_id, version = self._validate(kind, source_root)
        # D-6：安装期代码签名门禁。tool 是可执行代码，skill 的 instructions
        # 进 prompt 也等于可执行内容，与 hook 同一道关。
        # 校验放在 _validate 之后（先确认结构合法）、拷贝之前（不落盘再回滚）。
        if not self._file_only:
            from bianbu_agent.security.security_codesign import enforce_code_signature

            enforce_code_signature(source_root, str(kind))
        source_digest = digest_directory(source_root)
        normalized_id = self._validate_id(capability_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            home_fd = self._open_home_directory()
            try:
                self._prepare_home_at(home_fd, self._directory_name_for(kind))
                staging_fd = self._open_directory_at(home_fd, ".staging")
                kind_fd = self._open_directory_at(
                    home_fd, self._directory_name_for(kind)
                )
                stage_name = self._mkdir_private(
                    staging_fd,
                    f"retired-{kind}-" if upgrade else f"{kind}-",
                )
                stage = self._fd_child_path(staging_fd, stage_name)
                try:
                    self._copy_source(source_root, stage)
                    if kind == "tool":
                        reject_reserved_ownership_metadata(stage)
                        if digest_directory(stage) != source_digest:
                            raise ValueError(
                                "capability source changed while it was being installed"
                            )
                        copied_id, copied_version = capability_id, version
                    else:
                        copied_id, copied_version = self._validate(kind, stage)
                    if (copied_id, copied_version) != (capability_id, version):
                        raise ValueError(
                            "capability source changed while it was being installed"
                        )
                    record = InstalledCapabilityRecord(
                        kind=kind,
                        capability_id=capability_id,
                        version=version,
                        content_digest=self._owned_digest(stage),
                        origin=origin,
                        owner_plugin_id=owner_plugin_id,
                    )
                    write_ownership_record(stage, record)
                    try:
                        self._rename_noreplace(
                            staging_fd, stage_name, kind_fd, normalized_id
                        )
                    except FileExistsError as exc:
                        if not upgrade:
                            raise FileExistsError(
                                f"capability already installed: {normalized_id}"
                            ) from exc
                        target_fd = os.open(
                            normalized_id,
                            _DIRECTORY_FLAGS,
                            dir_fd=kind_fd,
                        )
                        try:
                            existing = self._verify_expected_opened_target(
                                kind,
                                normalized_id,
                                target_fd,
                                phase="installed",
                                expected_origin=origin,
                                expected_owner_plugin_id=owner_plugin_id,
                            )
                            if existing is None:
                                raise ValueError(
                                    "existing capability is not owned by this installer"
                                )
                            self._rename_exchange(
                                staging_fd,
                                stage_name,
                                kind_fd,
                                normalized_id,
                            )
                        finally:
                            os.close(target_fd)
                except BaseException:
                    if stage.exists():
                        shutil.rmtree(stage)
                    raise
                finally:
                    os.close(kind_fd)
                    os.close(staging_fd)
            finally:
                os.close(home_fd)
        return record

    @staticmethod
    def _source_root(source: Path) -> Path:
        source_input = Path(source).expanduser()
        if source_input.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {source_input}")
        return source_input.resolve()

    def _validate(self, kind: CapabilityKind, source: Path) -> tuple[str, str]:
        reject_reserved_ownership_metadata(source)
        metadata, _ = self._skill_loader.inspect(source)
        return metadata["name"], metadata.get("version", "0.0.0")

    @staticmethod
    def _copy_source(source: Path, stage: Path) -> None:
        for path in regular_files(source):
            relative = path.relative_to(source)
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    def _target(self, kind: CapabilityKind, capability_id: str) -> Path:
        return self._root_for(kind) / self._validate_id(capability_id)

    @staticmethod
    def _validate_id(capability_id: str) -> str:
        if not isinstance(capability_id, str):
            raise ValueError("capability_id must be one safe directory component")
        normalized = capability_id.strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise ValueError("capability_id must be one safe directory component")
        return normalized

    @staticmethod
    def _normalize_kind(kind: CapabilityKind | str) -> CapabilityKind:
        if kind not in _MANIFESTS:
            raise ValueError(f"unsupported capability kind: {kind}")
        return kind  # type: ignore[return-value]

    @staticmethod
    def _directory_name_for(kind: CapabilityKind) -> str:
        return {"skill": "skills"}[kind]

    def _root_for(self, kind: CapabilityKind) -> Path:
        return self._home.root / self._directory_name_for(kind)

    def _open_home_directory(self) -> int:
        try:
            return os.open(self._home.root, _DIRECTORY_FLAGS)
        except OSError as exc:
            raise ValueError("Bianbu Agent Home root is not a safe directory") from exc

    def _prepare_home_at(self, home_fd: int, *extra_children: str) -> None:
        """Create and validate direct Home children while the lock is held.

        ``extra_children`` names extension roots the caller is about to write,
        so they are created on demand instead of at every Runtime startup.
        """

        relatives = [
            directory.relative_to(self._home.root)
            for directory in self._home.required_directories()[1:]
        ]
        relatives.extend(Path(name) for name in extra_children)
        for relative in relatives:
            if len(relative.parts) != 1:
                raise ValueError(f"unsupported nested Home directory: {relative}")
            name = relative.name
            try:
                os.mkdir(name, 0o700, dir_fd=home_fd)
            except FileExistsError:
                pass
            child_fd = self._open_directory_at(home_fd, name)
            os.close(child_fd)

    @staticmethod
    def _open_directory_at(parent_fd: int, name: str) -> int:
        try:
            return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError(f"Bianbu Agent Home directory is unsafe: {name}") from exc

    @staticmethod
    def _fd_child_path(parent_fd: int, name: str) -> Path:
        return Path(f"/proc/self/fd/{parent_fd}") / name

    @staticmethod
    def _mkdir_private(parent_fd: int, prefix: str) -> str:
        for _ in range(128):
            name = f"{prefix}{secrets.token_hex(16)}"
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            return name
        raise RuntimeError("could not allocate a private capability staging directory")

    @staticmethod
    def _new_name(parent_fd: int, prefix: str) -> str:
        for _ in range(128):
            name = f"{prefix}{secrets.token_hex(16)}"
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return name
        raise RuntimeError("could not allocate a capability quarantine name")

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
        if result != 0:
            error = ctypes.get_errno()
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

    def _verify_opened_target(
        self,
        kind: CapabilityKind,
        capability_id: str,
        directory_fd: int,
        *,
        phase: str,
        declared: bool = False,
    ) -> InstalledCapabilityRecord | None:
        target = Path(f"/proc/self/fd/{directory_fd}") / "."
        record = self._read_owned_target(
            kind,
            target,
            capability_id=capability_id,
            opened_directory=True,
            declared=declared,
        )
        if record is None:
            return None
        if not self._digest_matches(target, record):
            raise ValueError(
                f"{phase} capability digest does not match ownership record"
            )
        return record

    def _verify_expected_opened_target(
        self,
        kind: CapabilityKind,
        capability_id: str,
        directory_fd: int,
        *,
        phase: str,
        expected_origin: Literal["standalone", "plugin"],
        expected_owner_plugin_id: str | None,
        declared: bool = False,
    ) -> InstalledCapabilityRecord | None:
        if expected_origin == "standalone":
            # Keep the longstanding hookable verification boundary for standalone
            # uninstall tests and callers.
            return self._verify_opened_target(
                kind, capability_id, directory_fd, phase=phase, declared=declared
            )
        target = Path(f"/proc/self/fd/{directory_fd}") / "."
        record = self._read_owned_target(
            kind,
            target,
            capability_id=capability_id,
            opened_directory=True,
            expected_origin=expected_origin,
            expected_owner_plugin_id=expected_owner_plugin_id,
        )
        if record is None:
            return None
        if not self._digest_matches(target, record):
            raise ValueError(
                f"{phase} capability digest does not match ownership record"
            )
        return record

    def _restore_quarantine_if_safe(
        self,
        staging_fd: int,
        tombstone_name: str,
        kind_fd: int,
        capability_id: str,
        expected_stat: os.stat_result,
    ) -> str:
        if not self._entry_matches(staging_fd, tombstone_name, expected_stat):
            return (
                "quarantine name changed; preserved recovery path: "
                f".staging/{tombstone_name}; target path: {capability_id}"
            )
        try:
            self._rename_noreplace(
                staging_fd,
                tombstone_name,
                kind_fd,
                capability_id,
            )
        except FileExistsError:
            return (
                "target was recreated; preserved recovery path: "
                f".staging/{tombstone_name}; target path: {capability_id}"
            )
        except OSError as exc:
            return (
                "quarantine could not be restored; preserved recovery path: "
                f".staging/{tombstone_name}; target path: {capability_id}; cause: {exc}"
            )
        return f"quarantine safely restored to target path: {capability_id}"

    @staticmethod
    def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)

    def _entry_matches(
        self,
        directory_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> bool:
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return self._same_inode(current, expected)

    def _targets_for(self, kind: CapabilityKind) -> tuple[Path, ...]:
        root = self._root_for(kind)
        if not root.is_dir():
            return ()
        return tuple(
            path
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir()
        )

    def _read_owned_target(
        self,
        kind: CapabilityKind,
        target: Path,
        *,
        capability_id: str | None = None,
        opened_directory: bool = False,
        expected_origin: Literal["standalone", "plugin"] = "standalone",
        expected_owner_plugin_id: str | None = None,
        declared: bool = False,
    ) -> InstalledCapabilityRecord | None:
        if not target.exists():
            return None
        if not target.is_dir() or (target.is_symlink() and not opened_directory):
            raise ValueError("installed capability target is not a regular directory")
        record_path = target / ".bianbu-agent-sdk-ownership.json"
        if not record_path.exists():
            if declared and expected_origin == "standalone" and expected_owner_plugin_id is None:
                return self._declared_record(kind, target, capability_id=capability_id)
            return None
        if record_path.is_symlink() or not record_path.is_file():
            raise ValueError("installed capability ownership metadata is unsafe")
        record = InstalledCapabilityRecord.from_path(target)
        if (
            record.origin != expected_origin
            or record.owner_plugin_id != expected_owner_plugin_id
        ):
            if expected_origin == "standalone":
                raise ValueError("standalone installer rejects plugin-owned capability metadata")
            raise ValueError("installed capability ownership metadata does not match Plugin owner")
        expected_id = target.name if capability_id is None else capability_id
        if record.kind != kind or record.capability_id != expected_id:
            raise ValueError("installed capability ownership metadata does not match target")
        if not (target / _MANIFESTS[kind]).is_file():
            raise ValueError("installed capability manifest is missing")
        return record

    def _declared_record(
        self,
        kind: CapabilityKind,
        target: Path,
        *,
        capability_id: str | None = None,
    ) -> InstalledCapabilityRecord | None:
        """Derive a record for a capability the user declared on disk.

        The Runtime discovers capability directories from its own roots, so a
        directory that was dropped in by hand is a capability even though this
        SDK never installed it and wrote no ownership metadata.  Its identity,
        version and digest therefore come from the content itself, exactly like
        the packaged system resources.  ``None`` means "not a capability of this
        kind": a name that is missing, unreadable or malformed is skipped rather
        than breaking the whole listing, and metadata is never written back.
        """

        if not (target / _MANIFESTS[kind]).is_file():
            return None
        try:
            name, version = self._validate(kind, target)
            digest = self._owned_digest(target)
        except (OSError, ValueError):
            return None
        if capability_id is not None and name != capability_id:
            return None
        return InstalledCapabilityRecord(
            kind=kind,
            capability_id=name,
            version=version,
            content_digest=digest,
        )

    @staticmethod
    def _owned_digest(root: Path) -> str:
        files = installed_content_files(root)
        return digest_directory(root, files)

    def _digest_matches(self, target: Path, record: InstalledCapabilityRecord) -> bool:
        try:
            return self._owned_digest(target) == record.content_digest
        except ValueError:
            return False

    @staticmethod
    def _validate_owner_plugin_id(owner_plugin_id: str) -> str:
        normalized = str(owner_plugin_id).strip()
        if (
            not normalized
            or normalized in {".", ".."}
            or "/" in normalized
            or "\\" in normalized
            or "\x00" in normalized
        ):
            raise ValueError("owner_plugin_id must be one safe directory component")
        return normalized

    @classmethod
    def _validate_plugin_record(cls, record: InstalledCapabilityRecord) -> None:
        if record.origin != "plugin" or record.owner_plugin_id is None:
            raise ValueError("Plugin member must have plugin ownership metadata")
        cls._validate_owner_plugin_id(record.owner_plugin_id)


class PluginInstaller:
    """Install, inspect, verify, and quarantine self-contained Plugin packages."""

    def __init__(self, home: AgentHome, *, file_only: bool = False) -> None:
        self._home = home
        self._file_only = file_only
        self._capabilities = CapabilityInstaller(home)

    def install(self, source: Path, *, upgrade: bool = False) -> InstalledPlugin:
        source_input = Path(source).expanduser()
        if source_input.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {source_input}")
        source_root = source_input.resolve()
        source_package = self._validate_package(source_root)
        if not self._file_only:
            from bianbu_agent.security.security_codesign import enforce_code_signature

            enforce_code_signature(source_root, "plugin")
        return self._publish(source_root, source_package, upgrade=upgrade)

    def _publish(
        self,
        source_root: Path,
        source_package: InstalledPlugin,
        *,
        upgrade: bool,
    ) -> InstalledPlugin:
        lock = CapabilityFilesystemLock(self._home.root, exclusive=True)
        lock.__enter__()
        committed = False
        try:
            home_fd = self._capabilities._open_home_directory()
            try:
                self._capabilities._prepare_home_at(home_fd, "plugins")
                staging_fd = self._capabilities._open_directory_at(home_fd, ".staging")
                try:
                    name = self._capabilities._mkdir_private(staging_fd, "plugin-")
                    stage = self._capabilities._fd_child_path(staging_fd, name)
                    try:
                        self._copy_plugin(source_root, stage)
                        staged = self._validate_package(stage, expected_id=source_package.id)
                        if staged.content_digest != source_package.content_digest:
                            raise ValueError(
                                "Plugin source changed while it was being installed"
                            )
                        plugins_fd = self._capabilities._open_directory_at(home_fd, "plugins")
                        try:
                            try:
                                current = self._read_plugin_at(plugins_fd, staged.id)
                            except FileNotFoundError:
                                current = None
                            if current is None:
                                self._capabilities._rename_noreplace(
                                    staging_fd, name, plugins_fd, staged.id
                                )
                                committed = True
                            else:
                                if not upgrade:
                                    raise FileExistsError(
                                        f"plugin already installed: {staged.id}; use upgrade"
                                    )
                                if Version(staged.manifest.version) <= Version(
                                    current.manifest.version
                                ):
                                    raise ValueError(
                                        "Plugin upgrade version must be newer than the installed version"
                                    )
                                rollback = self._capabilities._new_name(
                                    staging_fd,
                                    f"plugin-rollback-{staged.id}-{current.manifest.version}-",
                                )
                                exchanged = False
                                try:
                                    self._capabilities._rename_exchange(
                                        staging_fd, name, plugins_fd, staged.id
                                    )
                                    exchanged = True
                                    self._capabilities._rename_noreplace(
                                        staging_fd, name, staging_fd, rollback
                                    )
                                except BaseException:
                                    if exchanged:
                                        try:
                                            self._capabilities._rename_exchange(
                                                staging_fd, name, plugins_fd, staged.id
                                            )
                                        except BaseException:
                                            pass
                                    raise
                                committed = True
                        finally:
                            self._close_install_fd(plugins_fd, committed)
                        return staged
                    except BaseException:
                        if stage.exists():
                            shutil.rmtree(stage)
                        raise
                finally:
                    self._close_install_fd(staging_fd, committed)
            finally:
                self._close_install_fd(home_fd, committed)
        finally:
            try:
                lock.__exit__(None, None, None)
            except OSError:
                if not committed:
                    raise

    @staticmethod
    def _close_install_fd(descriptor: int, committed: bool) -> None:
        try:
            os.close(descriptor)
        except OSError:
            if not committed:
                raise

    def _read_roots(self) -> tuple[Path, ...]:
        from bianbu_agent.system_resources import system_resource_dir

        return (system_resource_dir("plugins"), self._home.plugins_dir)

    def package_root(self, plugin_id: str) -> Path:
        normalized_id = CapabilityInstaller._validate_id(plugin_id)
        for root in reversed(self._read_roots()):
            target = root / normalized_id
            if target.exists() or target.is_symlink():
                return target
        return self._home.plugins_dir / normalized_id

    def assert_deletable(self, plugin_id: str) -> None:
        normalized_id = CapabilityInstaller._validate_id(plugin_id)
        if (self._read_roots()[0] / normalized_id).exists():
            raise ValueError("system Plugin cannot be uninstalled")

    def scan(self) -> tuple[tuple[InstalledPlugin, ...], tuple[PluginPackageIssue, ...]]:
        """Read every installed Plugin and report the unreadable directories.

        One unreadable package must not hide the rest of the catalog: the
        Runtime keeps serving system Plugins and reports the skipped directory
        with its reason instead of failing the whole listing.
        """

        plugins: dict[str, InstalledPlugin] = {}
        issues: list[PluginPackageIssue] = []
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            for root in self._read_roots():
                if not root.is_dir():
                    continue
                if root.is_symlink():
                    raise ValueError("Plugin catalog is unsafe: symbolic link")
                plugins_fd = os.open(root, _DIRECTORY_FLAGS)
                try:
                    for plugin_id in sorted(os.listdir(plugins_fd)):
                        if not self._plugin_directory_entry(plugins_fd, plugin_id):
                            continue
                        try:
                            plugins[plugin_id] = self._read_plugin_at(plugins_fd, plugin_id)
                        except UnsafeFilesystemEntry:
                            # A redirectable entry is an attack, not a bad file.
                            raise
                        except (OSError, ValueError) as exc:
                            logger.warning(
                                "Skipping unreadable Plugin %s in %s: %s",
                                plugin_id,
                                root,
                                exc,
                            )
                            issues.append(
                                PluginPackageIssue(
                                    path=root / plugin_id,
                                    reason=str(exc) or type(exc).__name__,
                                )
                            )
                finally:
                    os.close(plugins_fd)
        return tuple(plugins[key] for key in sorted(plugins)), tuple(issues)

    def list(self) -> tuple[InstalledPlugin, ...]:
        return self.scan()[0]

    def inspect(self, plugin_id: str) -> InstalledPlugin | None:
        target = self.package_root(plugin_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            try:
                plugins_fd = os.open(target.parent, _DIRECTORY_FLAGS)
            except FileNotFoundError:
                return None
            try:
                try:
                    return self._read_plugin_at(plugins_fd, target.name)
                except FileNotFoundError:
                    return None
            finally:
                os.close(plugins_fd)

    def verify(self, plugin_id: str) -> bool:
        try:
            return self.inspect(plugin_id) is not None
        except ValueError:
            return False

    def uninstall(self, plugin_id: str) -> bool:
        normalized_id = CapabilityInstaller._validate_id(plugin_id)
        self.assert_deletable(normalized_id)
        try:
            return self._quarantine_user_package(normalized_id)
        except ValueError as exc:
            # A package written by an older generation can no longer be read,
            # and the quarantine step verifies what it moves.  Say what the
            # operator has to do instead of only naming the parse error.
            raise ValueError(
                f"installed Plugin {normalized_id} cannot be read ({exc}); "
                f"delete {self._home.plugins_dir / normalized_id} by hand to drop it"
            ) from exc

    def discard_install(self, installed: InstalledPlugin) -> bool:
        """Roll back a newly installed user package without deleting its system base."""
        return self._quarantine_user_package(
            CapabilityInstaller._validate_id(installed.id),
            expected_digest=installed.content_digest,
        )

    def _quarantine_user_package(
        self, normalized_id: str, *, expected_digest: str | None = None,
    ) -> bool:
        if not (self._home.plugins_dir / normalized_id).exists():
            return False
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            home_fd = self._capabilities._open_home_directory()
            try:
                self._capabilities._prepare_home_at(home_fd, "plugins")
                staging_fd = self._capabilities._open_directory_at(home_fd, ".staging")
                plugins_fd = self._capabilities._open_directory_at(home_fd, "plugins")
                try:
                    try:
                        plugin_fd = os.open(normalized_id, _DIRECTORY_FLAGS, dir_fd=plugins_fd)
                    except FileNotFoundError:
                        return False
                    except OSError as exc:
                        raise ValueError("installed Plugin target is not a regular directory") from exc
                    try:
                        current = self._verify_opened_plugin(plugin_fd, normalized_id, phase="installed")
                        if expected_digest is not None and current.content_digest != expected_digest:
                            raise ValueError("Plugin changed before installation rollback")
                        initial_stat = os.fstat(plugin_fd)
                        tombstone = self._capabilities._new_name(staging_fd, "plugin-uninstall-")
                        self._capabilities._rename_noreplace(plugins_fd, normalized_id, staging_fd, tombstone)
                        if not self._capabilities._entry_matches(staging_fd, tombstone, initial_stat):
                            recovery = self._restore_if_safe(
                                staging_fd, tombstone, plugins_fd, normalized_id, initial_stat
                            )
                            raise ValueError(f"plugin quarantine name changed after atomic move; {recovery}")
                        try:
                            quarantined = self._verify_opened_plugin(plugin_fd, normalized_id, phase="quarantined")
                            if expected_digest is not None and quarantined.content_digest != expected_digest:
                                raise ValueError("Plugin changed during installation rollback")
                        except BaseException as exc:
                            recovery = self._restore_if_safe(
                                staging_fd, tombstone, plugins_fd, normalized_id, initial_stat
                            )
                            raise ValueError(f"{exc}; {recovery}") from exc
                    finally:
                        os.close(plugin_fd)
                finally:
                    os.close(plugins_fd)
                    os.close(staging_fd)
            finally:
                os.close(home_fd)
        return True

    def rollback(self, plugin_id: str) -> InstalledPlugin:
        normalized_id = CapabilityInstaller._validate_id(plugin_id)
        prefix = f"plugin-rollback-{normalized_id}-"
        if not self._home.plugins_dir.is_dir():
            raise FileNotFoundError(
                f"no rollback version is available for Plugin {normalized_id}"
            )
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            home_fd = self._capabilities._open_home_directory()
            try:
                self._capabilities._prepare_home_at(home_fd, "plugins")
                staging_fd = self._capabilities._open_directory_at(home_fd, ".staging")
                plugins_fd = self._capabilities._open_directory_at(home_fd, "plugins")
                try:
                    candidates = []
                    for name in os.listdir(staging_fd):
                        if not name.startswith(prefix):
                            continue
                        stat_result = os.stat(
                            name, dir_fd=staging_fd, follow_symlinks=False
                        )
                        if stat.S_ISDIR(stat_result.st_mode):
                            candidates.append((stat_result.st_mtime_ns, name))
                    if not candidates:
                        raise FileNotFoundError(
                            f"no rollback version is available for Plugin {normalized_id}"
                        )
                    _, selected = max(candidates)
                    selected_fd = os.open(
                        selected, _DIRECTORY_FLAGS, dir_fd=staging_fd
                    )
                    try:
                        restored = self._verify_opened_plugin(
                            selected_fd, normalized_id, phase="rollback"
                        )
                    finally:
                        os.close(selected_fd)
                    current = self._read_plugin_at(plugins_fd, normalized_id)
                    current_backup = self._capabilities._new_name(
                        staging_fd,
                        f"plugin-rollback-{normalized_id}-{current.manifest.version}-",
                    )
                    self._capabilities._rename_noreplace(
                        plugins_fd, normalized_id, staging_fd, current_backup
                    )
                    try:
                        self._capabilities._rename_noreplace(
                            staging_fd, selected, plugins_fd, normalized_id
                        )
                    except BaseException:
                        self._capabilities._rename_noreplace(
                            staging_fd, current_backup, plugins_fd, normalized_id
                        )
                        raise
                    return restored
                finally:
                    os.close(plugins_fd)
                    os.close(staging_fd)
            finally:
                os.close(home_fd)

    def _target(self, plugin_id: str) -> Path:
        return self._home.plugins_dir / CapabilityInstaller._validate_id(plugin_id)

    @staticmethod
    def _copy_plugin(source: Path, stage: Path) -> None:
        for directory in sorted(
            (
                path
                for path in source.rglob("*")
                if path.is_dir()
            ),
            key=lambda item: item.as_posix(),
        ):
            (stage / directory.relative_to(source)).mkdir(parents=True, exist_ok=True)
        for path in regular_files(source):
            destination = stage / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)

    def _validate_package(self, root: Path, *, expected_id: str | None = None) -> InstalledPlugin:
        files = regular_files(root)
        reject_reserved_ownership_metadata(root)
        manifest = load_installed_plugin_manifest(root)
        plugin_id = manifest.id
        if expected_id is not None and plugin_id != expected_id:
            raise ValueError(
                f"Plugin manifest ID {plugin_id!r} does not match installed ID {expected_id!r}"
            )
        allowed_directories = {"tools", "skills", "hooks", "lib", "scripts", "mcps"}
        allowed_files = {PLUGIN_MANIFEST_FILENAME, "config.yaml"}
        if manifest.entrypoint:
            entrypoint_file = manifest.entrypoint.partition(":")[0]
            allowed_files.add(entrypoint_file)
            entrypoint_path = root / entrypoint_file
            if entrypoint_path.is_symlink() or not entrypoint_path.is_file():
                raise ValueError(
                    f"Plugin entrypoint file is missing or unsafe: {entrypoint_file}"
                )
        if (root / "mcp.json").exists():
            raise ValueError(
                "standalone MCP packages must be installed through the MCP catalog; "
                "Plugin MCP servers belong under mcps/<name>/mcp.json"
            )
        for path in root.iterdir():
            if path.name in allowed_files:
                if path.is_symlink() or not path.is_file():
                    raise UnsafeFilesystemEntry(
                        f"Plugin {path.name} must be a regular file"
                    )
                continue
            if path.name not in allowed_directories:
                raise ValueError(f"unsupported Plugin package entry: {path.name}")
            if path.is_symlink() or not path.is_dir():
                raise UnsafeFilesystemEntry(
                    f"Plugin {path.name} must be a regular directory"
                )
        members: list[PluginMember] = []
        local_names: set[str] = set()
        for kind, directory_name in (
            ("tool", "tools"),
            ("skill", "skills"),
            ("hook", "hooks"),
            ("mcp", "mcps"),
        ):
            directory = root / directory_name
            if not directory.exists():
                continue
            for member_root in sorted(directory.iterdir(), key=lambda item: item.name):
                if member_root.is_symlink() or not member_root.is_dir():
                    raise UnsafeFilesystemEntry(
                        f"Plugin {directory_name} members must be regular directories"
                    )
                if kind == "mcp":
                    entries = tuple(member_root.iterdir())
                    if {path.name for path in entries} != {"mcp.json"}:
                        raise ValueError("Plugin MCP members may only contain mcp.json")
                    config_path = member_root / "mcp.json"
                    if config_path.is_symlink() or not config_path.is_file():
                        raise UnsafeFilesystemEntry(
                            "Plugin MCP mcp.json must be a regular file"
                        )
                    validate_mcp_config(
                        config_path,
                        workspace=self._home.workspace_dir,
                    )
                    local_name = member_root.name
                    version = manifest.version
                elif kind == "hook":
                    local_name, _ = DirectoryHookLoader.inspect_single_file(member_root)
                    version = manifest.version
                elif kind == "tool":
                    inspection = DirectoryToolLoader.inspect(member_root)
                    local_name, version = inspection.name, manifest.version
                else:
                    metadata, _ = DirectorySkillLoader.inspect(member_root)
                    local_name = metadata["name"]
                    version = manifest.version
                if local_name in local_names:
                    raise ValueError(f"duplicate plugin member local name: {local_name}")
                local_names.add(local_name)
                if local_name != member_root.name:
                    raise ValueError(
                        f"Plugin {kind} member local name must match its directory: {local_name}"
                    )
                CapabilityInstaller._validate_id(local_name)
                member_stat = os.stat(member_root, follow_symlinks=False)
                kind_stat = os.stat(directory, follow_symlinks=False)
                members.append(
                    PluginMember(
                        manifest.id,
                        kind,
                        local_name,
                        version,
                        (member_stat.st_dev, member_stat.st_ino),
                        (kind_stat.st_dev, kind_stat.st_ino),
                    )
                )
        root_stat = os.stat(root)
        return InstalledPlugin(
            manifest=manifest,
            content_digest=digest_directory(root, files),
            members=tuple(members),
            inode=(root_stat.st_dev, root_stat.st_ino),
        )

    @staticmethod
    def _plugin_directory_entry(plugins_fd: int, plugin_id: str) -> bool:
        entry = os.stat(plugin_id, dir_fd=plugins_fd, follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode):
            raise UnsafeFilesystemEntry(
                "installed Plugin target is not a regular directory"
            )
        return stat.S_ISDIR(entry.st_mode)

    def _read_plugin_at(self, plugins_fd: int, plugin_id: str) -> InstalledPlugin:
        try:
            plugin_fd = os.open(plugin_id, _DIRECTORY_FLAGS, dir_fd=plugins_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise UnsafeFilesystemEntry(
                "installed Plugin target is not a regular directory"
            ) from exc
        try:
            plugin = self._verify_opened_plugin(plugin_fd, plugin_id, phase="catalog")
            return plugin
        finally:
            os.close(plugin_fd)

    def with_open_member(
        self, capability_id: str, consumer: Callable[[Path, PluginMember], T]
    ) -> T | None:
        namespace, separator, local_name = str(capability_id).strip().partition("/")
        if not separator or not namespace or not local_name or "/" in local_name:
            return None
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            home_fd = os.open(self.package_root(namespace).parent.parent, _DIRECTORY_FLAGS)
            try:
                plugins_fd = self._capabilities._open_directory_at(home_fd, "plugins")
                try:
                    plugin_fd = os.open(namespace, _DIRECTORY_FLAGS, dir_fd=plugins_fd)
                    try:
                        plugin = self._verify_opened_plugin(plugin_fd, namespace, phase="load")
                        member = next((item for item in plugin.members if item.local_name == local_name), None)
                        if member is None:
                            return None
                        directory_name = {
                            "tool": "tools",
                            "skill": "skills",
                            "hook": "hooks",
                            "mcp": "mcps",
                        }[member.kind]
                        kind_fd = self._capabilities._open_directory_at(plugin_fd, directory_name)
                        try:
                            member_fd = os.open(member.local_name, _DIRECTORY_FLAGS, dir_fd=kind_fd)
                            try:
                                self._verify_open_member_chain(
                                    home_fd, plugins_fd, plugin_fd, kind_fd, member_fd,
                                    plugin, member, namespace,
                                )
                                return consumer(Path(f"/proc/self/fd/{member_fd}"), member)
                            finally:
                                os.close(member_fd)
                        finally:
                            os.close(kind_fd)
                    finally:
                        os.close(plugin_fd)
                finally:
                    os.close(plugins_fd)
            finally:
                os.close(home_fd)

    def _verify_open_member_chain(
        self, home_fd: int, plugins_fd: int, plugin_fd: int, kind_fd: int,
        member_fd: int, plugin: InstalledPlugin, member: PluginMember, plugin_id: str,
    ) -> None:
        if not self._capabilities._same_inode(
            os.stat("plugins", dir_fd=home_fd, follow_symlinks=False), os.fstat(plugins_fd)
        ):
            raise ValueError("plugins directory changed before catalog handoff")
        if not self._capabilities._same_inode(
            os.stat(plugin_id, dir_fd=plugins_fd, follow_symlinks=False), os.fstat(plugin_fd)
        ) or (os.fstat(plugin_fd).st_dev, os.fstat(plugin_fd).st_ino) != plugin.inode:
            raise ValueError("Plugin directory changed before catalog handoff")
        directory_name = {"tool": "tools", "skill": "skills", "hook": "hooks", "mcp": "mcps"}[member.kind]
        kind_stat = os.fstat(kind_fd)
        if not self._capabilities._same_inode(
            os.stat(directory_name, dir_fd=plugin_fd, follow_symlinks=False), kind_stat
        ) or (kind_stat.st_dev, kind_stat.st_ino) != member.kind_inode:
            raise ValueError("Plugin kind directory changed before catalog handoff")
        member_stat = os.stat(member.local_name, dir_fd=kind_fd, follow_symlinks=False)
        if not self._capabilities._same_inode(member_stat, os.fstat(member_fd)) or (
            member_stat.st_dev, member_stat.st_ino
        ) != member.inode:
            raise ValueError("Plugin member changed before catalog handoff")

    def _verify_opened_plugin(self, directory_fd: int, plugin_id: str, *, phase: str) -> InstalledPlugin:
        target = Path(f"/proc/self/fd/{directory_fd}") / "."
        return self._validate_package(target, expected_id=plugin_id)

    def _restore_if_safe(
        self,
        staging_fd: int,
        tombstone: str,
        plugins_fd: int,
        plugin_id: str,
        expected_stat: os.stat_result,
    ) -> str:
        if not self._capabilities._entry_matches(staging_fd, tombstone, expected_stat):
            return f"quarantine name changed; preserved recovery path: .staging/{tombstone}"
        try:
            self._capabilities._rename_noreplace(staging_fd, tombstone, plugins_fd, plugin_id)
        except FileExistsError:
            return f"target was recreated; preserved recovery path: .staging/{tombstone}"
        except OSError as exc:
            return f"quarantine could not be restored; preserved recovery path: .staging/{tombstone}; cause: {exc}"
        return f"quarantine safely restored to target path: {plugin_id}"

__all__ = ["CapabilityInstaller", "PluginInstaller"]
