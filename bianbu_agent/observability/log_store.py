"""Bounded JSONL storage for correlated operational logs."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any

from bianbu_agent.observability.context import current_log_context


_AUTH_VALUE = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|authorization|credential|password|passwd|secret|token)"
    r"[\"']?\s*[:=]\s*(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_COMMON_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{12,}")
_JWT = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_EXTRA_FIELDS = frozenset({"event", "run_status", "error_code", "duration_ms"})


def _redact(value: object) -> str:
    text = str(value)
    text = _AUTH_VALUE.sub("[REDACTED]", text)
    text = _ASSIGNMENT.sub("[REDACTED]", text)
    text = _COMMON_KEY.sub("[REDACTED]", text)
    return _JWT.sub("[REDACTED]", text)


def _private_log_opener(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


class _RuntimeLogHandler(logging.Handler):
    def __init__(self, store: "RuntimeLogStore") -> None:
        super().__init__(level=logging.INFO)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            document: dict[str, object] = {
                "timestamp": time.time(),
                "level": record.levelname,
                "logger": record.name,
                "message": _redact(record.getMessage()),
                **current_log_context(),
            }
            for field in _EXTRA_FIELDS:
                value = getattr(record, field, None)
                if value is not None and value != "":
                    document[field] = (
                        value if isinstance(value, (int, float)) else _redact(value)
                    )
            if record.exc_info is not None:
                exception = record.exc_info[1]
                document["exception"] = _redact(
                    f"{type(exception).__name__}: {exception}"
                )
            self.store._append(document)
        except Exception:
            # logging.handleError prints the original, unredacted record and
            # arguments to stderr. Never expose them when persistence fails.
            try:
                sys.stderr.write("Runtime operational log could not be persisted.\n")
            except (OSError, ValueError):
                pass


class RuntimeLogStore:
    """Process-safe bounded JSON Lines log with correlation queries."""

    def __init__(self, path: str | Path, *, max_bytes: int = 8 * 1024 * 1024) -> None:
        if isinstance(max_bytes, bool) or int(max_bytes) < 512:
            raise ValueError("max_bytes must be at least 512")
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = int(max_bytes)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._thread_lock = threading.Lock()
        self.handler = _RuntimeLogHandler(self)
        self._installed_levels: dict[logging.Logger, int] = {}

    def close(self) -> None:
        self.handler.close()

    def install(self, logger: logging.Logger | None = None) -> None:
        target = logger or logging.getLogger("bianbu_agent")
        if target not in self._installed_levels:
            self._installed_levels[target] = target.level
            if target.level == logging.NOTSET or target.level > logging.INFO:
                target.setLevel(logging.INFO)
        if self.handler not in target.handlers:
            target.addHandler(self.handler)

    def uninstall(self, logger: logging.Logger | None = None) -> None:
        target = logger or logging.getLogger("bianbu_agent")
        if self.handler in target.handlers:
            target.removeHandler(self.handler)
        original_level = self._installed_levels.pop(target, None)
        if original_level is not None:
            target.setLevel(original_level)
        self.close()

    def _append(self, document: dict[str, object]) -> None:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        if len(payload) > self.max_bytes:
            document["message"] = "[TRUNCATED: operational log record exceeded limit]"
            document.pop("exception", None)
            payload = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8") + b"\n"
        with self._thread_lock:
            with open(self.lock_path, "a+b", opener=_private_log_opener) as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current_size = self.path.stat().st_size if self.path.exists() else 0
                if current_size and current_size + len(payload) > self.max_bytes:
                    os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
                with open(self.path, "ab", buffering=0, opener=_private_log_opener) as stream:
                    stream.write(payload)
                    os.fsync(stream.fileno())
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def query(
        self,
        *,
        agent_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        selected_limit = int(limit)
        if selected_limit <= 0 or selected_limit > 10_000:
            raise ValueError("limit must be between 1 and 10000")
        filters = {
            key: str(value)
            for key, value in {
                "agent_id": agent_id,
                "session_id": session_id,
                "run_id": run_id,
            }.items()
            if value is not None and str(value)
        }
        records: list[dict[str, Any]] = []
        with self._thread_lock:
            with open(self.lock_path, "a+b", opener=_private_log_opener) as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                for path in (
                    self.path.with_suffix(self.path.suffix + ".1"),
                    self.path,
                ):
                    if not path.is_file():
                        continue
                    with path.open("r", encoding="utf-8", errors="replace") as stream:
                        for line in stream:
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(record, dict):
                                continue
                            if all(str(record.get(key) or "") == value for key, value in filters.items()):
                                records.append(record)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return records[-selected_limit:]


def log_run_terminal(
    logger: logging.Logger,
    *,
    status: str,
    error_code: str = "",
) -> None:
    logger.info(
        "Run reached terminal state: %s",
        status,
        extra={
            "event": "run_terminal",
            "run_status": str(status),
            "error_code": str(error_code),
        },
    )


__all__ = ["RuntimeLogStore", "log_run_terminal"]
