"""Safe local lifecycle for directory-backed Agent configuration packages."""

from __future__ import annotations

from collections.abc import Callable, Iterable
import ctypes
import errno
import logging
import os
from dataclasses import dataclass
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import zipfile

from packaging.version import Version

from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent._internal.locking import CapabilityFilesystemLock
from bianbu_agent.agents import AgentManifest, AgentSpec, validate_agent_id
from bianbu_agent.home import AgentHome
from bianbu_agent.agents.system_paths import system_agent_directory


logger = logging.getLogger(__name__)
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
MAX_AGENT_PACKAGE_FILE_BYTES = 64 * 1024 * 1024
MAX_AGENT_PACKAGE_TOTAL_BYTES = 256 * 1024 * 1024
MAX_AGENT_PACKAGE_MEMBERS = 4096
MAX_AGENT_PACKAGE_PATH_BYTES = 1024 * 1024
# A removal marker for a packaged Agent sits inside ``agents/`` so the
# decision travels with the profile it belongs to.  The leading dot and the
# regular-file shape keep the package scans from treating it as a record.
HIDDEN_AGENT_PREFIX = ".hidden-"
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


@dataclass(frozen=True, slots=True)
class AgentPackage:
    """One validated installed Agent package."""

    root: Path
    manifest: AgentManifest
    content_digest: str

    @property
    def id(self) -> str:
        return self.manifest.spec.id

    @property
    def version(self) -> str:
        return self.manifest.version


@dataclass(frozen=True, slots=True)
class AgentPackageSecurityResult:
    """Result of publishing a package and registering its logical Agent.

    ``security_status`` is ``registered`` when the callback confirms a
    registration, ``security_pending`` when the callback is unavailable in an
    optional/development deployment, and ``not_configured`` when no callback
    was supplied.  The package itself is always structurally validated before
    the callback is invoked.
    """

    package: AgentPackage
    security_status: str


