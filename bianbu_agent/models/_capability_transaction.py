"""Durable rollback journal for two-record project capability mutations."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bianbu_agent._internal.json_records import SCHEMA_VERSION, validate_record_id
from bianbu_agent.errors import ModelRecordRollbackError, ModelResolutionError


_JOURNAL_FILE_NAME = ".model-records.transaction"
_JOURNAL_KIND = "project_capability_pair"
_JOURNAL_TEMPORARY_PREFIX = ".model-transaction-"
_RECORD_TEMPORARY_PREFIX = ".record-"


class _RecordDirectory(Protocol):
    def read_for_replace_validation(
        self,
        record_id: object,
    ) -> dict[str, object] | None: ...

    def create(self, record_id: object, payload: dict[str, object]) -> None: ...

    def replace(self, record_id: object, payload: dict[str, object]) -> None: ...

    def delete(self, record_id: object) -> bool: ...


@dataclass(frozen=True, slots=True)
class _ModelPairSnapshot:
    provider_id: str
    capability_id: str
    provider: dict[str, object] | None
    capability: dict[str, object] | None


class DurableModelPairJournal:
    """Persist old pair snapshots before mutating either public record."""

    def __init__(
        self,
        root: Path,
        *,
        boundary: Callable[[str], None],
        validate_snapshot: Callable[[str, dict[str, object]], None],
    ) -> None:
        self._root = root
        self._path = root / _JOURNAL_FILE_NAME
        self._boundary = boundary
        self._validate_snapshot = validate_snapshot

    def begin(
        self,
        *,
        provider_id: str,
        capability_id: str,
        provider: dict[str, object] | None,
        capability: dict[str, object] | None,
    ) -> None:
        if self.read() is not None:
            raise ModelResolutionError("pending Model transaction journal")
        document = {
            "schema_version": SCHEMA_VERSION,
            "kind": _JOURNAL_KIND,
            "provider_id": provider_id,
            "capability_id": capability_id,
            "before": {
                "provider": provider,
                "capability": capability,
            },
        }
        try:
            content = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError):
            raise ModelResolutionError("invalid Model transaction journal") from None

        descriptor, name = tempfile.mkstemp(
            prefix=_JOURNAL_TEMPORARY_PREFIX,
            suffix=".tmp",
            dir=self._root,
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                self._boundary("journal:temporary-write")
                stream.flush()
                os.fsync(stream.fileno())
                self._boundary("journal:temporary-fsync")
            try:
                os.link(temporary, self._path, follow_symlinks=False)
            except FileExistsError:
                raise ModelResolutionError("pending Model transaction journal") from None
            self._boundary("journal:publish-link")
            self._fsync_directory()
            self._boundary("journal:directory-fsync")
            temporary.unlink()
            self._boundary("journal:temporary-remove")
            self._fsync_directory()
            self._boundary("journal:temporary-remove-directory-fsync")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def read(self) -> _ModelPairSnapshot | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        try:
            descriptor = os.open(self._path, flags)
        except FileNotFoundError:
            return None
        except OSError:
            raise ModelResolutionError("unsafe Model transaction journal") from None
        try:
            entry = os.fstat(descriptor)
            if (
                not stat.S_ISREG(entry.st_mode)
                or stat.S_IMODE(entry.st_mode) != 0o600
            ):
                raise ModelResolutionError("unsafe Model transaction journal")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                try:
                    document = json.load(stream)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    raise ModelResolutionError(
                        "invalid Model transaction journal"
                    ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return self._decode(document)

    def recover(self, records: _RecordDirectory) -> None:
        snapshot = self.read()
        if snapshot is None:
            self._cleanup_temporaries()
            return

        failures: list[str] = []
        for record_id, previous in (
            (snapshot.capability_id, snapshot.capability),
            (snapshot.provider_id, snapshot.provider),
        ):
            try:
                self._restore(records, record_id, previous)
            except BaseException:
                failures.append(record_id)
        if failures:
            raise ModelRecordRollbackError(tuple(failures)) from None

        self.commit()
        self._cleanup_temporaries()

    def commit(self) -> None:
        if self.read() is None:
            return
        self._path.unlink()
        self._boundary("journal:remove")
        self._fsync_directory()
        self._boundary("journal:remove-directory-fsync")

    def _decode(self, document: object) -> _ModelPairSnapshot:
        try:
            if not isinstance(document, Mapping):
                raise ValueError
            if document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError
            if document.get("kind") != _JOURNAL_KIND:
                raise ValueError
            provider_id = validate_record_id(document["provider_id"])
            capability_id = validate_record_id(document["capability_id"])
            capability = capability_id.removeprefix("capability-")
            is_project_capability_pair = bool(capability) and (
                capability_id == f"capability-{capability}"
                and provider_id == f"custom-provider-{capability}"
            )
            if not is_project_capability_pair:
                raise ValueError
            before = document["before"]
            if not isinstance(before, Mapping):
                raise ValueError
            if set(before) != {"provider", "capability"}:
                raise ValueError
            provider = self._decode_snapshot(
                provider_id,
                before["provider"],
            )
            capability_record = self._decode_snapshot(
                capability_id,
                before["capability"],
            )
        except (KeyError, TypeError, ValueError, ModelResolutionError):
            raise ModelResolutionError("invalid Model transaction journal") from None
        return _ModelPairSnapshot(
            provider_id=provider_id,
            capability_id=capability_id,
            provider=provider,
            capability=capability_record,
        )

    def _decode_snapshot(
        self,
        record_id: str,
        value: object,
    ) -> dict[str, object] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError
        snapshot = dict(value)
        self._validate_snapshot(record_id, snapshot)
        return snapshot

    @staticmethod
    def _restore(
        records: _RecordDirectory,
        record_id: str,
        previous: dict[str, object] | None,
    ) -> None:
        current = records.read_for_replace_validation(record_id)
        if current == previous:
            return
        if previous is None:
            if current is not None:
                records.delete(record_id)
        elif current is None:
            records.create(record_id, previous)
        else:
            records.replace(record_id, previous)

    def _cleanup_temporaries(self) -> None:
        removed = False
        for path in self._root.iterdir():
            if not path.name.startswith(
                (_JOURNAL_TEMPORARY_PREFIX, _RECORD_TEMPORARY_PREFIX)
            ):
                continue
            entry = path.stat(follow_symlinks=False)
            if (
                not stat.S_ISREG(entry.st_mode)
                or stat.S_IMODE(entry.st_mode) != 0o600
            ):
                raise ModelResolutionError(
                    "unsafe Model transaction temporary"
                ) from None
            path.unlink()
            removed = True
        if removed:
            self._fsync_directory()

    def _fsync_directory(self) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
            os, "O_CLOEXEC", 0
        )
        descriptor = os.open(self._root, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["DurableModelPairJournal"]
