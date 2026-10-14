"""Provider-filtered, credential-sanitized LLM call capture."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from contextlib import contextmanager
import fcntl
import gzip
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
import httpx
from bianbu_agent.observability import bind_log_context


LOGGER = logging.getLogger(__name__)

_ACTIVE_CAPTURE_RUN: ContextVar[str | None] = ContextVar(
    "bianbu_provider_capture_run", default=None
)
_ACTIVE_CAPTURE_RUNS: dict[str, str] = {}
_ACTIVE_CAPTURE_RUNS_LOCK = threading.Lock()


def _active_capture_run(provider_id: str) -> str | None:
    run_id = _ACTIVE_CAPTURE_RUN.get()
    if run_id:
        return run_id
    with _ACTIVE_CAPTURE_RUNS_LOCK:
        return _ACTIVE_CAPTURE_RUNS.get(provider_id)


def _http_body(content: bytes) -> object:
    if not content:
        return ""
    text = content.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # OpenAI-compatible streaming responses are Server-Sent Events. Keep
        # each JSON data frame structured so the response remains inspectable
        # instead of being mistaken for a data URL by credential sanitization.
        frames: list[object] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                frames.append(json.loads(payload))
            except json.JSONDecodeError:
                frames.append(payload)
        return {"stream": frames} if frames else text


class ProviderCaptureHTTPHooks:
    """Capture the actual OpenAI-compatible HTTP exchange for one provider."""

    def __init__(self, store: "ProviderCaptureStore", provider_id: str) -> None:
        self.store = store
        self.provider_id = provider_id
        self._requests: dict[str, dict[str, object]] = {}
        self._lock = threading.Lock()

    def _begin(self, request: httpx.Request) -> None:
        run_id = _active_capture_run(self.provider_id)
        if not run_id:
            return
        record = {
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": _http_body(request.content),
        }
        with self._lock:
            self._requests[run_id] = {"request": record}

    def _finish(self, response: httpx.Response) -> None:
        run_id = _active_capture_run(self.provider_id)
        if not run_id:
            return
        try:
            content = response.content
        except httpx.ResponseNotRead:
            # Event hooks run before the caller consumes a streaming response.
            # Never read it here: doing so buffers the entire model response.
            content = b""
        except Exception:
            content = b""
        with self._lock:
            record = self._requests.get(run_id)
            if record is None:
                return
            record["response"] = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "body": _http_body(content),
            }

    def on_request(self, request: httpx.Request) -> None:
        try:
            self._begin(request)
        except Exception:
            LOGGER.exception("Provider capture HTTP request hook failed")

    def on_response(self, response: httpx.Response) -> None:
        try:
            self._finish(response)
        except Exception:
            LOGGER.exception("Provider capture HTTP response hook failed")

    async def on_request_async(self, request: httpx.Request) -> None:
        self.on_request(request)

    async def on_response_async(self, response: httpx.Response) -> None:
        # Do not consume streaming responses in the hook; the model client
        # must receive each chunk immediately.
        self.on_response(response)

    def pop(self, run_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._requests.pop(run_id, None)

PREVIEW_BYTES = 4 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_STORAGE_BYTES = 100 * 1024 * 1024
MAX_LIST_RECORDS = 100
MAX_RETAINED_RECORDS = 1_000
MAX_PREVIEW_INDEX_BYTES = 64 * 1024
SUMMARY_STRING_CHARS = 256
_MAX_JSON_DEPTH = 64
_MAX_JSON_INTEGER_DIGITS = 64
_MAX_JSON_INTEGER_ABS = 10**_MAX_JSON_INTEGER_DIGITS - 1
_MAX_MANIFEST_BYTES = 512 * 1024
_MAX_TARGET_BYTES = 4 * 1024
_MANIFEST_VERSION = 1

_RECORD_ID = re.compile(r"[0-9a-f]{32}\Z")
_SECRET_NAME = re.compile(
    r"(?:authorization|api[_-]?key|secret|credential|password|passwd|cookie|token)",
    re.IGNORECASE,
)
_AUTH_VALUE = re.compile(
    r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE
)
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_COMMON_API_KEY = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"sk-[A-Za-z0-9_-]{16,}|sk_[A-Za-z0-9_-]{16,}|"
    r"pk_live_[A-Za-z0-9]{16,}|rk_live_[A-Za-z0-9]{16,}|"
    r"AIza[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|"
    r"xox[a-z]-[A-Za-z0-9-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SUMMARY_STRING_FIELDS = (
    "id",
    "status",
    "provider_id",
    "model",
    "model_run_id",
    "parent_run_id",
)
_SUMMARY_NUMBER_FIELDS = (
    "captured_at",
    "started_at",
    "ended_at",
    "duration_ms",
    "stored_size",
)
_USAGE_FIELDS = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "reasoning_tokens",
    }
)
_USAGE_CONTAINER_FIELDS = frozenset({"token_usage", "usage_metadata"})


class ProviderCaptureStore:
    """Persist one active Provider target and bounded gzip capture records."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.records_root = self.root / "records"
        self.target_path = self.root / "target.json"
        self.manifest_path = self.records_root / "index.json"
        self.lock_path = self.root / ".records.lock"
        self._lock = threading.RLock()

    def target(self) -> str | None:
        """Return the persisted canonical Provider ID, if capture is active."""

        with self._lock:
            try:
                if not _is_regular_file(self.target_path):
                    return None
                with self.target_path.open("rb") as stream:
                    raw = stream.read(_MAX_TARGET_BYTES + 1)
                if len(raw) > _MAX_TARGET_BYTES:
                    return None
                payload = _strict_json_loads(raw)
            except (
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                OverflowError,
                RecursionError,
                TypeError,
                ValueError,
            ):
                return None
            if not isinstance(payload, dict):
                return None
            provider_id = payload.get("provider_id")
            return provider_id if _valid_provider_id(provider_id) else None

    def select(self, provider_id: str) -> None:
        """Atomically start capture for exactly one canonical Provider ID."""

        if not _valid_provider_id(provider_id):
            raise ValueError("provider_id must be a non-empty canonical string")
        selected = provider_id.strip()
        payload = _json_bytes({"provider_id": selected})
        with self._lock:
            self._atomic_write(self.target_path, payload)

    def stop(self) -> None:
        """Stop new captures without deleting retained records."""

        with self._lock:
            try:
                self.target_path.unlink()
            except FileNotFoundError:
                return
            _fsync_directory(self.root)

    def write(self, record: Mapping[str, object]) -> str:
        """Sanitize, bound, and atomically persist one capture record."""

        if not isinstance(record, Mapping):
            raise TypeError("capture record must be a mapping")
        record_id = uuid4().hex
        sanitized = _sanitize(record)
        if not isinstance(sanitized, dict):  # Defensive: Mapping always sanitizes to dict.
            raise TypeError("capture record must sanitize to an object")
        document = dict(sanitized)
        document["id"] = record_id
        document["captured_at"] = time.time()
        document = _bound_document(document)
        payload = _json_bytes(document)
        if len(payload) > MAX_RECORD_BYTES:
            raise ValueError("bounded capture record exceeds maximum size")
        validated = _strict_json_loads(payload)
        if not _valid_record_document(validated, record_id):
            raise ValueError("bounded capture record has an invalid final shape")
        assert isinstance(validated, dict)
        document = validated

        with self._lock:
            self._ensure_records_root()
            with self._filesystem_lock():
                self._remove_capture_temps()
                entries = self._load_or_rebuild_manifest()
                record_path = self._record_path(record_id)
                preview_path = self._preview_path(record_id)
                self._atomic_gzip_write(record_path, payload)
                try:
                    metadata = record_path.stat(follow_symlinks=False)
                    selected = dict(document)
                    selected["stored_size"] = metadata.st_size
                    _, preview_payload = _bounded_preview_document(
                        selected,
                        metadata.st_mtime_ns,
                    )
                    self._atomic_preview_write(preview_path, preview_payload)
                    preview_metadata = preview_path.stat(follow_symlinks=False)
                    entries = [
                        entry for entry in entries if entry["id"] != record_id
                    ]
                    entries.append(
                        _manifest_entry(
                            record_id=record_id,
                            captured_at=float(document["captured_at"]),
                            record_metadata=metadata,
                            preview_metadata=preview_metadata,
                        )
                    )
                    entries = self._prune_manifest_entries(entries)
                    self._write_manifest(entries)
                except Exception:
                    self._remove_record_pair(record_id)
                    _fsync_directory(self.records_root)
                    raise
        return record_id

    def list(self, limit: int = MAX_LIST_RECORDS) -> list[dict[str, object]]:
        """Return newest-first bounded previews, never full input/output values."""

        selected_limit = max(0, min(int(limit), MAX_LIST_RECORDS))
        if selected_limit == 0:
            return []
        if not self._records_available():
            return []
        with self._lock:
            with self._filesystem_lock():
                entries = self._read_manifest()
                if entries is None:
                    return []
                retained = self._prune_manifest_entries(entries)
                if retained != entries:
                    self._write_manifest(retained)
                candidates = retained[:selected_limit]
                records: list[dict[str, object]] = []
                for entry in candidates:
                    record_id = str(entry["id"])
                    item = self._read_preview(self._preview_path(record_id))
                    if item is None:
                        continue
                    records.append(item)
        return records[:selected_limit]

    def get(self, record_id: str) -> dict[str, object] | None:
        """Return one full bounded record, rejecting every non-canonical ID."""

        selected = _validate_record_id(record_id)
        if not self._records_available():
            return None
        with self._lock:
            with self._filesystem_lock():
                entries = self._read_manifest()
                if entries is None:
                    return None
                retained = self._prune_manifest_entries(entries)
                if retained != entries:
                    self._write_manifest(retained)
                if not any(entry["id"] == selected for entry in retained):
                    return None
                record = self._read(self._record_path(selected))
                if record is None:
                    return None
                try:
                    stored_size = self._record_path(selected).stat().st_size
                except OSError:
                    return None
        result = dict(record)
        result["stored_size"] = stored_size
        return result

    def clear(self) -> None:
        """Delete retained records without changing the active target."""

        if not self._records_available():
            return
        with self._lock:
            with self._filesystem_lock():
                removed = False
                try:
                    candidates = tuple(self.records_root.iterdir())
                except OSError:
                    candidates = ()
                for path in candidates:
                    if not (
                        path.name == self.manifest_path.name
                        or path.name.endswith(".json.gz")
                        or path.name.endswith(".preview.json")
                        or _is_capture_temp(path)
                    ):
                        continue
                    try:
                        path.unlink()
                        removed = True
                    except FileNotFoundError:
                        continue
                if removed:
                    _fsync_directory(self.records_root)

    def _records_available(self) -> bool:
        try:
            metadata = self.records_root.stat(follow_symlinks=False)
        except (FileNotFoundError, OSError):
            return False
        return stat.S_ISDIR(metadata.st_mode)

    @contextmanager
    def _filesystem_lock(self):
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("unsafe capture lock file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _record_path(self, record_id: str) -> Path:
        return self.records_root / f"{record_id}.json.gz"

    def _preview_path(self, record_id: str) -> Path:
        return self.records_root / f"{record_id}.preview.json"

    def _ensure_records_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("unsafe capture root")
        self.records_root.mkdir(exist_ok=True)
        if self.records_root.is_symlink() or not self.records_root.is_dir():
            raise ValueError("unsafe capture records directory")

    def _read_manifest(self) -> list[dict[str, object]] | None:
        try:
            if not _is_regular_file(self.manifest_path):
                return None
            with self.manifest_path.open("rb") as stream:
                payload = stream.read(_MAX_MANIFEST_BYTES + 1)
            if len(payload) > _MAX_MANIFEST_BYTES:
                return None
            value = _strict_json_loads(payload)
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None
        return _validated_manifest(value)

    def _write_manifest(self, entries: list[dict[str, object]]) -> None:
        selected = _sort_manifest(entries)
        payload = _json_bytes(
            {"records": selected, "version": _MANIFEST_VERSION}
        )
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise ValueError("capture manifest exceeds encoded limit")
        parsed = _strict_json_loads(payload)
        if _validated_manifest(parsed) is None:
            raise ValueError("capture manifest has an invalid final shape")
        self._atomic_preview_write(self.manifest_path, payload)

    def _load_or_rebuild_manifest(self) -> list[dict[str, object]]:
        entries = self._read_manifest()
        return entries if entries is not None else self._rebuild_manifest()

    def _record_paths(self) -> list[Path]:
        if not self.records_root.exists() or self.records_root.is_symlink():
            return []
        paths: list[Path] = []
        try:
            candidates = tuple(self.records_root.iterdir())
        except OSError:
            return []
        for path in candidates:
            if not path.name.endswith(".json.gz"):
                continue
            record_id = path.name.removesuffix(".json.gz")
            if _RECORD_ID.fullmatch(record_id) is None or not _is_regular_file(path):
                continue
            paths.append(path)
        return paths

    def _preview_paths(self) -> list[Path]:
        if not self.records_root.exists() or self.records_root.is_symlink():
            return []
        paths: list[Path] = []
        try:
            candidates = tuple(self.records_root.iterdir())
        except OSError:
            return []
        for path in candidates:
            if not path.name.endswith(".preview.json"):
                continue
            record_id = path.name.removesuffix(".preview.json")
            if _RECORD_ID.fullmatch(record_id) is None or not _is_regular_file(path):
                continue
            paths.append(path)
        return paths

    def _read(self, path: Path) -> dict[str, object] | None:
        try:
            with gzip.open(path, "rb") as stream:
                payload = stream.read(MAX_RECORD_BYTES + 1)
            if len(payload) > MAX_RECORD_BYTES:
                return None
            record = _strict_json_loads(payload)
        except (
            FileNotFoundError,
            OSError,
            EOFError,
            gzip.BadGzipFile,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None
        expected_id = path.name.removesuffix(".json.gz")
        if not _valid_record_document(record, expected_id):
            return None
        assert isinstance(record, dict)
        return record

    def _read_preview(self, path: Path) -> dict[str, object] | None:
        try:
            if not _is_regular_file(path):
                return None
            with path.open("rb") as stream:
                payload = stream.read(MAX_PREVIEW_INDEX_BYTES + 1)
            if len(payload) > MAX_PREVIEW_INDEX_BYTES:
                return None
            preview = _strict_json_loads(payload)
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            return None
        record_id = path.name.removesuffix(".preview.json")
        selected = _validated_preview_record(preview, record_id)
        if selected is None:
            return None
        try:
            metadata = self._record_path(record_id).stat(follow_symlinks=False)
        except OSError:
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size != selected.get("stored_size")
            or metadata.st_mtime_ns != preview.get("_record_mtime_ns")
        ):
            return None
        return selected

    def maintain(self) -> None:
        """Repair indexes and physically enforce every retention bound."""

        if not self._records_available():
            return
        with self._lock:
            with self._filesystem_lock():
                entries = self._rebuild_manifest()
                self._write_manifest(entries)

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.root, prefix=f".{target.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_gzip_write(self, target: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.records_root, prefix=".capture-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as stream:
                    stream.write(payload)
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, target)
            _fsync_directory(self.records_root)
        finally:
            temporary.unlink(missing_ok=True)

    def _atomic_preview_write(self, target: Path, payload: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.records_root, prefix=".capture-", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(self.records_root)
        finally:
            temporary.unlink(missing_ok=True)

    def _rebuild_manifest(self) -> list[dict[str, object]]:
        removed = self._remove_capture_temps()
        removed = self._remove_noncanonical_capture_files() or removed
        cutoff = time.time() - RETENTION_SECONDS
        candidates: list[tuple[os.stat_result, Path]] = []
        for path in self._record_paths():
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError:
                continue
            candidates.append((metadata, path))
        candidates.sort(
            key=lambda item: (item[0].st_mtime_ns, item[1].name),
            reverse=True,
        )
        retained_paths: list[tuple[os.stat_result, Path]] = []
        for index, (metadata, path) in enumerate(candidates):
            if metadata.st_mtime < cutoff or index >= MAX_RETAINED_RECORDS:
                record_id = path.name.removesuffix(".json.gz")
                removed = self._remove_record_pair(record_id) or removed
                continue
            retained_paths.append((metadata, path))

        retained_ids = {
            path.name.removesuffix(".json.gz") for _, path in retained_paths
        }
        for preview_path in self._preview_paths():
            record_id = preview_path.name.removesuffix(".preview.json")
            if record_id in retained_ids:
                continue
            try:
                preview_path.unlink()
                removed = True
            except (FileNotFoundError, OSError):
                continue

        entries: list[dict[str, object]] = []
        for _, record_path in retained_paths:
            record_id = record_path.name.removesuffix(".json.gz")
            preview_path = self._preview_path(record_id)
            preview = self._read_preview(preview_path)
            if preview is None:
                record = self._read(record_path)
                if record is None:
                    removed = self._remove_record_pair(record_id) or removed
                    continue
                try:
                    record_metadata = record_path.stat(follow_symlinks=False)
                    selected = dict(record)
                    selected["stored_size"] = record_metadata.st_size
                    _, preview_payload = _bounded_preview_document(
                        selected,
                        record_metadata.st_mtime_ns,
                    )
                    self._atomic_preview_write(preview_path, preview_payload)
                    preview = self._read_preview(preview_path)
                except (FileNotFoundError, OSError, ValueError, TypeError):
                    preview = None
            if preview is None:
                removed = self._remove_record_pair(record_id) or removed
                continue
            try:
                record_metadata = record_path.stat(follow_symlinks=False)
                preview_metadata = preview_path.stat(follow_symlinks=False)
            except OSError:
                removed = self._remove_record_pair(record_id) or removed
                continue
            entries.append(
                _manifest_entry(
                    record_id=record_id,
                    captured_at=float(preview["captured_at"]),
                    record_metadata=record_metadata,
                    preview_metadata=preview_metadata,
                )
            )
        entries = self._prune_manifest_entries(entries)
        if removed:
            _fsync_directory(self.records_root)
        return entries

    def _prune_manifest_entries(
        self,
        entries: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        cutoff = time.time() - RETENTION_SECONDS
        retained: list[dict[str, object]] = []
        removed = False
        for entry in entries:
            record_id = str(entry["id"])
            try:
                record_metadata = self._record_path(record_id).stat(
                    follow_symlinks=False
                )
                preview_metadata = self._preview_path(record_id).stat(
                    follow_symlinks=False
                )
            except OSError:
                removed = self._remove_record_pair(record_id) or removed
                continue
            if (
                not stat.S_ISREG(record_metadata.st_mode)
                or not stat.S_ISREG(preview_metadata.st_mode)
                or record_metadata.st_mtime < cutoff
            ):
                removed = self._remove_record_pair(record_id) or removed
                continue
            retained.append(
                _manifest_entry(
                    record_id=record_id,
                    captured_at=float(entry["captured_at"]),
                    record_metadata=record_metadata,
                    preview_metadata=preview_metadata,
                )
            )
        retained = _sort_manifest(retained)

        while len(retained) > MAX_RETAINED_RECORDS:
            if not self._remove_oldest_manifest_entry(retained):
                break
            removed = True

        total = sum(
            int(entry["record_size"]) + int(entry["preview_size"])
            for entry in retained
        )
        while total > MAX_STORAGE_BYTES and retained:
            before = total
            for index in range(len(retained) - 1, -1, -1):
                entry = retained[index]
                if not self._remove_record_pair(str(entry["id"])):
                    continue
                total -= int(entry["record_size"]) + int(entry["preview_size"])
                retained.pop(index)
                removed = True
                break
            if total == before:
                break
        if removed:
            _fsync_directory(self.records_root)
        return retained

    def _remove_oldest_manifest_entry(
        self,
        entries: list[dict[str, object]],
    ) -> bool:
        for index in range(len(entries) - 1, -1, -1):
            if self._remove_record_pair(str(entries[index]["id"])):
                entries.pop(index)
                return True
        return False

    def _remove_record_pair(self, record_id: str) -> bool:
        for path in (self._record_path(record_id), self._preview_path(record_id)):
            try:
                path.unlink()
            except (FileNotFoundError, OSError):
                continue
        return not _is_regular_file(self._record_path(record_id)) and not _is_regular_file(
            self._preview_path(record_id)
        )

    def _remove_capture_temps(self) -> bool:
        removed = False
        try:
            candidates = tuple(self.records_root.iterdir())
        except OSError:
            return False
        for path in candidates:
            if not _is_capture_temp(path):
                continue
            try:
                path.unlink()
                removed = True
            except (FileNotFoundError, OSError):
                continue
        return removed

    def _remove_noncanonical_capture_files(self) -> bool:
        removed = False
        try:
            candidates = tuple(self.records_root.iterdir())
        except OSError:
            return False
        for path in candidates:
            record_id: str | None = None
            if path.name.endswith(".json.gz"):
                record_id = path.name.removesuffix(".json.gz")
            elif path.name.endswith(".preview.json"):
                record_id = path.name.removesuffix(".preview.json")
            if (
                record_id is None
                or _RECORD_ID.fullmatch(record_id) is not None
                or not _is_regular_file(path)
            ):
                continue
            try:
                path.unlink()
                removed = True
            except (FileNotFoundError, OSError):
                continue
        return removed


class ProviderCaptureCallback(BaseCallbackHandler):
    """Capture chat-model calls selected at start without affecting execution."""

    def __init__(self, store: ProviderCaptureStore) -> None:
        self.store = store
        self._calls: dict[str, dict[str, object]] = {}
        self._http_hooks: dict[str, ProviderCaptureHTTPHooks] = {}
        self._lock = threading.Lock()

    def http_hooks(self, provider_id: str) -> ProviderCaptureHTTPHooks:
        """Return reusable sync/async hooks for a ChatOpenAI provider client."""
        with self._lock:
            hooks = self._http_hooks.get(provider_id)
            if hooks is None:
                hooks = ProviderCaptureHTTPHooks(self.store, provider_id)
                self._http_hooks[provider_id] = hooks
            return hooks

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        key = str(run_id)
        with self._lock:
            self._calls.pop(key, None)
        try:
            details = dict(metadata or {})
            _ACTIVE_CAPTURE_RUN.set(None)
            provider_id = details.get("provider_id")
            if not isinstance(provider_id, str) or provider_id != self.store.target():
                return
            _ACTIVE_CAPTURE_RUN.set(key)
            with _ACTIVE_CAPTURE_RUNS_LOCK:
                _ACTIVE_CAPTURE_RUNS[provider_id] = key
            call = {
                "model_run_id": key,
                "parent_run_id": str(parent_run_id) if parent_run_id else None,
                "provider_id": provider_id,
                "model": _model_name(serialized, details),
                "started_at": time.time(),
                "metadata": details,
                "serialized": serialized,
                "invocation_parameters": kwargs,
                "tags": list(tags or []),
            }
            with self._lock:
                self._calls[key] = call
        except Exception:
            LOGGER.exception("Provider capture start failed")

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        call = self._pop(run_id)
        if call is None:
            return
        try:
            ended_at = time.time()
            completion: dict[str, object] = {
                "ended_at": ended_at,
                "duration_ms": max(
                    0,
                    round((ended_at - float(call["started_at"])) * 1000),
                ),
                "status": "success",
            }
            usage = _llm_result_usage(response)
            if usage:
                completion["usage"] = usage
            http_exchange = self.http_hooks(str(call["provider_id"])).pop(str(run_id))
            _ACTIVE_CAPTURE_RUN.set(None)
            with _ACTIVE_CAPTURE_RUNS_LOCK:
                if _ACTIVE_CAPTURE_RUNS.get(str(call["provider_id"])) == str(run_id):
                    _ACTIVE_CAPTURE_RUNS.pop(str(call["provider_id"]), None)
            if http_exchange:
                call.update({"http_request": http_exchange.get("request", {}),
                             "http_response": http_exchange.get("response", {})})
            call.update(completion)
            self.store.write(call)
            with bind_log_context(provider_request_id=str(run_id)):
                LOGGER.info(
                    "Provider request completed",
                    extra={
                        "event": "provider_request",
                        "duration_ms": completion["duration_ms"],
                    },
                )
        except Exception:
            LOGGER.exception("Provider capture completion failed")

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        call = self._pop(run_id)
        if call is None:
            return
        try:
            ended_at = time.time()
            call.update(
                {
                    "ended_at": ended_at,
                    "duration_ms": max(0, round((ended_at - float(call["started_at"])) * 1000)),
                    "status": "error",
                    "error": str(error),
                }
            )
            http_exchange = self.http_hooks(str(call["provider_id"])).pop(str(run_id))
            _ACTIVE_CAPTURE_RUN.set(None)
            with _ACTIVE_CAPTURE_RUNS_LOCK:
                if _ACTIVE_CAPTURE_RUNS.get(str(call["provider_id"])) == str(run_id):
                    _ACTIVE_CAPTURE_RUNS.pop(str(call["provider_id"]), None)
            if http_exchange:
                call.update({"http_request": http_exchange.get("request", {}),
                             "http_response": http_exchange.get("response", {})})
            self.store.write(call)
            with bind_log_context(provider_request_id=str(run_id)):
                LOGGER.error(
                    "Provider request failed: %s",
                    error,
                    extra={
                        "event": "provider_request",
                        "duration_ms": call["duration_ms"],
                    },
                )
        except Exception:
            LOGGER.exception("Provider capture error completion failed")

    def _pop(self, run_id: UUID) -> dict[str, object] | None:
        with self._lock:
            return self._calls.pop(str(run_id), None)


def _valid_provider_id(value: object) -> bool:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 0 < len(value) <= 256
        or any(ord(character) < 32 for character in value)
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validate_record_id(value: object) -> str:
    if not isinstance(value, str) or _RECORD_ID.fullmatch(value) is None:
        raise ValueError("invalid capture record id")
    return value


def _is_regular_file(path: Path) -> bool:
    try:
        metadata = path.stat(follow_symlinks=False)
    except (FileNotFoundError, OSError):
        return False
    return stat.S_ISREG(metadata.st_mode)


def _is_capture_temp(path: Path) -> bool:
    return (
        path.name.startswith(".capture-")
        and path.name.endswith(".tmp")
        and _is_regular_file(path)
    )


def _manifest_entry(
    *,
    record_id: str,
    captured_at: float,
    record_metadata: os.stat_result,
    preview_metadata: os.stat_result,
) -> dict[str, object]:
    return {
        "captured_at": captured_at,
        "id": record_id,
        "preview_size": preview_metadata.st_size,
        "record_mtime_ns": record_metadata.st_mtime_ns,
        "record_size": record_metadata.st_size,
    }


def _validated_manifest_entry(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    record_id = value.get("id")
    captured_at = value.get("captured_at")
    if (
        not isinstance(record_id, str)
        or _RECORD_ID.fullmatch(record_id) is None
        or isinstance(captured_at, bool)
        or not isinstance(captured_at, (int, float))
        or not math.isfinite(captured_at)
    ):
        return None
    numbers: dict[str, int] = {}
    for key in ("preview_size", "record_mtime_ns", "record_size"):
        selected = value.get(key)
        if isinstance(selected, bool) or not isinstance(selected, int) or selected < 0:
            return None
        numbers[key] = selected
    return {
        "captured_at": captured_at,
        "id": record_id,
        **numbers,
    }


def _validated_manifest(value: object) -> list[dict[str, object]] | None:
    if (
        not isinstance(value, dict)
        or value.get("version") != _MANIFEST_VERSION
        or not isinstance(value.get("records"), list)
        or len(value["records"]) > MAX_RETAINED_RECORDS
    ):
        return None
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value["records"]:
        selected = _validated_manifest_entry(item)
        if selected is None or selected["id"] in seen:
            return None
        seen.add(str(selected["id"]))
        entries.append(selected)
    return _sort_manifest(entries)


def _sort_manifest(
    entries: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    return sorted(
        entries,
        key=lambda entry: (float(entry["captured_at"]), str(entry["id"])),
        reverse=True,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sanitize(
    value: object,
    *,
    in_headers: bool = False,
    depth: int = 0,
) -> object:
    if depth >= _MAX_JSON_DEPTH - 1 and (
        isinstance(value, Mapping)
        or (
            isinstance(value, Sequence)
            and not isinstance(value, (str, bytes, bytearray, memoryview))
        )
    ):
        return {"depth": depth, "type": "max-depth"}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if _bounded_integer(value):
            return value
        return {"bits": value.bit_length(), "type": "large-integer"}
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "non-finite-number"}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"bytes": len(value), "type": "binary"}
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key, redacted_key = _sanitize_key(raw_key)
            if in_headers and isinstance(raw_key, str):
                key, redacted_key = raw_key, False
            if (
                not redacted_key
                and _normalized_key(key) in {"name", "toolname", "functionname"}
                and isinstance(raw_value, str)
            ):
                sanitized[key] = raw_value
                continue
            secret_value = redacted_key or _is_secret_key(key, raw_value)
            header_value = in_headers or _is_header_container_key(key)
            if secret_value:
                sanitized[key] = "[REDACTED]"
            else:
                sanitized[key] = _sanitize(
                    raw_value,
                    in_headers=header_value,
                    depth=depth + 1,
                )
        return sanitized
    if isinstance(value, Sequence):
        if in_headers:
            return [_sanitize_header_item(item, depth=depth + 1) for item in value]
        return [_sanitize(item, depth=depth + 1) for item in value]
    return {"python_type": type(value).__name__, "type": "unsupported"}


def _sanitize_header_item(value: object, *, depth: int) -> object:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        if len(items) == 2:
            key = str(items[0])
            redacted_key = False
            if redacted_key or _is_secret_key(key, items[1]):
                return [key, "[REDACTED]"]
            return [key, _sanitize(items[1], depth=depth)]
    if isinstance(value, Mapping):
        return _sanitize(value, in_headers=True, depth=depth)
    return "[REDACTED]"


def _sanitize_key(value: object) -> tuple[str, bool]:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[BINARY_KEY]", True
    if isinstance(value, str):
        normalized = _normalized_key(value)
        if (
            normalized not in _USAGE_FIELDS
            and normalized not in _USAGE_CONTAINER_FIELDS
            and (_is_secret_key(value, "") or _sanitize_string(value) != value)
        ):
            return "[REDACTED_KEY]", True
        return value, False
    if value is None or isinstance(value, (bool, int, float)):
        return str(value), False
    return f"[UNSUPPORTED_KEY:{type(value).__name__}]", True


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", value.strip().lower())


def _is_header_container_key(value: str) -> bool:
    compact = re.sub(r"[^a-z0-9]+", "", value.strip().lower())
    return compact.endswith("header") or compact.endswith("headers")


def _is_secret_key(value: str, raw_value: object) -> bool:
    normalized = _normalized_key(value)
    if normalized in _USAGE_FIELDS:
        return not _is_nonnegative_finite_number(raw_value)
    if normalized in _USAGE_CONTAINER_FIELDS:
        return not _is_strict_usage_mapping(raw_value)
    return _SECRET_NAME.search(normalized) is not None


def _is_nonnegative_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return (
        value >= 0
        and (not isinstance(value, int) or _bounded_integer(value))
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _bounded_integer(value: int) -> bool:
    return -_MAX_JSON_INTEGER_ABS <= value <= _MAX_JSON_INTEGER_ABS


def _is_strict_usage_mapping(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    return all(
        isinstance(key, str)
        and _normalized_key(key) in _USAGE_FIELDS
        and _is_nonnegative_finite_number(item)
        for key, item in value.items()
    )


def _sanitize_string(value: str) -> str:
    sanitized = _AUTH_VALUE.sub(lambda match: f"{match.group(1)} [REDACTED]", value)
    sanitized = _JWT.sub("[REDACTED]", sanitized)
    sanitized = _COMMON_API_KEY.sub("[REDACTED]", sanitized)
    return sanitized


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _strict_json_loads(payload: bytes) -> object:
    value = json.loads(
        payload,
        parse_constant=_reject_json_constant,
        parse_int=_bounded_json_int,
    )
    if not _valid_json_tree(value):
        raise ValueError("capture record contains unsupported JSON values")
    return value


def _valid_record_document(value: object, expected_id: str) -> bool:
    if not isinstance(value, dict) or value.get("id") != expected_id:
        return False
    captured_at = value.get("captured_at")
    return (
        not isinstance(captured_at, bool)
        and isinstance(captured_at, (int, float))
        and math.isfinite(captured_at)
    )


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


def _bounded_json_int(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError("capture record integer is too large")
    return int(value)


def _valid_json_tree(value: object) -> bool:
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        selected, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            return False
        if selected is None or isinstance(selected, (bool, int)):
            continue
        if isinstance(selected, float):
            if not math.isfinite(selected):
                return False
            continue
        if isinstance(selected, str):
            try:
                selected.encode("utf-8")
            except UnicodeEncodeError:
                return False
            continue
        if isinstance(selected, list):
            pending.extend((item, depth + 1) for item in selected)
            continue
        if isinstance(selected, dict):
            for key, item in selected.items():
                if not isinstance(key, str):
                    return False
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError:
                    return False
                pending.append((item, depth + 1))
            continue
        return False
    return True


def _bound_document(document: dict[str, object]) -> dict[str, object]:
    original_document_bytes = len(_json_bytes(document))
    if original_document_bytes <= MAX_RECORD_BYTES:
        return document
    bounded = dict(document)
    protected = {"id", "captured_at"}
    candidates = sorted(
        (
            (len(_json_bytes(value)), key, value)
            for key, value in bounded.items()
            if key not in protected
        ),
        reverse=True,
    )
    truncated_fields: list[str] = []
    omitted_bytes = 0
    for original_bytes, key, value in candidates:
        text = value if isinstance(value, str) else _json_bytes(value).decode("utf-8")
        head = _utf8_head(text, PREVIEW_BYTES)
        tail = _utf8_tail(text, PREVIEW_BYTES)
        retained_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
        selected_omitted_bytes = max(0, original_bytes - retained_bytes)
        bounded[key] = {
            "head": head,
            "omitted_bytes": selected_omitted_bytes,
            "original_bytes": original_bytes,
            "tail": tail,
            "type": "truncated",
        }
        truncated_fields.append(key)
        omitted_bytes += selected_omitted_bytes
        bounded["truncation"] = _root_truncation(
            original_document_bytes,
            omitted_bytes,
            truncated_fields,
        )
        if len(_json_bytes(bounded)) <= MAX_RECORD_BYTES:
            return bounded
    return {
        "captured_at": document["captured_at"],
        "id": document["id"],
        "truncation": _root_truncation(
            original_document_bytes,
            original_document_bytes,
            [key for _, key, _ in candidates],
            compact=True,
        ),
    }


def _root_truncation(
    original_bytes: int,
    omitted_bytes: int,
    fields: Sequence[str],
    *,
    compact: bool = False,
) -> dict[str, object]:
    selected_fields = [
        _summary_string(field) or "[UNNAMED_FIELD]"
        for field in fields
    ]
    if compact and len(selected_fields) > 16:
        selected_fields = selected_fields[:16] + ["[ADDITIONAL_FIELDS]"]
    return {
        "field_count": len(fields),
        "fields": selected_fields,
        "omitted_bytes": omitted_bytes,
        "original_bytes": original_bytes,
        "type": "truncated",
    }


def _preview_record(
    record: dict[str, object],
    *,
    preview_bytes: int = PREVIEW_BYTES,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key in _SUMMARY_STRING_FIELDS:
        selected = _summary_string(record.get(key))
        if selected is not None:
            result[key] = selected
    for key in _SUMMARY_NUMBER_FIELDS:
        selected = _summary_number(record.get(key))
        if selected is not None:
            result[key] = selected
    usage = _summary_usage(record.get("usage"))
    if usage:
        result["usage"] = usage
    truncation = _summary_truncation(record.get("truncation"))
    if truncation:
        result["truncation"] = truncation
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("agent_id", "session_id", "run_id"):
            selected = _summary_string(metadata.get(key))
            if selected is not None:
                result[key] = selected
    request = record.get("http_request")
    request_body = request.get("body", "") if isinstance(request, Mapping) else ""
    response = record.get("http_response")
    response_body = (
        response.get("body", "")
        if isinstance(response, Mapping)
        else record.get("error", "")
    )
    input_preview = _preview(request_body, limit=preview_bytes)
    output_preview = _preview(response_body, limit=preview_bytes)
    result["input_characters"] = input_preview["characters"]
    result["output_characters"] = output_preview["characters"]
    result["input_preview"] = input_preview
    result["output_preview"] = output_preview
    return result


def _bounded_preview_document(
    record: dict[str, object],
    record_mtime_ns: int,
) -> tuple[dict[str, object], bytes]:
    """Return the largest symmetric preview whose exact JSON fits the sidecar."""

    best: tuple[dict[str, object], bytes] | None = None
    lower = 0
    upper = PREVIEW_BYTES
    while lower <= upper:
        selected_bytes = (lower + upper) // 2
        preview = _preview_record(record, preview_bytes=selected_bytes)
        preview["_record_mtime_ns"] = record_mtime_ns
        payload = _json_bytes(preview)
        if len(payload) <= MAX_PREVIEW_INDEX_BYTES:
            best = preview, payload
            lower = selected_bytes + 1
        else:
            upper = selected_bytes - 1
    if best is None:
        raise ValueError("capture preview metadata exceeds encoded sidecar limit")
    preview, payload = best
    parsed = _strict_json_loads(payload)
    record_id = record.get("id")
    if not isinstance(record_id, str) or _validated_preview_record(parsed, record_id) is None:
        raise ValueError("capture preview has an invalid final shape")
    return preview, payload


def _validated_preview_record(
    value: object,
    expected_id: str,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or value.get("id") != expected_id:
        return None
    captured_at = value.get("captured_at")
    stored_size = value.get("stored_size")
    record_mtime_ns = value.get("_record_mtime_ns")
    if (
        isinstance(captured_at, bool)
        or not isinstance(captured_at, (int, float))
        or not math.isfinite(captured_at)
        or isinstance(stored_size, bool)
        or not isinstance(stored_size, int)
        or stored_size < 0
        or isinstance(record_mtime_ns, bool)
        or not isinstance(record_mtime_ns, int)
        or record_mtime_ns < 0
    ):
        return None

    result: dict[str, object] = {"id": expected_id, "captured_at": captured_at}
    for key in _SUMMARY_STRING_FIELDS:
        if key == "id":
            continue
        selected = _summary_string(value.get(key))
        if selected is not None:
            result[key] = selected
    for key in _SUMMARY_NUMBER_FIELDS:
        if key == "captured_at":
            continue
        selected = _summary_number(value.get(key))
        if selected is not None:
            result[key] = selected
    for key in ("agent_id", "session_id", "run_id"):
        selected = _summary_string(value.get(key))
        if selected is not None:
            result[key] = selected
    usage = _summary_usage(value.get("usage"))
    if usage:
        result["usage"] = usage
    truncation = _summary_truncation(value.get("truncation"))
    if truncation:
        result["truncation"] = truncation

    for prefix in ("input", "output"):
        preview = _validated_preview(value.get(f"{prefix}_preview"))
        if preview is None:
            return None
        characters = value.get(f"{prefix}_characters")
        if (
            isinstance(characters, bool)
            or not isinstance(characters, int)
            or characters < 0
            or characters != preview["characters"]
        ):
            return None
        result[f"{prefix}_characters"] = characters
        result[f"{prefix}_preview"] = preview
    return result


def _validated_preview(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    head = value.get("head")
    tail = value.get("tail")
    truncated = value.get("truncated")
    size = value.get("bytes")
    characters = value.get("characters")
    omitted_bytes = value.get("omitted_bytes")
    if (
        not isinstance(head, str)
        or not isinstance(tail, str)
        or not isinstance(truncated, bool)
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in (size, characters, omitted_bytes)
        )
    ):
        return None
    try:
        head_bytes = len(head.encode("utf-8"))
        tail_bytes = len(tail.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if (
        head_bytes > PREVIEW_BYTES
        or tail_bytes > PREVIEW_BYTES
        or head_bytes + tail_bytes + omitted_bytes != size
        or truncated is not (omitted_bytes > 0)
        or characters > size
        or characters < len(head) + len(tail)
    ):
        return None
    return {
        "head": head,
        "tail": tail,
        "truncated": truncated,
        "omitted_bytes": omitted_bytes,
        "bytes": size,
        "characters": characters,
    }


def _summary_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= SUMMARY_STRING_CHARS:
        return value
    return f"[TRUNCATED: {len(value)} characters]"


def _summary_number(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if len(str(value)) <= 64 else None


def _summary_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for key in sorted(_USAGE_FIELDS):
        selected = _summary_number(value.get(key))
        if selected is not None and selected >= 0:
            result[key] = selected
    return result


def _summary_truncation(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or value.get("type") != "truncated":
        return {}
    result: dict[str, object] = {"type": "truncated"}
    for key in ("field_count", "original_bytes", "omitted_bytes"):
        selected = _summary_number(value.get(key))
        if selected is not None and selected >= 0:
            result[key] = selected
    return result


def _preview(value: object, *, limit: int = PREVIEW_BYTES) -> dict[str, object]:
    text = value if isinstance(value, str) else _json_bytes(value).decode("utf-8")
    size = len(text.encode("utf-8"))
    head = _utf8_head(text, limit)
    tail = _utf8_tail(text, limit)
    head_end = len(head)
    tail_start = len(text) - len(tail)
    if head_end >= tail_start:
        tail = text[head_end:]
    omitted_bytes = max(
        0,
        size - len(head.encode("utf-8")) - len(tail.encode("utf-8")),
    )
    return {
        "head": head,
        "tail": tail,
        "truncated": omitted_bytes > 0,
        "omitted_bytes": omitted_bytes,
        "bytes": size,
        "characters": len(text),
    }


def _utf8_head(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def _utf8_tail(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    return value.encode("utf-8")[-limit:].decode("utf-8", errors="ignore")


def _model_name(serialized: Mapping[str, object], metadata: Mapping[str, object]) -> str | None:
    for value in (
        metadata.get("ls_model_name"),
        serialized.get("name"),
        serialized.get("id"),
    ):
        if isinstance(value, str) and value:
            return value
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and value:
            last = value[-1]
            if isinstance(last, str):
                return last
    return None


def _llm_result_usage(response: LLMResult) -> dict[str, int | float]:
    llm_output = response.llm_output
    if isinstance(llm_output, Mapping):
        for key in ("token_usage", "usage_metadata"):
            selected = _normalized_usage(llm_output.get(key))
            if selected:
                return selected

    combined: dict[str, int | float] = {}
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            selected = _normalized_usage(getattr(message, "usage_metadata", None))
            if not selected:
                continue
            for key, value in selected.items():
                combined[key] = combined.get(key, 0) + value
            break
    return combined


def _normalized_usage(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int | float] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            continue
        key = _normalized_key(raw_key)
        if key in _USAGE_FIELDS and _is_nonnegative_finite_number(raw_value):
            result[key] = raw_value
    return result


__all__ = [
    "MAX_LIST_RECORDS",
    "MAX_PREVIEW_INDEX_BYTES",
    "MAX_RECORD_BYTES",
    "MAX_RETAINED_RECORDS",
    "MAX_STORAGE_BYTES",
    "PREVIEW_BYTES",
    "RETENTION_SECONDS",
    "ProviderCaptureCallback",
    "ProviderCaptureStore",
]