class AgentPackageManager:
    """Install, inspect, update, and remove project-local Agent packages."""

    def __init__(
        self,
        home: AgentHome,
        protected_ids: Iterable[str] = (),
        *,
        system_agents_dir: Path | None = None,
    ) -> None:
        self._home = home
        self._system_agents_dir = (
            Path(system_agents_dir) if system_agents_dir is not None
            else system_agent_directory(home.root)
        )
        self._protected_ids = frozenset(
            validate_agent_id(agent_id) for agent_id in protected_ids
        )

    @property
    def home(self) -> AgentHome:
        return self._home

    def list(self) -> tuple[AgentPackage, ...]:
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            self._prepare_roots()
            system = {item.id: item for item in self._system_packages()}
            packages: list[AgentPackage] = []
            for path in sorted(
                self._home.agents_dir.iterdir(),
                key=lambda item: item.name.casefold(),
            ):
                if not (path.is_dir() or path.is_symlink()):
                    continue
                if path.is_symlink():
                    # A redirectable package entry is an attack, not a bad file.
                    raise ValueError("Agent package root must not be a symbolic link")
                package = self._read_user_package(path)
                if package is not None:
                    packages.append(package)
            user_ids = {item.id for item in packages}
            packages.extend(item for key, item in system.items() if key not in user_ids)
            folded: dict[str, str] = {}
            for package in packages:
                previous = folded.setdefault(package.id.casefold(), package.id)
                if previous != package.id:
                    raise ValueError(
                        "Agent package IDs conflict case-insensitively: "
                        f"{previous}, {package.id}"
                    )
            return tuple(packages)

    def inspect(self, agent_id: str) -> AgentPackage | None:
        normalized = validate_agent_id(agent_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            self._prepare_roots()
            target = self._package_path(normalized)
            if target.is_symlink():
                raise ValueError("installed Agent package must not be a symbolic link")
            if not target.exists():
                return self._system_package(normalized)
            package = self._read_user_package(target, expected_id=normalized)
            return package if package is not None else self._system_package(normalized)

    def verify(self, agent_id: str) -> bool:
        return self.inspect(agent_id) is not None

    def retire_unloadable_records(self) -> tuple[str, ...]:
        """Move user Agent records that no longer parse out of ``agents/``.

        The declared Agent contract is versioned by the Runtime that writes it:
        an Agent no longer declares Hooks, for example, so a record from an
        older generation is rejected by the current manifest parser.  Such a
        record is retired into ``staging`` rather than skipped forever, which
        keeps the user directory readable without preserving a dead format.
        """

        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            return self._retire_unloadable_records_locked()

    def _retire_unloadable_records_locked(self) -> tuple[str, ...]:
        """Retire records the current declaration contract cannot read."""

        retired: list[str] = []
        for path in sorted(
            self._home.agents_dir.iterdir(),
            key=lambda item: item.name.casefold(),
        ):
            if not (path.is_dir() or path.is_symlink()):
                continue
            if path.is_symlink():
                raise ValueError("Agent package root must not be a symbolic link")
            try:
                self._inspect_root(path)
            except ValueError:
                target = self._unique_staging_path(f"retired-agent-{path.name}-")
                _rename_noreplace(path, target)
                logger.warning("Retired unloadable Agent record %s as %s", path, target)
                retired.append(path.name)
        return tuple(retired)

    def _read_user_package(
        self,
        root: Path,
        *,
        expected_id: str | None = None,
    ) -> AgentPackage | None:
        """Read one user record, retiring one the current contract rejects.

        The rejected record is moved into ``staging`` instead of being skipped
        forever, so no caller keeps guessing a declaration format this Runtime
        no longer reads.  Retirement is deliberately lock-free: read paths also
        run while the capability lock is already held (an inventory build holds
        it shared), and a second flock on the same file would block.  The move
        itself is atomic, and losing the race to another reader simply means the
        record is already gone.
        """

        try:
            return self._inspect_root(root, expected_id=expected_id)
        except ValueError as exc:
            target = self._unique_staging_path(f"retired-agent-{root.name}-")
            try:
                _rename_noreplace(root, target)
            except OSError:
                return None
            logger.warning("Retired unloadable Agent record %s as %s: %s", root, target, exc)
            return None

    def install(
        self,
        source: str | Path,
        *,
        upgrade: bool = False,
    ) -> AgentPackage:
        source_input = Path(source).expanduser()
        if source_input.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {source_input}")
        source_root = source_input.resolve()
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            stage = Path(
                tempfile.mkdtemp(prefix="agent-", dir=self._home.staging_dir)
            )
            published = False
            cleanup_stage = True
            try:
                source_is_archive = source_root.is_file()
                if source_is_archive:
                    _extract_agent_archive(source_root, stage)
                else:
                    self._copy_source(source_root, stage)
                _fsync_tree(stage)
                staged = self._inspect_root(stage)
                self._validate_bundled_tools(staged)
                if not source_is_archive and source_root.name != staged.id:
                    raise ValueError("Agent package directory name must match agent.id")
                self._assert_mutable(staged.id)
                self._assert_no_casefold_conflict(staged.id)
                target = self._package_path(staged.id)
                stage = self._stage_on_target_filesystem(stage, target)
                effective = self._inspect_root(target, expected_id=staged.id) if target.exists() else self._system_package(staged.id)
                if effective is not None and not upgrade:
                    raise FileExistsError(
                        f"Agent package already installed: {staged.id}"
                    )
                if upgrade and effective is None:
                    raise KeyError(staged.id)
                current = effective
                if current is not None and Version(staged.version) <= Version(
                    current.version
                ):
                    raise ValueError("Agent package upgrade version must be higher")
                if target.exists():
                    _rename_exchange(stage, target)
                    try:
                        _fsync_directory(target.parent)
                        _fsync_directory(self._home.staging_dir)
                    except BaseException:
                        try:
                            _rename_exchange(stage, target)
                        except BaseException:
                            cleanup_stage = False
                            raise
                        _fsync_directory(target.parent)
                        _fsync_directory(self._home.staging_dir)
                        raise
                    published = True
                    try:
                        shutil.rmtree(stage)
                    except OSError:
                        pass
                else:
                    _rename_noreplace(stage, target)
                    try:
                        _fsync_directory(target.parent)
                    except BaseException:
                        _rename_noreplace(target, stage)
                        _fsync_directory(target.parent)
                        _fsync_directory(self._home.staging_dir)
                        raise
                    published = True
                self._unhide(staged.id)
                return self._inspect_root(target, expected_id=staged.id)
            finally:
                if not published and cleanup_stage and stage.exists():
                    shutil.rmtree(stage)

    def install_with_security(
        self,
        source: str | Path,
        *,
        upgrade: bool = False,
        register: Callable[[AgentPackage], object | None] | None = None,
        required: bool = False,
    ) -> AgentPackageSecurityResult:
        """Install a validated package and register its logical Agent.

        Registration deliberately happens *after* the existing atomic
        publication.  Optional/development registration failures leave the
        package installed and report ``security_pending``.  Required failures
        roll back the publication (including restoring an upgraded package)
        before re-raising the security error.
        """

        if register is None:
            return AgentPackageSecurityResult(
                self.install(source, upgrade=upgrade), "not_configured"
            )

        # Keep an on-disk copy of the previous package so an upgrade can be
        # restored if securityd rejects registration.  The copy is created
        # before publication and cleaned up regardless of the outcome.
        source_input = Path(source).expanduser()
        previous: AgentPackage | None = None
        backup: Path | None = None
        backup_root: Path | None = None
        if upgrade:
            try:
                # Read only the candidate manifest to identify the target. A
                # malformed source still fails through the normal install path.
                staged_manifest = _source_manifest(source_input)
                previous = self.inspect(staged_manifest.spec.id)
            except (FileNotFoundError, ValueError, OSError, KeyError, zipfile.BadZipFile):
                previous = None
            if previous is not None:
                backup_root = self._unique_staging_path("agent-security-backup-")
                backup_root.mkdir(mode=0o700)
                backup = backup_root / previous.id
                shutil.copytree(previous.root, backup)

        try:
            package = self.install(source, upgrade=upgrade)
            try:
                outcome = register(package)
            except BaseException:
                if required:
                    self._rollback_security_install(package, previous, backup)
                    raise
                return AgentPackageSecurityResult(package, "security_pending")
            if outcome is None or outcome is False:
                if required:
                    error = RuntimeError("security registration unavailable")
                    self._rollback_security_install(package, previous, backup)
                    raise error
                return AgentPackageSecurityResult(package, "security_pending")
            return AgentPackageSecurityResult(package, "registered")
        finally:
            if backup_root is not None and backup_root.exists():
                shutil.rmtree(backup_root, ignore_errors=True)

    def _rollback_security_install(
        self,
        package: AgentPackage,
        previous: AgentPackage | None,
        backup: Path | None,
    ) -> None:
        """Best-effort restore used only after required registration failure."""

        try:
            with CapabilityFilesystemLock(self._home.root, exclusive=True):
                target = self._home.agents_dir / package.id
                if target.exists():
                    current = self._inspect_root(target, expected_id=package.id)
                    if current.content_digest != package.content_digest:
                        raise ValueError("Agent changed before security rollback")
                    quarantined = self._unique_staging_path("agent-rollback-")
                    _rename_noreplace(target, quarantined)
                    verified = self._inspect_root(quarantined, expected_id=package.id)
                    if verified.content_digest != package.content_digest:
                        _rename_noreplace(quarantined, target)
                        raise ValueError("Agent changed during security rollback")
                    shutil.rmtree(quarantined)
                if (previous is not None and previous.root == target
                        and backup is not None and backup.exists()):
                    restored = self._stage_on_target_filesystem(backup, target)
                    _rename_noreplace(restored, target)
                    _fsync_directory(target.parent)
        except BaseException:
            # Preserve the securityd error as the actionable exception.  The
            # package manager's next startup verification will surface any
            # unusual filesystem rollback failure.
            return

    def sync(
        self,
        source: str | Path,
        *,
        files: Iterable[str | Path] | None = None,
    ) -> AgentPackage:
        """Atomically replace development content without version ordering."""

        source_input = Path(source).expanduser()
        if source_input.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {source_input}")
        source_root = source_input.resolve()
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            stage = Path(tempfile.mkdtemp(prefix="agent-sync-", dir=self._home.staging_dir))
            published = False
            cleanup_stage = True
            try:
                if files is None:
                    self._copy_source(source_root, stage)
                else:
                    _copy_selected_files(source_root, stage, files)
                _fsync_tree(stage)
                staged = self._inspect_root(stage)
                self._validate_bundled_tools(staged)
                self._assert_no_casefold_conflict(staged.id)
                target = self._package_path(staged.id)
                stage = self._stage_on_target_filesystem(stage, target)
                if target.exists():
                    self._inspect_root(target, expected_id=staged.id)
                    _rename_exchange(stage, target)
                    try:
                        _fsync_directory(target.parent)
                        _fsync_directory(self._home.staging_dir)
                    except BaseException:
                        try:
                            _rename_exchange(stage, target)
                        except BaseException:
                            cleanup_stage = False
                            raise
                        _fsync_directory(target.parent)
                        _fsync_directory(self._home.staging_dir)
                        raise
                    published = True
                    shutil.rmtree(stage, ignore_errors=True)
                else:
                    _rename_noreplace(stage, target)
                    try:
                        _fsync_directory(target.parent)
                    except BaseException:
                        _rename_noreplace(target, stage)
                        _fsync_directory(target.parent)
                        _fsync_directory(self._home.staging_dir)
                        raise
                    published = True
                self._unhide(staged.id)
                return self._inspect_root(target, expected_id=staged.id)
            finally:
                if not published and cleanup_stage and stage.exists():
                    shutil.rmtree(stage)

    def create(self, spec: AgentSpec, *, version: str = "1.0.0") -> AgentPackage:
        manifest = AgentManifest(schema_version=1, version=version, spec=spec)
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            self._assert_no_casefold_conflict(spec.id)
            target = self._package_path(spec.id)
            if target.exists() or self._system_package(spec.id) is not None:
                raise FileExistsError(f"Agent package already installed: {spec.id}")
            stage = Path(
                tempfile.mkdtemp(prefix="agent-", dir=self._home.staging_dir)
            )
            published = False
            try:
                with (stage / "agent.yaml").open("w", encoding="utf-8") as stream:
                    stream.write(manifest.to_yaml())
                    stream.flush()
                    os.fsync(stream.fileno())
                _fsync_directory(stage)
                self._inspect_root(stage, expected_id=spec.id)
                _rename_noreplace(stage, target)
                try:
                    _fsync_directory(target.parent)
                except BaseException:
                    _rename_noreplace(target, stage)
                    _fsync_directory(target.parent)
                    _fsync_directory(self._home.staging_dir)
                    raise
                published = True
                self._unhide(spec.id)
                return self._inspect_root(target, expected_id=spec.id)
            finally:
                if not published and stage.exists():
                    shutil.rmtree(stage)

    def update(self, spec: AgentSpec) -> AgentPackage:
        normalized = validate_agent_id(spec.id)
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            target = self._ensure_user_copy(normalized)
            if not target.exists():
                raise KeyError(normalized)
            current = self._inspect_root(target, expected_id=normalized)
            manifest = AgentManifest(
                schema_version=1,
                version=current.version,
                spec=spec,
            )
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target,
                prefix=".agent.yaml.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(manifest.to_yaml())
                    stream.flush()
                    os.fsync(stream.fileno())
                AgentManifest.from_path(temporary)
                os.replace(temporary, target / "agent.yaml")
                _fsync_directory(target)
                self._unhide(normalized)
                return self._inspect_root(target, expected_id=normalized)
            finally:
                temporary.unlink(missing_ok=True)

    def assert_deletable(self, agent_id: str) -> None:
        normalized = validate_agent_id(agent_id)
        self._assert_mutable(normalized)

    def uninstall(self, agent_id: str) -> bool:
        """Remove an Agent, hiding the packaged declaration of the same ID.

        A packaged Agent lives on a read-only filesystem, so deleting it means
        remembering the removal: one marker next to the user records.  The
        signed package content stays untouched, the user stops seeing the
        expert, and re-installing an Agent with that ID clears the marker.
        """

        normalized = validate_agent_id(agent_id)
        self.assert_deletable(normalized)
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            packaged = self._system_package(normalized) is not None
            target = self._package_path(normalized)
            if target.is_symlink():
                raise ValueError("installed Agent package must not be a symbolic link")
            removed = False
            if target.exists():
                self._inspect_root(target, expected_id=normalized)
                quarantined = target.parent / f".agent-uninstall-{secrets.token_hex(16)}"
                _rename_noreplace(target, quarantined)
                try:
                    self._inspect_root(quarantined, expected_id=normalized)
                    shutil.rmtree(quarantined)
                except BaseException:
                    if quarantined.exists() and not target.exists():
                        _rename_noreplace(quarantined, target)
                    raise
                removed = True
            if packaged:
                self._hide(normalized)
                removed = True
            return removed

    def hidden_ids(self) -> frozenset[str]:
        """Return the packaged Agents this profile has removed."""

        try:
            entries = tuple(self._home.agents_dir.iterdir())
        except FileNotFoundError:
            return frozenset()
        hidden: set[str] = set()
        for path in entries:
            if not path.name.startswith(HIDDEN_AGENT_PREFIX):
                continue
            if not path.is_file():
                continue
            try:
                hidden.add(validate_agent_id(path.name[len(HIDDEN_AGENT_PREFIX):]))
            except ValueError:
                continue
        return frozenset(hidden)

    def _hide(self, agent_id: str) -> None:
        marker = self._home.agents_dir / f"{HIDDEN_AGENT_PREFIX}{agent_id}"
        if marker.exists():
            return
        descriptor = os.open(
            marker,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self._home.agents_dir)
        _fsync_directory(self._home.root)

    def _unhide(self, agent_id: str) -> None:
        marker = self._home.agents_dir / f"{HIDDEN_AGENT_PREFIX}{agent_id}"
        try:
            marker.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(self._home.agents_dir)
        _fsync_directory(self._home.root)

    def avatar_path(self, agent_id: str) -> Path | None:
        """Return the regular image owned by an Agent package, if present."""

        normalized = validate_agent_id(agent_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=False):
            self._prepare_roots()
            system = self._system_package(normalized)
            user = self._home.agents_dir / normalized
            if user.exists() or user.is_symlink():
                # A record this contract rejects is retired here, so the
                # packaged declaration of the same Agent stays the one served.
                target = user if self._read_user_package(user, expected_id=normalized) is not None else (
                    system.root if system is not None else user
                )
            else:
                target = system.root if system is not None else user
            if not target.exists():
                return None
            matches = [
                target / f"avatar{suffix}"
                for suffix in (".jpg", ".png", ".gif", ".webp")
                if _is_regular_file(target / f"avatar{suffix}")
            ]
            if len(matches) > 1:
                raise ValueError("Agent package contains multiple avatar images")
            return matches[0] if matches else None

    def write_avatar(self, agent_id: str, suffix: str, payload: bytes) -> Path:
        """Atomically replace the image owned by one Agent package."""

        normalized = validate_agent_id(agent_id)
        if suffix not in {".jpg", ".png", ".gif", ".webp"}:
            raise ValueError("unsupported Agent avatar suffix")
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            root = self._ensure_user_copy(normalized)
            if not root.exists():
                raise KeyError(normalized)
            self._inspect_root(root, expected_id=normalized)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=root,
                prefix=".avatar.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            target = root / f"avatar{suffix}"
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                for old_suffix in (".jpg", ".png", ".gif", ".webp"):
                    old = root / f"avatar{old_suffix}"
                    if old != target:
                        old.unlink(missing_ok=True)
                _fsync_directory(root)
                return target
            finally:
                temporary.unlink(missing_ok=True)

    def delete_avatar(self, agent_id: str) -> bool:
        """Delete the image owned by one Agent package."""

        normalized = validate_agent_id(agent_id)
        with CapabilityFilesystemLock(self._home.root, exclusive=True):
            self._prepare_roots()
            root = self._ensure_user_copy(normalized)
            if not root.exists():
                raise KeyError(normalized)
            self._inspect_root(root, expected_id=normalized)
            removed = False
            for suffix in (".jpg", ".png", ".gif", ".webp"):
                target = root / f"avatar{suffix}"
                if _is_regular_file(target):
                    target.unlink()
                    removed = True
            if removed:
                _fsync_directory(root)
            return removed

    def _prepare_roots(self) -> None:
        for path, label in (
            (self._home.root, "Home"),
            (self._home.agents_dir, "agents"),
            (self._home.staging_dir, "staging"),
        ):
            if path.is_symlink():
                raise ValueError(f"Bianbu Agent {label} directory is unsafe")
            path.mkdir(parents=True, exist_ok=True)
            if not path.is_dir():
                raise ValueError(f"Bianbu Agent {label} path is not a directory")

    def _inspect_root(
        self,
        root: Path,
        *,
        expected_id: str | None = None,
    ) -> AgentPackage:
        root = Path(root)
        if root.is_symlink():
            raise ValueError("Agent package root must not be a symbolic link")
        files = regular_files(root)
        manifest_path = root / "agent.yaml"
        if manifest_path not in files:
            # The walk skips a directory it cannot read, so an unreadable
            # package looks exactly like an absent declaration; name both.
            raise ValueError(f"Agent package has no readable agent.yaml: {root}")
        manifest = AgentManifest.from_path(manifest_path)
        if expected_id is not None and manifest.spec.id != expected_id:
            raise ValueError("Agent package directory name must match agent.id")
        return AgentPackage(
            root=root.resolve(),
            manifest=manifest,
            content_digest=digest_directory(root, files),
        )

    def _assert_no_casefold_conflict(self, agent_id: str) -> None:
        folded = agent_id.casefold()
        for entry in (*self._home.agents_dir.iterdir(), *(p.root for p in self._system_packages())):
            if entry.name.casefold() == folded and entry.name != agent_id:
                raise ValueError(
                    "Agent package ID conflicts case-insensitively with "
                    f"{entry.name}"
                )

    def _validate_bundled_tools(self, package: AgentPackage) -> None:
        from bianbu_agent.agents.bundled_tools import load_bundled_tools

        tools = load_bundled_tools(
            package.root, package.manifest, workspace_root=self._home.workspace_dir,
        )
        if not self._home.plugins_dir.exists():
            return
        for plugin in self._home.plugins_dir.iterdir():
            if plugin.name.casefold() != package.id.casefold():
                continue
            for qualified_id in tools:
                local_name = qualified_id.partition("/")[2]
                if (plugin / "tools" / local_name).exists():
                    raise ValueError(f"Agent Tool ownership collision: {qualified_id}")

    def _assert_mutable(self, agent_id: str) -> None:
        if agent_id in self._protected_ids:
            raise ValueError(f"Agent package is protected: {agent_id}")

    def _package_path(self, agent_id: str) -> Path:
        return self._home.agents_dir / agent_id

    def _ensure_user_copy(self, agent_id: str) -> Path:
        target = self._package_path(agent_id)
        if target.exists() or target.is_symlink():
            try:
                self._inspect_root(target, expected_id=agent_id)
                return target
            except ValueError:
                pass
            if self._system_package(agent_id) is None:
                raise ValueError(f"installed Agent record is unloadable: {agent_id}")
            # An unloadable record cannot be edited in place. Retire it so the
            # packaged Agent of the same ID can be materialised again; the
            # retired directory is dropped by the usual staging cleanup.
            retired = self._unique_staging_path(f"retired-agent-{agent_id}-")
            _rename_noreplace(target, retired)
            logger.warning("Retired unloadable Agent record %s as %s", target, retired)
        system = self._system_package(agent_id)
        if system is None:
            raise KeyError(agent_id)
        stage = Path(tempfile.mkdtemp(prefix="agent-override-", dir=self._home.staging_dir))
        try:
            self._copy_source(system.root, stage)
            _fsync_tree(stage)
            self._inspect_root(stage, expected_id=agent_id)
            _rename_noreplace(stage, target)
            _fsync_directory(target.parent)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return target

    def _stage_on_target_filesystem(self, stage: Path, target: Path) -> Path:
        # System profiles may live on a different filesystem from user data.
        # Keep atomic exchange/rename local to the destination filesystem.
        if stage.stat().st_dev == target.parent.stat().st_dev:
            return stage
        relocated = Path(tempfile.mkdtemp(prefix=".agent-stage-", dir=target.parent))
        try:
            shutil.copytree(stage, relocated, dirs_exist_ok=True)
            _fsync_tree(relocated)
        except BaseException:
            shutil.rmtree(relocated)
            raise
        shutil.rmtree(stage)
        return relocated

    def is_system(self, agent_id: str) -> bool:
        """Report whether the packaged declaration is the effective one.

        A user directory that cannot be loaded is retired before this answer is
        produced, so the packaged Agent of the same ID keeps both its content
        and its read-only presentation instead of being reported as editable.
        """

        normalized = validate_agent_id(agent_id)
        user = self._package_path(normalized)
        if (user.exists() or user.is_symlink()) and self._read_user_package(
            user, expected_id=normalized
        ) is not None:
            return False
        return any(
            item.id.casefold() == normalized.casefold()
            for item in self._system_packages()
        )

    def _system_package(self, agent_id: str) -> AgentPackage | None:
        return next((item for item in self._system_packages() if item.id == agent_id), None)

    def _system_packages(self) -> tuple[AgentPackage, ...]:
        from bianbu_agent.system_resources import system_resource_dir
        packages = {}
        hidden = self.hidden_ids()
        # Profile-specific starter keeps its stable ID and wins over package defaults.
        for root in (system_resource_dir("agents"), self._system_agents_dir):
            if root.is_symlink():
                raise ValueError("system Agent directory must not be a symbolic link")
            if not root.exists():
                continue
            for path in sorted(root.iterdir()):
                if path.name.startswith(".") or not (path.is_dir() or path.is_symlink()):
                    continue
                if path.is_symlink():
                    raise ValueError("system Agent declaration must not be a symbolic link")
                if path.name in hidden:
                    continue
                packages[path.name] = self._inspect_root(path, expected_id=path.name)
        return tuple(packages.values())

    def _unique_staging_path(self, prefix: str) -> Path:
        for _ in range(128):
            candidate = self._home.staging_dir / f"{prefix}{secrets.token_hex(16)}"
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
        raise RuntimeError("could not allocate Agent package staging path")

    @staticmethod
    def _copy_source(source: Path, destination: Path) -> None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            source_fd = os.open(source, flags)
        except OSError as exc:
            raise ValueError(f"unsafe Agent package source: {source}") from exc
        try:
            _copy_directory_fd(source_fd, destination, Path(), set())
        finally:
            os.close(source_fd)


def _rename_noreplace(source: Path, destination: Path) -> None:
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 with RENAME_NOREPLACE is required")
    result = _RENAMEAT2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _extract_agent_archive(source: Path, destination: Path) -> None:
    if source.suffix != ".agent":
        raise ValueError("Agent package archive must use the .agent suffix")
    seen: set[str] = set()
    namespace: dict[str, str] = {}
    total_size = 0
    total_path_bytes = 0
    try:
        with zipfile.ZipFile(source) as archive:
            members = archive.infolist()
            if len(members) > MAX_AGENT_PACKAGE_MEMBERS:
                raise ValueError("Agent package archive has too many entries")
            for info in members:
                original_name = info.orig_filename
                raw_name = info.filename
                if original_name != raw_name:
                    raise ValueError(
                        f"unsafe Agent package archive path: {original_name!r}"
                    )
                name = raw_name[:-1] if info.is_dir() else raw_name
                parts = name.split("/") if name else []
                if (
                    not name
                    or "\\" in raw_name
                    or "\x00" in raw_name
                    or raw_name.startswith("/")
                    or any(part in {"", ".", ".."} for part in parts)
                ):
                    raise ValueError(f"unsafe Agent package archive path: {raw_name}")
                folded = name.casefold()
                if folded in seen:
                    raise ValueError(f"duplicate case-insensitive archive path: {name}")
                seen.add(folded)
                _register_package_path(namespace, parts, kind="archive")
                total_path_bytes += len(raw_name.encode("utf-8", errors="surrogatepass"))
                if total_path_bytes > MAX_AGENT_PACKAGE_PATH_BYTES:
                    raise ValueError("Agent package archive path metadata is too large")
                unix_mode = info.external_attr >> 16
                file_type = stat.S_IFMT(unix_mode)
                if file_type == stat.S_IFLNK:
                    raise ValueError(f"symbolic links are not allowed: {name}")
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise ValueError(f"unsupported Agent package archive entry: {name}")
                total_size += info.file_size
                if (
                    info.file_size > MAX_AGENT_PACKAGE_FILE_BYTES
                    or total_size > MAX_AGENT_PACKAGE_TOTAL_BYTES
                ):
                    raise ValueError("Agent package archive is too large")
                target = destination.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=False)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                )
                try:
                    with archive.open(info) as source_stream, os.fdopen(
                        descriptor, "wb", closefd=False
                    ) as target_stream:
                        shutil.copyfileobj(source_stream, target_stream)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
                finally:
                    os.close(descriptor)
                os.chmod(target, 0o755 if unix_mode & 0o111 else 0o644)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid Agent package archive: {source}") from exc


