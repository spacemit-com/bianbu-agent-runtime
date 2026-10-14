"""Deterministic, atomic JSON records stored one file per stable ID."""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def validate_record_id(value: object) -> str:
    record_id = str(value).strip()
    if not record_id or ".." in record_id or _ID.fullmatch(record_id) is None:
        raise ValueError(f"invalid record id: {value!r}")
    return record_id


class JSONRecordDirectory:
    def __init__(self, root: Path) -> None:
        candidate = Path(root).expanduser()
        if candidate.is_symlink():
            raise ValueError(f"record directory must not be a symbolic link: {candidate}")
        self.root = candidate.resolve()
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError(f"unsafe record directory: {self.root}")

    async def close(self) -> None:
        return None

    def path(self, record_id: object) -> Path:
        return self.root / f"{validate_record_id(record_id)}.json"

    def list_ids(self) -> tuple[str, ...]:
        if not self.root.exists():
            return ()
        result: list[str] = []
        for path in self.root.iterdir():
            if path.suffix != ".json":
                continue
            entry = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
                raise ValueError(f"unsafe JSON record: {path.name}")
            result.append(validate_record_id(path.stem))
        return tuple(sorted(result, key=lambda item: (item.casefold(), item)))

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

    def read(self, record_id: object) -> dict[str, Any] | None:
        path = self.path(record_id)
        try:
            entry = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(entry.st_mode) or not stat.S_ISREG(entry.st_mode):
            raise ValueError(f"unsafe JSON record: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON record {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid JSON record {path.name}: object required")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version in {path.name}")
        if payload.get("id") != path.stem:
            raise ValueError(f"record id does not match filename: {path.name}")
        return payload

    def create(self, record_id: object, payload: dict[str, Any]) -> None:
        normalized = validate_record_id(record_id)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ensure_casefold_available(normalized)
        target = self.path(normalized)
        temporary = self._write_temporary(normalized, payload)
        try:
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise ValueError(f"record already exists: {normalized}") from exc
            self._fsync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def replace(self, record_id: object, payload: dict[str, Any]) -> None:
        normalized = validate_record_id(record_id)
        target = self.path(normalized)
        if self.read(normalized) is None:
            raise KeyError(normalized)
        temporary = self._write_temporary(normalized, payload)
        try:
            os.replace(temporary, target)
            self._fsync_directory()
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, record_id: object) -> bool:
        normalized = validate_record_id(record_id)
        target = self.path(normalized)
        if self.read(normalized) is None:
            return False
        target.unlink()
        self._fsync_directory()
        return True

    def _write_temporary(self, record_id: str, payload: dict[str, Any]) -> Path:
        document = dict(payload)
        document["id"] = record_id
        document["schema_version"] = SCHEMA_VERSION
        try:
            content = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise ValueError(f"record is not JSON serializable: {exc}") from exc
        descriptor, name = tempfile.mkstemp(prefix=".record-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary

    def _fsync_directory(self) -> None:
        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["JSONRecordDirectory", "SCHEMA_VERSION", "validate_record_id"]
