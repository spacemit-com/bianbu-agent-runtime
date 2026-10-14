from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

import yaml

from bianbu_agent._internal.json_records import (
    SCHEMA_VERSION,
    validate_record_id,
)
from bianbu_agent.errors import (
    ModelRecordIntegrityError,
    ModelRecordRollbackError,
    ModelResolutionError,
)
from bianbu_agent.models._capability_transaction import DurableModelPairJournal
from bianbu_agent.models._record_lock import ModelRecordFileLock
from bianbu_agent.models.providers import (
    ProviderConfig,
    normalize_provider_configuration,
)


def _durability_boundary(name: str) -> None:
    """Internal crash-injection seam; production execution intentionally does nothing."""


class _SecureModelRecordDirectory:
    """Store private YAML records below one directory per provider."""

    def __init__(self, root: Path, *, read_only: bool = False) -> None:
        self.read_only = read_only
        candidate = Path(root).expanduser()
        if candidate.is_symlink():
            raise ValueError(
                f"model directory must not be a symbolic link: {candidate}"
            )
        self.root = candidate.resolve()
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        if self.read_only:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError(f"unsafe model directory: {self.root}")

    async def close(self) -> None:
        return None

    def path(self, record_id: object) -> Path:
        normalized = validate_record_id(record_id)
        found = self._find_path(normalized)
        return found or self.root / f"{normalized}.yaml"

    def list_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(self._record_index(), key=lambda item: (item.casefold(), item))
        )

    def ensure_casefold_available(self, record_id: str) -> None:
        folded = record_id.casefold()
        conflict = next(
            (item for item in self.list_ids() if item.casefold() == folded and item != record_id),
            None,
        )
        if conflict is not None:
            raise ValueError(
                f"record id conflicts case-insensitively with existing record: {conflict}"
            )

    def read(self, record_id: object) -> dict[str, object] | None:
        return self._read_record(record_id, require_private_permissions=True)

    def read_for_replace_validation(
        self,
        record_id: object,
    ) -> dict[str, object] | None:
        return self._read_record(record_id, require_private_permissions=False)

    def create(self, record_id: object, payload: dict[str, object]) -> None:
        if self.read_only:
            raise ValueError("system Model directory is read-only")
        normalized = validate_record_id(record_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ensure_casefold_available(normalized)
        target = self._target_path(normalized, payload)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_safe_provider_directory(target.parent)
        if target.exists():
            raise ValueError(f"model file already exists: {target.relative_to(self.root)}")
        temporary = self._write_temporary(normalized, payload, target.parent)
        try:
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise ValueError(f"record already exists: {normalized}") from exc
            _durability_boundary(f"{normalized}:publish-link")
            self._fsync_directory(target.parent)
            self._fsync_directory(self.root)
            _durability_boundary(f"{normalized}:directory-fsync")
        finally:
            temporary.unlink(missing_ok=True)

    def _read_record(
        self,
        record_id: object,
        *,
        require_private_permissions: bool,
    ) -> dict[str, object] | None:
        normalized = validate_record_id(record_id)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(self.path(normalized), flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValueError(f"unsafe Model YAML record: {normalized}") from exc
        try:
            entry = os.fstat(descriptor)
            if not stat.S_ISREG(entry.st_mode):
                raise ValueError(f"unsafe Model YAML record: {normalized}")
            if (
                (self.read_only and bool(entry.st_mode & 0o022))
                or (not self.read_only and require_private_permissions
                    and stat.S_IMODE(entry.st_mode) != 0o600)
            ):
                raise ModelResolutionError(
                    f"Model record permissions unsafe: {normalized}"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                try:
                    payload = yaml.safe_load(stream)
                except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                    raise ValueError(
                        f"invalid Model YAML record: {normalized}"
                    ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(payload, dict):
            raise ValueError(f"invalid Model YAML record: {normalized}")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported Model schema_version: {normalized}")
        if payload.get("id") != normalized:
            raise ValueError(f"Model record id mismatch: {normalized}")
        return payload

    def replace(self, record_id: object, payload: dict[str, object]) -> None:
        if self.read_only:
            raise ValueError("system Model directory is read-only")
        normalized = validate_record_id(record_id)
        target = self._find_path(normalized)
        if target is None:
            raise KeyError(normalized)
        try:
            entry = target.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            raise KeyError(normalized) from exc
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise ValueError(f"unsafe Model YAML record: {normalized}")
        replacement = self._target_path(normalized, payload)
        replacement.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_safe_provider_directory(replacement.parent)
        if replacement != target and replacement.exists():
            raise ValueError(
                f"model file already exists: {replacement.relative_to(self.root)}"
            )
        temporary = self._write_temporary(normalized, payload, replacement.parent)
        try:
            if replacement != target:
                self._begin_move(normalized, target, replacement)
            os.replace(temporary, replacement)
            _durability_boundary(f"{normalized}:publish-replace")
            self._fsync_directory(replacement.parent)
            if replacement != target:
                target.unlink()
                self._fsync_directory(target.parent)
                self._remove_empty_provider_directory(target.parent)
            self._fsync_directory(self.root)
            _durability_boundary(f"{normalized}:directory-fsync")
            if replacement != target:
                self._commit_move()
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, record_id: object) -> bool:
        if self.read_only:
            raise ValueError("system Model directory is read-only")
        normalized = validate_record_id(record_id)
        target = self._find_path(normalized)
        if target is None:
            return False
        if self.read(normalized) is None:
            return False
        target.unlink()
        _durability_boundary(f"{normalized}:publish-delete")
        self._fsync_directory(target.parent)
        self._remove_empty_provider_directory(target.parent)
        self._fsync_directory(self.root)
        _durability_boundary(f"{normalized}:delete-directory-fsync")
        return True

    @property
    def _hidden_directory(self) -> Path:
        """Directory of delete markers for records this layer does not own.

        A packaged record lives on a read-only filesystem, so the only way to
        remove it is to remember the removal.  One empty file per record keeps
        the decision beside the credential records it hides, and a name that
        starts with a dot keeps the record scan from treating it as a Provider
        directory.
        """
        return self.root / ".hidden"

    def hidden_ids(self) -> frozenset[str]:
        directory = self._hidden_directory
        if not directory.is_dir():
            return frozenset()
        hidden: set[str] = set()
        for path in directory.iterdir():
            if path.name.startswith("."):
                continue
            try:
                hidden.add(validate_record_id(path.name))
            except ValueError:
                continue
        return frozenset(hidden)

    def hide(self, record_id: object) -> None:
        if self.read_only:
            raise ValueError("system Model directory is read-only")
        normalized = validate_record_id(record_id)
        directory = self._hidden_directory
        directory.mkdir(parents=True, exist_ok=True)
        marker = directory / normalized
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
        self._fsync_directory(directory)
        self._fsync_directory(self.root)

    def unhide(self, record_id: object) -> None:
        if self.read_only:
            return
        marker = self._hidden_directory / validate_record_id(record_id)
        try:
            marker.unlink()
        except FileNotFoundError:
            return
        self._fsync_directory(marker.parent)
        self._fsync_directory(self.root)

    def _write_temporary(
        self,
        record_id: str,
        payload: dict[str, object],
        directory: Path,
    ) -> Path:
        document = dict(payload)
        document["id"] = record_id
        document["schema_version"] = SCHEMA_VERSION
        try:
            content = yaml.safe_dump(
                document,
                allow_unicode=True,
                sort_keys=False,
            ).encode("utf-8")
        except (TypeError, ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"record is not YAML serializable: {exc}") from exc
        descriptor, name = tempfile.mkstemp(prefix=".record-", dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                _durability_boundary(f"{record_id}:temporary-write")
                stream.flush()
                os.fsync(stream.fileno())
                _durability_boundary(f"{record_id}:temporary-fsync")
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return temporary

    def _record_index(self) -> dict[str, Path]:
        if not self.root.exists():
            return {}
        if self.read_only:
            if self._move_path.exists():
                raise ModelResolutionError("system Model directory has a pending move")
        else:
            self._recover_move()
        result: dict[str, Path] = {}
        for provider_dir in self.root.iterdir():
            if provider_dir.name.startswith("."):
                continue
            entry = provider_dir.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
                raise ValueError(f"unsafe Model provider directory: {provider_dir.name}")
            for path in provider_dir.iterdir():
                if path.suffix not in {".yaml", ".yml"}:
                    continue
                file_entry = path.stat(follow_symlinks=False)
                if stat.S_ISLNK(file_entry.st_mode) or not stat.S_ISREG(file_entry.st_mode):
                    raise ValueError(f"unsafe Model YAML record: {path.name}")
                record_id = self._read_id(path)
                if record_id in result:
                    raise ValueError(f"duplicate Model record id: {record_id}")
                result[record_id] = path
        return result

    def _find_path(self, record_id: str) -> Path | None:
        return self._record_index().get(record_id)

    @staticmethod
    def _read_id(path: Path) -> str:
        try:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            return validate_record_id(payload["id"])
        except (KeyError, OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
            raise ValueError(f"invalid Model YAML record: {path.name}") from None

    def _target_path(self, record_id: str, payload: dict[str, object]) -> Path:
        provider = _path_component(payload.get("provider"), field_name="provider")
        if record_id.startswith(("provider-", "custom-provider-")):
            filename = "provider"
        elif (
            record_id.startswith("capability-")
            and isinstance(payload.get("parameters"), Mapping)
            and "model_id" in payload["parameters"]
        ):
            filename = f".binding-{record_id}"
        else:
            filename = _path_component(payload.get("model"), field_name="model")
            if filename == "provider":
                filename = "model-provider"
        return self.root / provider / f"{filename}.yaml"

    @property
    def _move_path(self) -> Path:
        return self.root / ".model-record-move"

    def _begin_move(self, record_id: str, old: Path, new: Path) -> None:
        if self._move_path.exists():
            raise ModelResolutionError("pending Model record move")
        document = {
            "schema_version": SCHEMA_VERSION,
            "record_id": record_id,
            "old": old.relative_to(self.root).as_posix(),
            "new": new.relative_to(self.root).as_posix(),
        }
        descriptor, name = tempfile.mkstemp(
            prefix=".model-move-",
            dir=self.root,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(
                    (json.dumps(document, sort_keys=True) + "\n").encode("utf-8")
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._move_path)
            self._fsync_directory(self.root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _recover_move(self) -> None:
        if not self._move_path.exists():
            return
        try:
            entry = self._move_path.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(entry.st_mode)
                or not stat.S_ISREG(entry.st_mode)
                or stat.S_IMODE(entry.st_mode) != 0o600
            ):
                raise ValueError
            document = json.loads(self._move_path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError
            if document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            record_id = validate_record_id(document["record_id"])
            old = self._validated_move_target(document["old"])
            new = self._validated_move_target(document["new"])
            if old == new:
                raise ValueError
        except (KeyError, OSError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise ModelResolutionError("invalid Model record move") from None

        try:
            old_exists = self._validate_move_record(old, record_id)
            new_exists = self._validate_move_record(new, record_id)
        except (OSError, TypeError, UnicodeDecodeError, ValueError, yaml.YAMLError):
            raise ModelResolutionError("invalid Model record move") from None
        if old_exists:
            if new_exists:
                new.unlink()
                self._fsync_directory(new.parent)
                self._remove_empty_provider_directory(new.parent)
        elif not new_exists:
            raise ModelResolutionError("incomplete Model record move")
        self._commit_move()

    def _validated_move_target(self, value: object) -> Path:
        if not isinstance(value, str):
            raise ValueError
        relative = Path(value)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.suffix not in {".yaml", ".yml"}
        ):
            raise ValueError
        return self.root / relative

    def _validate_move_record(self, path: Path, record_id: str) -> bool:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return False
        try:
            entry = os.fstat(descriptor)
            if (
                not stat.S_ISREG(entry.st_mode)
                or stat.S_IMODE(entry.st_mode) != 0o600
            ):
                raise ValueError
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                document = yaml.safe_load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != SCHEMA_VERSION
            or document.get("id") != record_id
            or self._target_path(record_id, document) != path
        ):
            raise ValueError
        return True

    def _commit_move(self) -> None:
        self._move_path.unlink(missing_ok=True)
        self._fsync_directory(self.root)

    @staticmethod
    def _ensure_safe_provider_directory(directory: Path) -> None:
        entry = directory.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISDIR(entry.st_mode):
            raise ValueError(f"unsafe Model provider directory: {directory.name}")

    @staticmethod
    def _remove_empty_provider_directory(directory: Path) -> None:
        try:
            directory.rmdir()
        except OSError:
            pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)



class _LayeredModelRecordDirectory:
    """Merge read-only package defaults with private user records."""
    def __init__(self, root: Path):
        from bianbu_agent.system_resources import system_resource_dir
        self.user = _SecureModelRecordDirectory(root)
        self.system = _SecureModelRecordDirectory(system_resource_dir("models"), read_only=True)
        self.root, self.lock = self.user.root, self.user.lock

    async def open(self):
        await self.user.open()

    async def close(self):
        await self.user.close()

    def list_ids(self):
        names = set(self.user.list_ids()) | (
            set(self.system.list_ids()) - self.user.hidden_ids()
        )
        folded = {}
        for name in names:
            if name.casefold() in folded and folded[name.casefold()] != name:
                raise ValueError("Model IDs conflict case-insensitively across layers")
            folded[name.casefold()] = name
        return tuple(sorted(names, key=lambda name: (name.casefold(), name)))

    def read(self, record_id):
        value = self.user.read(record_id)
        if value is not None:
            return value
        if record_id in self.user.hidden_ids():
            return None
        return self.system.read(record_id)

    def is_packaged(self, record_id):
        """Report whether the read-only package layer declares this record."""

        return self.system.read(record_id) is not None

    def read_for_replace_validation(self, record_id):
        value = self.user.read_for_replace_validation(record_id)
        return value if value is not None else self.system.read(record_id)

    def path(self, record_id):
        if record_id in self.user.list_ids():
            return self.user.path(record_id)
        if record_id in self.system.list_ids():
            return self.system.path(record_id)
        return self.user.path(record_id)

    def ensure_casefold_available(self, record_id):
        if any(name.casefold() == record_id.casefold() and name != record_id for name in self.list_ids()):
            raise ValueError("Model IDs conflict case-insensitively across layers")

    def create(self, record_id, payload):
        self.ensure_casefold_available(record_id)
        # A hidden packaged record is not an existing record: creating the same
        # ID is how a user takes a removed Provider back.
        if self.read(record_id) is not None:
            raise ValueError(f"record already exists: {record_id}")
        self.user.create(record_id, payload)
        # Re-creating a hidden Provider is how a user takes a packaged default
        # back, so the removal marker has to go with the new record.
        self.user.unhide(record_id)

    def replace(self, record_id, payload):
        self.ensure_casefold_available(record_id)
        if self.user.read_for_replace_validation(record_id) is not None:
            self.user.replace(record_id, payload)
        elif self.system.read(record_id) is not None:
            self.user.create(record_id, payload)
        else:
            raise KeyError(record_id)
        self.user.unhide(record_id)

    def delete(self, record_id):
        """Delete a record from the user layer, hiding a packaged one.

        The package owns its own files, so deleting a packaged record writes a
        removal marker instead: the user loses the Provider, the signed package
        content stays byte-identical, and running with no marker brings it back.
        """

        packaged = self.system.read(record_id) is not None
        deleted = self.user.delete(record_id)
        if packaged:
            self.user.hide(record_id)
            return True
        return deleted


class _LayeredModelPairJournal(DurableModelPairJournal):
    """Rollback restores user overrides, never materializes package defaults."""
    def __init__(self, root, *, user_records, **kwargs):
        super().__init__(root, **kwargs)
        self._user_records = user_records

    def begin(self, *, provider_id, capability_id, **kwargs):
        kwargs['provider'] = self._user_records.read_for_replace_validation(provider_id)
        kwargs['capability'] = self._user_records.read_for_replace_validation(capability_id)
        return super().begin(provider_id=provider_id, capability_id=capability_id, **kwargs)

    def recover(self, records):
        return super().recover(self._user_records)

@dataclass(frozen=True, slots=True)
class ModelConfig:
    id: str
    provider: str
    model: str
    base_url: str = ""
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", validate_record_id(self.id))
        for name in ("provider", "model"):
            value = str(getattr(self, name)).strip()
            if not value:
                raise ValueError(f"{name} must not be empty")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "base_url", str(self.base_url).strip())
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


class YAMLModelStore:
    def __init__(
        self,
        root: Path,
        *,
        capability_change_handler: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._records = _LayeredModelRecordDirectory(root)
        self._capability_change_handler = capability_change_handler
        self._capability_revision = 0
        self._transaction_lock = ModelRecordFileLock(self._records.root)
        self._transaction_journal = _LayeredModelPairJournal(
            self._records.root, user_records=self._records.user,
            boundary=lambda name: _durability_boundary(name),
            validate_snapshot=_validate_transaction_snapshot,
        )

    @property
    def capability_revision(self) -> int:
        return self._capability_revision

    async def open(self) -> None:
        await self._records.open()
        with self._transaction_lock.exclusive():
            self._recover_consistent_state_locked()

    async def close(self) -> None:
        await self._records.close()

    async def get(self, model_id: str) -> ModelConfig | None:
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                payload = self._records.read(model_id)
                return None if payload is None else _decode(payload)

    async def list(self) -> list[ModelConfig]:
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                return [
                    _decode(self._records.read(item) or {})
                    for item in self._records.list_ids()
                ]

    async def packaged_record_ids(self) -> frozenset[str]:
        """The effective records the package layer declares.

        A user record of the same ID shadows the packaged one, and a hidden ID
        is removed even though the package still ships it, so this answers
        "would deleting it only write a removal marker?".
        """

        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                return frozenset(
                    item
                    for item in self._records.list_ids()
                    if self._records.is_packaged(item)
                )

    async def record_origin(self, record_id: str) -> str:
        """Return ``"system"`` for packaged records and ``"user"`` otherwise."""

        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                selected = validate_record_id(record_id)
                return "system" if self._records.is_packaged(selected) else "user"

    async def list_providers(self) -> list[ProviderConfig]:
        """Return credential providers without standalone Model records."""

        return [
            _provider_config(item)
            for item in await self.list()
            if item.model == "credential"
            and str(item.parameters.get("record_kind") or "")
            in {"provider_credential", "custom_provider"}
        ]

    async def get_provider(self, provider_id: str) -> ProviderConfig | None:
        record = await self.get(_provider_record_id(str(provider_id).strip()))
        if record is None or record.model != "credential":
            return None
        return _provider_config(record)

    async def cache_provider_model_profiles(
        self, provider_id: str, profiles: Mapping[str, object], *, base_url: str
    ) -> None:
        """Replace observed limits only if discovery still matches this provider.

        Persist using the existing secured record/transaction boundary so new
        Workers share the cache without making HTTP requests during a Run.
        """
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                record_id = _provider_record_id(provider_id)
                payload = self._records.read(record_id)
                if payload is None:
                    return
                record = _decode(payload)
                if record.base_url.rstrip("/") != base_url.rstrip("/"):
                    return
                parameters = dict(record.parameters)
                parameters["observed_model_profiles"] = {
                    "base_url": base_url.rstrip("/"),
                    "observed_at": time.time(),
                    "models": dict(profiles),
                }
                self._records.replace(record_id, _encode(replace(record, parameters=parameters)))
        # Tool/subagent model snapshots are generation-owned. Publish a new
        # revision after persistence; existing leased generations remain valid.
        await self._publish_capability_change()

    async def upsert_provider(self, provider: ProviderConfig) -> ProviderConfig:
        existing = await self.get_provider(provider.id)
        if existing is None:
            return await self.create_provider(provider)
        await self.update(self._provider_record(provider))
        return provider

    async def create_provider(self, provider: ProviderConfig) -> ProviderConfig:
        """Create one provider credential without replacement semantics."""

        await self.create(self._provider_record(provider))
        return provider

    @staticmethod
    def _provider_record(provider: ProviderConfig) -> ModelConfig:
        record_id = _provider_record_id(provider.id)
        parameters = dict(provider.parameters)
        parameters.update(
            {
                "record_kind": "provider_credential",
                "name": provider.name,
                "protocol": provider.protocol,
                "api_key": provider.api_key,
            }
        )
        record = ModelConfig(
            id=record_id,
            provider=provider.id,
            model="credential",
            base_url=provider.base_url,
            parameters=parameters,
        )
        return record

    async def delete_provider(self, provider_id: str) -> bool:
        record_id = _provider_record_id(str(provider_id).strip())
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                deleted = self._records.delete(record_id)
        if deleted:
            await self._publish_capability_change()
        return deleted

    async def create(self, config: ModelConfig) -> ModelConfig:
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                self._validate_references(config)
                self._records.create(config.id, _encode(config))
        await self._publish_capability_change()
        return config

    async def update(self, config: ModelConfig) -> ModelConfig:
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                self._validate_references(config)
                self._records.replace(config.id, _encode(config))
        await self._publish_capability_change()
        return config

    async def delete(self, model_id: str) -> bool:
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                record_id = validate_record_id(model_id)
                if self._records.read(record_id) is None:
                    return False
                for capability_id in self._records.list_ids():
                    payload = self._records.read(capability_id)
                    if payload is None:
                        continue
                    capability = _decode(payload)
                    if (
                        (_is_capability_id(capability.id)
                        or self._is_plain_model_resource(capability))
                        and _provider_record_id(capability.provider) == record_id
                    ):
                        raise ModelRecordIntegrityError(
                            "Provider record is referenced by Model records: "
                            f"{record_id}, {capability.id}"
                        )
                deleted = self._records.delete(record_id)
        if deleted:
            await self._publish_capability_change()
        return deleted

    async def delete_capability_binding(self, capability: str) -> bool:
        """Delete a binding and any journal-owned Provider as one transaction."""

        selected_capability = str(capability).strip().lower()
        capability_id = validate_record_id(f"capability-{selected_capability}")
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                capability_snapshot = self._records.read(capability_id)
                if capability_snapshot is None:
                    return False
                binding = _decode(capability_snapshot)
                provider_id = _provider_record_id(binding.provider)
                provider_snapshot = self._records.read(provider_id)
                provider = (
                    _decode(provider_snapshot)
                    if provider_snapshot is not None
                    else None
                )
                owns_provider = bool(
                    provider is not None
                    and provider.parameters.get("record_kind")
                    == _PROJECT_PROVIDER_KIND
                )
                if owns_provider:
                    assert provider is not None
                    _validate_project_provider_owner(
                        provider,
                        selected_capability,
                        capability_id,
                    )
                    self._ensure_owned_provider_exclusive_locked(
                        provider.id,
                        capability_id,
                    )
                    rollback_error: ModelRecordRollbackError | None = None
                    try:
                        self._transaction_journal.begin(
                            provider_id=provider_id,
                            capability_id=capability_id,
                            provider=provider_snapshot,
                            capability=capability_snapshot,
                        )
                        self._records.delete(capability_id)
                        self._records.delete(provider_id)
                        self._transaction_journal.commit()
                    except BaseException:
                        rollback_error = self._recover_after_write_failure()
                        if rollback_error is None:
                            raise
                    if rollback_error is not None:
                        raise rollback_error from None
                else:
                    self._records.delete(capability_id)
        await self._publish_capability_change()
        return True

    async def patch_provider_resource(
        self,
        record_id: str,
        *,
        fields: frozenset[str],
        label: str | None,
        base_url: str | None,
        api_key: str | None,
        parameters: Mapping[str, object] | None,
    ) -> ModelConfig:
        """Apply one Provider PATCH while holding the resource lock."""

        selected_id = validate_record_id(record_id)
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                current = self._provider_resource_locked(selected_id)
                selected_parameters = dict(current.parameters)
                if "parameters" in fields:
                    supplied = dict(parameters or {})
                    protected = {
                        key: value
                        for key, value in selected_parameters.items()
                        if key
                        in {
                            "api_key",
                            "capability",
                            "custom_id",
                            "name",
                            "owner",
                            "record_kind",
                        }
                    }
                    selected_parameters = {**supplied, **protected}
                if "label" in fields:
                    selected_parameters["name"] = str(label or "")
                if "api_key" in fields:
                    selected_parameters["api_key"] = str(api_key or "")
                updated = ModelConfig(
                    id=current.id,
                    provider=current.provider,
                    model=current.model,
                    base_url=(
                        str(base_url or "")
                        if "base_url" in fields
                        else current.base_url
                    ),
                    parameters=selected_parameters,
                )
                if updated != current:
                    self._validate_references(updated)
                    if (
                        current.parameters.get("record_kind")
                        == _PROJECT_PROVIDER_KIND
                    ):
                        capability_id = str(current.parameters.get("owner") or "")
                        capability_snapshot = self._records.read(capability_id)
                        if capability_snapshot is None:
                            raise ModelRecordIntegrityError(
                                "Project Provider binding record missing: "
                                f"{current.id}"
                            )
                        provider_snapshot = self._records.read(current.id)
                        rollback_error: ModelRecordRollbackError | None = None
                        try:
                            self._transaction_journal.begin(
                                provider_id=current.id,
                                capability_id=capability_id,
                                provider=provider_snapshot,
                                capability=capability_snapshot,
                            )
                            self._records.replace(updated.id, _encode(updated))
                            self._records.replace(
                                capability_id,
                                capability_snapshot,
                            )
                            self._transaction_journal.commit()
                        except BaseException:
                            rollback_error = self._recover_after_write_failure()
                            if rollback_error is None:
                                raise
                        if rollback_error is not None:
                            raise rollback_error from None
                    else:
                        self._records.replace(updated.id, _encode(updated))
        await self._publish_capability_change()
        return updated

    async def create_model_resource(self, config: ModelConfig) -> ModelConfig:
        """Create a Model only while its Provider reference is locked."""

        # D-8：本路径不走 _validate_references，base_url 校验须单独挂上 ——
        # 否则经 POST /models 建带 base_url 的记录就绕过了白名单。
        from bianbu_agent.security.security_provider_policy import enforce_provider_host

        enforce_provider_host(config.base_url)
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                self._validate_plain_model_identity(config)
                self._provider_for_reference_locked(config.provider)
                self._records.create(config.id, _encode(config))
        await self._publish_capability_change()
        return config

    async def patch_model_resource(
        self,
        model_id: str,
        *,
        fields: frozenset[str],
        provider: str | None,
        model: str | None,
        parameters: Mapping[str, object] | None,
    ) -> ModelConfig:
        """Apply one Model PATCH with reference validation under the lock."""

        selected_id = validate_record_id(model_id)
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                current = self._plain_model_resource_locked(selected_id)
                selected_provider = (
                    str(provider or "")
                    if "provider_id" in fields
                    else current.provider
                )
                selected_model = (
                    str(model or "") if "model" in fields else current.model
                )
                selected_parameters = (
                    dict(parameters or {})
                    if "parameters" in fields
                    else dict(current.parameters)
                )
                if {"provider_id", "model"} & fields:
                    references = self._model_bindings_locked(current.id)
                    if references:
                        raise ModelRecordIntegrityError(
                            "Model is referenced by capability binding: "
                            f"{current.id}, {references[0]}"
                        )
                self._provider_for_reference_locked(selected_provider)
                updated = ModelConfig(
                    id=current.id,
                    provider=selected_provider,
                    model=selected_model,
                    parameters=selected_parameters,
                )
                self._records.replace(updated.id, _encode(updated))
        await self._publish_capability_change()
        return updated

    async def delete_model_resource(self, model_id: str) -> bool:
        """Delete a Model only if no binding references it under the same lock."""

        selected_id = validate_record_id(model_id)
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                current = self._plain_model_resource_locked(selected_id)
                references = self._model_bindings_locked(current.id)
                if references:
                    raise ModelRecordIntegrityError(
                        "Model is referenced by capability binding: "
                        f"{current.id}, {references[0]}"
                    )
                deleted = self._records.delete(current.id)
        if deleted:
            await self._publish_capability_change()
        return deleted

    async def delete_provider_resource(self, record_id: str) -> bool:
        """Delete a Provider only if no Model or binding references it."""

        selected_id = validate_record_id(record_id)
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                provider = self._provider_resource_locked(selected_id)
                reference = next(
                    (
                        item.id
                        for item in self._configs_locked()
                        if (
                            self._is_plain_model_resource(item)
                            or _is_capability_id(item.id)
                        )
                        and item.provider == provider.provider
                    ),
                    None,
                )
                if reference is not None:
                    raise ModelRecordIntegrityError(
                        f"Provider is referenced by resource: {provider.id}, {reference}"
                    )
                deleted = self._records.delete(provider.id)
        if deleted:
            await self._publish_capability_change()
        return deleted

    async def upsert_capability_binding(
        self,
        *,
        capability: str,
        model_id: str,
        options: Mapping[str, object],
    ) -> ModelConfig:
        """Resolve and write a binding as one lock-held decision."""

        selected_capability = str(capability).strip().lower()
        capability_id = validate_record_id(f"capability-{selected_capability}")
        selected_model_id = validate_record_id(model_id)
        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                model = self._plain_model_resource_locked(selected_model_id)
                provider = self._provider_for_reference_locked(model.provider)
                _validate_complete_provider(provider, capability_id)
                config = ModelConfig(
                    id=capability_id,
                    provider=model.provider,
                    model=model.model,
                    parameters={
                        **dict(options),
                        "capability": selected_capability,
                        "model_id": model.id,
                    },
                )
                existing_snapshot = self._records.read(capability_id)
                existing = (
                    _decode(existing_snapshot)
                    if existing_snapshot is not None
                    else None
                )
                owned_provider_id = (
                    _provider_record_id(existing.provider)
                    if existing is not None
                    else ""
                )
                owned_provider_snapshot = (
                    self._records.read(owned_provider_id)
                    if owned_provider_id
                    else None
                )
                owned_provider = (
                    _decode(owned_provider_snapshot)
                    if owned_provider_snapshot is not None
                    else None
                )
                owns_provider = bool(
                    owned_provider is not None
                    and owned_provider.parameters.get("record_kind")
                    == _PROJECT_PROVIDER_KIND
                )
                if owns_provider:
                    assert owned_provider is not None
                    _validate_project_provider_owner(
                        owned_provider,
                        selected_capability,
                        capability_id,
                    )
                    self._ensure_owned_provider_exclusive_locked(
                        owned_provider.id,
                        capability_id,
                    )
                    rollback_error: ModelRecordRollbackError | None = None
                    try:
                        self._transaction_journal.begin(
                            provider_id=owned_provider_id,
                            capability_id=capability_id,
                            provider=owned_provider_snapshot,
                            capability=existing_snapshot,
                        )
                        self._records.replace(capability_id, _encode(config))
                        self._records.delete(owned_provider_id)
                        self._transaction_journal.commit()
                    except BaseException:
                        rollback_error = self._recover_after_write_failure()
                        if rollback_error is None:
                            raise
                    if rollback_error is not None:
                        raise rollback_error from None
                elif existing is None:
                    self._records.create(capability_id, _encode(config))
                else:
                    self._records.replace(capability_id, _encode(config))
        await self._publish_capability_change()
        return config

    async def create_project_capability(
        self,
        *,
        capability: str,
        model: str,
        base_url: str,
        api_key: str,
        voice: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> tuple[ModelConfig, ModelConfig]:
        """Durably create a project-owned pair without replacing records.

        A private rollback journal is fsynced before either record changes. A
        restarted Store or Resolver restores the absent old pair after abrupt
        process death, while the root-scoped lock excludes concurrent readers.
        Supplied capability options are committed in the same transaction.
        """

        (
            selected_capability,
            capability_id,
            provider_id,
            selected_model,
        ) = _project_capability_identity(capability, model)
        selected_base_url = str(base_url).strip()
        if not selected_base_url:
            raise ValueError("base_url must not be empty")
        selected_api_key = str(api_key).strip()
        if not selected_api_key:
            raise ValueError("api_key must not be empty")
        provider, capability_record = _build_project_capability_records(
            selected_capability=selected_capability,
            capability_id=capability_id,
            provider_id=provider_id,
            selected_model=selected_model,
            selected_base_url=selected_base_url,
            selected_api_key=selected_api_key,
            provider_name=f"{selected_capability.upper()} Provider",
            capability_parameters=dict(options or {}),
            voice=voice,
        )

        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                for record_id in (provider_id, capability_id):
                    if self._records.read_for_replace_validation(record_id) is not None:
                        raise ValueError(f"record already exists: {record_id}")

                rollback_error: ModelRecordRollbackError | None = None
                try:
                    self._transaction_journal.begin(
                        provider_id=provider_id,
                        capability_id=capability_id,
                        provider=None,
                        capability=None,
                    )
                    self._records.create(provider.id, _encode(provider))
                    self._records.create(capability_record.id, _encode(capability_record))
                    self._transaction_journal.commit()
                except BaseException:
                    rollback_error = self._recover_after_write_failure()
                    if rollback_error is None:
                        raise
                if rollback_error is not None:
                    raise rollback_error from None
        await self._publish_capability_change()
        return provider, capability_record

    async def upsert_project_capability(
        self,
        *,
        capability: str,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        voice: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> tuple[ModelConfig, ModelConfig]:
        """Durably update a capability and its private provider record.

        Both records are prepared before a private rollback journal is fsynced.
        Recovery is idempotent, so readers observe the complete old pair until
        the journal is removed after both new records and directories are durable.
        When supplied, capability options replace the old options atomically.
        """

        (
            selected_capability,
            capability_id,
            provider_id,
            selected_model,
        ) = _project_capability_identity(capability, model)

        async with self._records.lock:
            with self._transaction_lock.exclusive():
                self._recover_consistent_state_locked()
                result = self._upsert_project_capability_locked(
                    selected_capability=selected_capability,
                    capability_id=capability_id,
                    provider_id=provider_id,
                    selected_model=selected_model,
                    base_url=base_url,
                    api_key=api_key,
                    voice=voice,
                    options=options,
                )
        await self._publish_capability_change()
        return result

    async def _publish_capability_change(self) -> None:
        self._capability_revision += 1
        if self._capability_change_handler is not None:
            await self._capability_change_handler()

    def _upsert_project_capability_locked(
        self,
        *,
        selected_capability: str,
        capability_id: str,
        provider_id: str,
        selected_model: str,
        base_url: str | None,
        api_key: str | None,
        voice: str | None,
        options: Mapping[str, object] | None,
    ) -> tuple[ModelConfig, ModelConfig]:
        provider_snapshot = self._records.read(provider_id)
        capability_snapshot = self._records.read(capability_id)
        existing_provider = (
            _decode(provider_snapshot) if provider_snapshot is not None else None
        )
        existing_capability = (
            _decode(capability_snapshot)
            if capability_snapshot is not None
            else None
        )
        references = [
            item.id
            for item in self._capabilities()
            if _provider_record_id(item.provider) == provider_id
        ]
        other_references = [
            item for item in references if item != capability_id
        ]
        if other_references:
            raise ModelRecordIntegrityError(
                "Project provider record is shared by another capability: "
                f"{provider_id}, {other_references[0]}"
            )

        if existing_provider is not None:
            record_kind = str(
                existing_provider.parameters.get("record_kind") or ""
            )
            if record_kind == _PROJECT_PROVIDER_KIND:
                _validate_project_provider_owner(
                    existing_provider,
                    selected_capability,
                    capability_id,
                )
            elif self._records.user.read_for_replace_validation(provider_id) is not None:
                # A read-only packaged record may be shadowed by this pair; a
                # private user record must never be adopted without a claim.
                raise ModelRecordIntegrityError(
                    "Project provider record collides with a user-owned "
                    f"record: {provider_id}"
                )

        source_provider: ModelConfig | None = None
        if existing_capability is not None:
            source_id = _provider_record_id(existing_capability.provider)
            source_payload = self._records.read(source_id)
            if source_payload is None:
                raise ModelRecordIntegrityError(
                    "Provider credential record missing: "
                    f"{source_id}, {capability_id}"
                )
            source_provider = _decode(source_payload)
            _validate_complete_provider(source_provider, capability_id)
        elif existing_provider is not None:
            source_provider = existing_provider

        selected_base_url = str(
            base_url
            if base_url is not None
            else (source_provider.base_url if source_provider else "")
        ).strip()
        if not selected_base_url:
            raise ValueError("base_url must not be empty")
        selected_api_key = str(
            api_key
            if api_key is not None
            else (
                source_provider.parameters.get("api_key", "")
                if source_provider
                else ""
            )
        ).strip()
        if not selected_api_key:
            raise ValueError("api_key must not be empty")

        provider_name = str(
            existing_provider.parameters.get("name")
            if existing_provider is not None
            else ""
        ).strip() or f"{selected_capability.upper()} Provider"
        capability_parameters = (
            dict(options)
            if options is not None
            else (
                dict(existing_capability.parameters)
                if existing_capability is not None
                else {}
            )
        )
        provider, capability_record = _build_project_capability_records(
            selected_capability=selected_capability,
            capability_id=capability_id,
            provider_id=provider_id,
            selected_model=selected_model,
            selected_base_url=selected_base_url,
            selected_api_key=selected_api_key,
            provider_name=provider_name,
            capability_parameters=capability_parameters,
            voice=voice,
        )

        rollback_error: ModelRecordRollbackError | None = None
        try:
            self._transaction_journal.begin(
                provider_id=provider_id,
                capability_id=capability_id,
                provider=provider_snapshot,
                capability=capability_snapshot,
            )
            self._write_upsert(provider, provider_snapshot)
            self._write_upsert(capability_record, capability_snapshot)
            self._transaction_journal.commit()
        except BaseException:
            rollback_error = self._recover_after_write_failure()
            if rollback_error is None:
                raise
        if rollback_error is not None:
            raise rollback_error from None
        return provider, capability_record

    def _write_upsert(
        self,
        config: ModelConfig,
        snapshot: dict[str, object] | None,
    ) -> None:
        if snapshot is None:
            self._records.create(config.id, _encode(config))
        else:
            self._records.replace(config.id, _encode(config))

    def _recover_consistent_state_locked(self) -> None:
        recovery_records = _SecureModelRecordDirectory(self._records.root)
        self._transaction_journal.recover(recovery_records)
        self._remove_owned_provider_orphans_locked()

    def _recover_after_write_failure(self) -> ModelRecordRollbackError | None:
        try:
            self._transaction_journal.recover(self._records)
        except ModelRecordRollbackError as rollback_error:
            return ModelRecordRollbackError(rollback_error.record_ids)
        return None

    def _remove_owned_provider_orphans_locked(self) -> None:
        for provider_id in self._records.user.list_ids():
            if not provider_id.startswith("custom-provider-"):
                continue
            payload = self._records.read_for_replace_validation(provider_id)
            if payload is None:
                continue
            provider = _decode(payload)
            if provider.parameters.get("record_kind") != _PROJECT_PROVIDER_KIND:
                continue
            capability = str(
                provider.parameters.get("capability") or ""
            ).strip().lower()
            owner = str(provider.parameters.get("owner") or "").strip()
            try:
                _validate_project_provider_owner(provider, capability, owner)
            except ModelRecordIntegrityError:
                continue
            if self._records.read_for_replace_validation(owner) is None:
                self._records.delete(provider_id)

    def _read_config_locked(self, record_id: str) -> ModelConfig | None:
        payload = self._records.read(record_id)
        return None if payload is None else _decode(payload)

    def _configs_locked(self) -> list[ModelConfig]:
        return [
            _decode(self._records.read(record_id) or {})
            for record_id in self._records.list_ids()
        ]

    def _provider_resource_locked(self, record_id: str) -> ModelConfig:
        provider = self._read_config_locked(record_id)
        if provider is None or not _is_provider_resource(provider):
            raise KeyError(record_id)
        return provider

    def _provider_for_reference_locked(self, provider: str) -> ModelConfig:
        record_id = _provider_record_id(provider)
        try:
            selected = self._provider_resource_locked(record_id)
        except KeyError:
            raise KeyError(provider) from None
        if selected.parameters.get("record_kind") == _PROJECT_PROVIDER_KIND:
            raise ModelRecordIntegrityError(
                "Transaction-owned Provider cannot be selected by a Model: "
                f"{selected.id}"
            )
        return selected

    def _plain_model_resource_locked(self, model_id: str) -> ModelConfig:
        model = self._read_config_locked(model_id)
        if model is None or not self._is_plain_model_resource(model):
            raise KeyError(model_id)
        return model

    @staticmethod
    def _is_plain_model_resource(config: ModelConfig) -> bool:
        return config.model != "credential" and not config.id.startswith(
            _MODEL_RESOURCE_RESERVED_PREFIXES
        )

    def _validate_plain_model_identity(self, config: ModelConfig) -> None:
        if not self._is_plain_model_resource(config):
            raise ValueError(f"reserved Model id: {config.id}")

    def _model_bindings_locked(self, model_id: str) -> list[str]:
        return [
            config.id
            for config in self._configs_locked()
            if _is_capability_id(config.id)
            and config.parameters.get("model_id") == model_id
        ]

    def _ensure_owned_provider_exclusive_locked(
        self,
        provider_id: str,
        owner_id: str,
    ) -> None:
        reference = next(
            (
                config.id
                for config in self._configs_locked()
                if config.id != owner_id
                and (
                    self._is_plain_model_resource(config)
                    or _is_capability_id(config.id)
                )
                and _provider_record_id(config.provider) == provider_id
            ),
            None,
        )
        if reference is not None:
            raise ModelRecordIntegrityError(
                "Transaction-owned Provider is referenced by another resource: "
                f"{provider_id}, {reference}"
            )

    def _validate_references(self, config: ModelConfig) -> None:
        # D-8：base_url 决定全部对话内容发往哪台服务器，此前无任何校验 ——
        # 一次 API 调用就能把所有会话导向攻击者的服务器且不留痕迹。
        # 校验放在这里（而非 API 层）是因为它是 create/update/patch 三条
        # 写路径的共同必经点，直接调 store 的调用方同样挡得住。
        # 未配置 BIANBU_ALLOWED_PROVIDER_HOSTS 时不限制（本地部署常连内网）。
        from bianbu_agent.security.security_provider_policy import enforce_provider_host

        enforce_provider_host(config.base_url)

        if _is_capability_id(config.id):
            if config.parameters.get("record_kind"):
                raise ModelRecordIntegrityError(
                    f"reserved capability record kind invalid: {config.id}"
                )
            provider_id = _provider_record_id(config.provider)
            payload = self._records.read(provider_id)
            if payload is None:
                raise ModelRecordIntegrityError(
                    "Provider credential record missing: "
                    f"{provider_id}, {config.id}"
                )
            _validate_complete_provider(_decode(payload), config.id)
            return

        existing_payload = self._records.read_for_replace_validation(config.id)
        existing_kind = (
            _decode(existing_payload).parameters.get("record_kind")
            if existing_payload is not None
            else None
        )
        record_kind = config.parameters.get("record_kind")
        if (
            existing_kind == _PROJECT_PROVIDER_KIND
            and record_kind != _PROJECT_PROVIDER_KIND
        ):
            raise ModelRecordIntegrityError(
                "Project provider ownership metadata cannot be removed: "
                f"{config.id}"
            )
        references: list[str] = []
        for record_id in self._records.list_ids():
            if record_id.startswith(("provider-", "custom-provider-")):
                continue
            payload = self._records.read(record_id)
            if payload is None:
                continue
            model = _decode(payload)
            if (
                _is_capability_id(model.id)
                or self._is_plain_model_resource(model)
            ) and _provider_record_id(model.provider) == config.id:
                references.append(model.id)
        references.sort()
        if record_kind == _PROJECT_PROVIDER_KIND:
            selected_capability = str(
                config.parameters.get("capability") or ""
            ).strip()
            owner = str(config.parameters.get("owner") or "").strip()
            _validate_project_provider_owner(
                config,
                selected_capability,
                owner,
            )
            invalid_reference = next(
                (reference for reference in references if reference != owner),
                None,
            )
            if owner not in references:
                raise ModelRecordIntegrityError(
                    "Project provider owner does not reference record: "
                    f"{config.id}, {owner}"
                )
            if invalid_reference is not None:
                raise ModelRecordIntegrityError(
                    "Project provider record is shared by another capability: "
                    f"{config.id}, {invalid_reference}"
                )
        referenced_by = next(iter(references), None)
        if referenced_by is not None:
            _validate_complete_provider(config, referenced_by)

    def _capabilities(self) -> list[ModelConfig]:
        result = []
        for record_id in self._records.list_ids():
            if not _is_capability_id(record_id):
                continue
            payload = self._records.read(record_id)
            if payload is None:
                continue
            config = _decode(payload)
            if _is_capability_id(config.id):
                result.append(config)
        return result

def _project_capability_identity(
    capability: str,
    model: str,
) -> tuple[str, str, str, str]:
    selected_capability = str(capability).strip().lower()
    if not selected_capability:
        raise ValueError("capability must not be empty")
    capability_id = validate_record_id(f"capability-{selected_capability}")
    provider_id = validate_record_id(f"custom-provider-{selected_capability}")
    selected_model = str(model).strip()
    if not selected_model:
        raise ValueError("model must not be empty")
    return selected_capability, capability_id, provider_id, selected_model


def _build_project_capability_records(
    *,
    selected_capability: str,
    capability_id: str,
    provider_id: str,
    selected_model: str,
    selected_base_url: str,
    selected_api_key: str,
    provider_name: str,
    capability_parameters: Mapping[str, object],
    voice: str | None,
) -> tuple[ModelConfig, ModelConfig]:
    provider = ModelConfig(
        id=provider_id,
        provider=f"custom:{selected_capability}",
        model="credential",
        base_url=selected_base_url,
        parameters={
            "record_kind": _PROJECT_PROVIDER_KIND,
            "capability": selected_capability,
            "owner": capability_id,
            "name": provider_name,
            "api_key": selected_api_key,
        },
    )
    selected_parameters = dict(capability_parameters)
    selected_parameters.pop("api_key", None)
    selected_parameters.pop("record_kind", None)
    selected_parameters["capability"] = selected_capability
    if voice is not None:
        selected_parameters["voice"] = str(voice)
    capability_record = ModelConfig(
        id=capability_id,
        provider=f"custom:{selected_capability}",
        model=selected_model,
        parameters=selected_parameters,
    )
    _validate_project_provider_owner(
        provider,
        selected_capability,
        capability_id,
    )
    _validate_complete_provider(provider, capability_id)
    return provider, capability_record


def _encode(config: ModelConfig) -> dict[str, object]:
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "parameters": dict(config.parameters),
    }


def _decode(payload: dict[str, object]) -> ModelConfig:
    return ModelConfig(
        id=str(payload["id"]),
        provider=str(payload["provider"]),
        model=str(payload["model"]),
        base_url=str(payload.get("base_url", "")),
        parameters=payload.get("parameters", {}),
    )


def _provider_record_id(provider: str) -> str:
    normalized = str(provider).strip()
    if normalized.startswith("custom:"):
        return f"custom-provider-{normalized.split(':', 1)[1]}"
    return f"provider-{normalized}"


def _provider_config(config: ModelConfig) -> ProviderConfig:
    parameters = dict(config.parameters)
    return ProviderConfig(
        id=config.provider,
        name=str(parameters.pop("name", "") or config.provider),
        protocol=str(parameters.pop("protocol", "") or "openai-compatible"),
        base_url=config.base_url,
        api_key=parameters.pop("api_key", ""),
        parameters={
            key: value
            for key, value in parameters.items()
            if key not in {"record_kind", "custom_id"}
        },
    )


def _is_capability_id(record_id: str) -> bool:
    return record_id.startswith("capability-")


_PROJECT_PROVIDER_KIND = "project_capability_provider"
_PROVIDER_RESOURCE_KINDS = frozenset(
    {"provider_credential", "custom_provider", _PROJECT_PROVIDER_KIND}
)
_MODEL_RESOURCE_RESERVED_PREFIXES = (
    "capability-",
    "credential-",
    "custom-provider-",
    "option-",
    "provider-",
)


def _is_provider_resource(config: ModelConfig) -> bool:
    return bool(
        config.model == "credential"
        and config.parameters.get("record_kind") in _PROVIDER_RESOURCE_KINDS
    )


def _validate_project_provider_owner(
    provider: ModelConfig,
    capability: str,
    capability_id: str,
) -> None:
    expected_capability = str(capability).strip().lower()
    expected_owner = f"capability-{expected_capability}"
    expected_provider_id = f"custom-provider-{expected_capability}"
    expected_provider = f"custom:{expected_capability}"
    parameters = provider.parameters
    if (
        parameters.get("record_kind") != _PROJECT_PROVIDER_KIND
        or str(parameters.get("capability") or "").strip().lower()
        != expected_capability
        or str(parameters.get("owner") or "").strip() != expected_owner
        or capability_id != expected_owner
        or provider.id != expected_provider_id
        or provider.provider != expected_provider
        or provider.model != "credential"
    ):
        raise ModelRecordIntegrityError(
            f"Project provider ownership metadata invalid: {provider.id}"
        )


def _validate_complete_provider(provider: ModelConfig, capability_id: str) -> None:
    provider_id = provider.id
    base_url, api_key = normalize_provider_configuration(
        provider.base_url,
        provider.parameters.get("api_key"),
    )
    if not api_key:
        raise ModelRecordIntegrityError(
            f"Provider API key missing: {provider_id}, {capability_id}"
        )
    if not base_url:
        raise ModelRecordIntegrityError(
            f"Provider Base URL missing: {provider_id}, {capability_id}"
        )


def _validate_transaction_snapshot(
    record_id: str,
    payload: dict[str, object],
) -> None:
    try:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError
        if payload.get("id") != record_id:
            raise ValueError
        if _decode(payload).id != record_id:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("invalid Model transaction snapshot") from None


def _path_component(value: object, *, field_name: str) -> str:
    raw = str(value or "").strip().lower().replace(":", "-")
    normalized = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-_")
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"{field_name} cannot be used as a file name")
    return normalized[:128]


__all__ = ["YAMLModelStore", "ModelConfig"]