def _source_manifest(source: Path) -> AgentManifest:
    """Read a candidate package manifest without publishing/extracting it."""

    if source.is_file():
        if source.suffix != ".agent":
            raise ValueError("Agent package archive must use the .agent suffix")
        try:
            with zipfile.ZipFile(source) as archive:
                try:
                    raw = archive.read("agent.yaml")
                except KeyError as exc:
                    raise ValueError("Agent package is missing agent.yaml") from exc
        except zipfile.BadZipFile as exc:
            raise ValueError(f"invalid Agent package archive: {source}") from exc
        return AgentManifest.from_yaml(raw.decode("utf-8"))
    return AgentManifest.from_path(source / "agent.yaml")


def register_project_path(namespace: dict[str, str], parts: tuple[str, ...]) -> None:
    """Register a project path in the package's case-insensitive namespace."""

    _register_package_path(namespace, parts, kind="resource")


def _register_package_path(
    namespace: dict[str, str], parts: list[str] | tuple[str, ...], *, kind: str
) -> None:
    prefix: list[str] = []
    for part in parts:
        prefix.append(part)
        exact = "/".join(prefix)
        folded = exact.casefold()
        previous = namespace.setdefault(folded, exact)
        if previous != exact:
            raise ValueError(
                f"duplicate case-insensitive {kind} path: {exact} conflicts with {previous}"
            )


def _rename_exchange(source: Path, destination: Path) -> None:
    if _RENAMEAT2 is None:
        raise OSError(errno.ENOSYS, "renameat2 with RENAME_EXCHANGE is required")
    result = _RENAMEAT2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_EXCHANGE,
    )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _copy_directory_fd(
    source_fd: int,
    destination: Path,
    relative_root: Path,
    seen_names: set[str],
) -> None:
    try:
        names = sorted((entry.name for entry in os.scandir(source_fd)), key=str.casefold)
    except OSError as exc:
        raise ValueError("Agent package source changed during installation") from exc
    for name in names:
        relative = relative_root / name
        relative_name = relative.as_posix()
        folded = relative_name.casefold()
        if folded in seen_names:
            raise ValueError(
                f"duplicate case-insensitive resource path: {relative_name}"
            )
        seen_names.add(folded)
        try:
            listed_stat = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            if stat.S_ISLNK(listed_stat.st_mode):
                raise ValueError(
                    f"symbolic links are not allowed: {relative_name}"
                )
            if not (
                stat.S_ISDIR(listed_stat.st_mode)
                or stat.S_ISREG(listed_stat.st_mode)
            ):
                raise ValueError(
                    f"unsupported Agent package filesystem entry: {relative_name}"
                )
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=source_fd,
            )
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError(
                f"unsafe Agent package source entry: {relative_name}"
            ) from exc
        try:
            entry_stat = os.fstat(descriptor)
            target = destination / relative
            if stat.S_ISDIR(entry_stat.st_mode):
                target.mkdir(mode=0o700)
                _copy_directory_fd(descriptor, destination, relative, seen_names)
                os.chmod(target, stat.S_IMODE(entry_stat.st_mode) or 0o755)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError(
                    f"unsupported Agent package filesystem entry: {relative_name}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target_fd = os.open(
                target,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                stat.S_IMODE(entry_stat.st_mode) or 0o600,
            )
            try:
                with os.fdopen(os.dup(descriptor), "rb") as source_stream:
                    with os.fdopen(target_fd, "wb", closefd=False) as target_stream:
                        shutil.copyfileobj(source_stream, target_stream)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
            finally:
                os.close(target_fd)
        finally:
            os.close(descriptor)


def _copy_selected_files(
    source: Path,
    destination: Path,
    files: Iterable[str | Path],
) -> None:
    source = source.resolve()
    seen: set[str] = set()
    for value in sorted((Path(item) for item in files), key=lambda item: item.as_posix()):
        relative = value if not value.is_absolute() else value.resolve().relative_to(source)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"unsafe Agent package source entry: {relative}")
        name = relative.as_posix()
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate case-insensitive resource path: {name}")
        seen.add(folded)
        source_file = source / relative
        if source_file.is_symlink() or not source_file.is_file():
            raise ValueError(f"unsafe Agent package source entry: {name}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        source_descriptor = os.open(
            source_file,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            source_stat = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError(f"unsupported Agent package filesystem entry: {name}")
            target_descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                with os.fdopen(os.dup(source_descriptor), "rb") as source_stream:
                    with os.fdopen(target_descriptor, "wb", closefd=False) as target_stream:
                        shutil.copyfileobj(source_stream, target_stream)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
            finally:
                os.close(target_descriptor)
        finally:
            os.close(source_descriptor)
        os.chmod(target, 0o755 if source_stat.st_mode & 0o111 else 0o644)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    directories.extend(
        path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()
    )
    for directory in reversed(directories):
        _fsync_directory(directory)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["AgentPackage", "AgentPackageManager", "AgentPackageSecurityResult"]
