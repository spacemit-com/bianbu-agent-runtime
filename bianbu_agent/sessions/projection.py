"""The per-Session projection file.

Every Session directory holds exactly two files: the transcript
(``session.jsonl.zstd``) and this projection.  The projection carries two
things with two different authorities:

* ``record`` — the Session's identity and metadata (title, agent, thread).
  This is authority, the same row that used to live in ``sessions/<id>.json``:
  losing it loses the Session.
* ``row`` — the folded list view (event count, inbox, Run statuses) bound to a
  byte size of the transcript.  This is a shortcut, never an authority: the
  byte size says exactly how stale it is, and a version or size mismatch sends
  the Session back to the log.

Keeping both in one file is what makes a Session directory self-describing and
individually removable; keeping them under separate version keys is what stops
a shape change in the folded row from destroying the identity.  Writes merge
into whatever is on disk and replace the file atomically, so the two writers
(Session identity, Run rows) never clobber each other and a crash can only
leave a projection behind the log, never ahead of it.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading

logger = logging.getLogger(__name__)

#: The only file a Session directory keeps beside its transcript.
FILE_NAME = "projection.json"

#: Bump when the ``record`` *shape* changes.  The record is authority, so a
#: version it cannot read is an unreadable Session rather than a discard.
RECORD_VERSION = 1

#: Bump when the ``row`` *shape* changes.  An older row is dropped whole; the
#: Session then refolds from its log on first touch.
ROW_VERSION = 1

#: Per-unit versions.  A unit whose ``ver`` differs from the live version is
#: discarded at read time and that unit refolds, so a unit can change shape
#: without invalidating its neighbours.
UNIT_VERSIONS: dict[str, int] = {
    "count": 1,
    "list": 1,
    "inbox": 1,
    "runs": 1,
}

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

#: One lock for the whole process: projections are written rarely, and the
#: alternative is two writers racing a read-merge-write on the same file.
_WRITE_LOCK = threading.Lock()

#: Marks a projection part a caller does not own: it is preserved as-is.
UNSET = object()
_UNSET = UNSET


def session_id_of(value: object) -> str:
    """Validate one Session id, which is also its directory name."""

    selected = str(value).strip()
    if _SESSION_ID.fullmatch(selected) is None:
        raise ValueError(f"invalid session_id: {value!r}")
    return selected


def projection_path(sessions_root: Path, session_id: str) -> Path:
    return Path(sessions_root) / session_id_of(session_id) / FILE_NAME


def read_projection(path: Path) -> dict[str, object] | None:
    """Read one projection file.  Anything unusable counts as absent."""

    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - a read model never fails a read
        logger.warning("Session projection is unreadable (%s): %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Session projection is not an object: %s", path)
        return None
    return raw


def record_from(document: Mapping[str, object] | None) -> dict[str, object] | None:
    """The Session identity carried by one projection document."""

    record = document.get("record") if isinstance(document, Mapping) else None
    if not isinstance(record, Mapping) or record.get("ver") != RECORD_VERSION:
        return None
    return dict(record)


def row_from(document: Mapping[str, object] | None) -> dict[str, object] | None:
    """The folded list row carried by one projection document."""

    row = document.get("row") if isinstance(document, Mapping) else None
    if not isinstance(row, Mapping) or row.get("ver") != ROW_VERSION:
        return None
    return dict(row)


def write_projection(
    path: Path,
    *,
    record: object = _UNSET,
    row: object = _UNSET,
) -> bool:
    """Merge the given parts into one projection file, atomically.

    Callers pass only the part they own; everything else on disk is preserved,
    which is what lets the Session identity and the folded Run rows share one
    file.  ``None`` removes a part.  Returns whether the file changed.
    """

    selected = Path(path)
    staged: str | None = None
    with _WRITE_LOCK:
        document = read_projection(selected) or {}
        changed = False
        if record is not _UNSET:
            changed |= _set_part(document, "record", record)
        if row is not _UNSET:
            changed |= _set_part(document, "row", row)
        if not changed:
            return False
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        selected.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(prefix=".projection-", dir=selected.parent)
        staged = name
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, selected)
            directory = os.open(selected.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if staged is not None:
                Path(staged).unlink(missing_ok=True)
    return True


def _set_part(document: dict[str, object], key: str, value: object) -> bool:
    if value is None:
        return document.pop(key, None) is not None
    payload = dict(value)  # type: ignore[arg-type]
    if document.get(key) == payload:
        return False
    document[key] = payload
    return True


def unit_row(key: str, *, seq: int, value: object) -> dict[str, object]:
    return {"ver": UNIT_VERSIONS[key], "seq": int(seq), "val": value}


class SessionProjections:
    """The in-memory mirror of the projection files under one Session root.

    ``load()`` reads every projection once so a listing needs no transcript
    I/O; a Session's own first touch then replaces the seeded rows with the
    log's answer.  Writes stage only the part this instance owns, so a flusher
    that owns rows never rewrites the identity another writer owns, and a
    deleted Session directory is never resurrected with a half projection.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self._documents: dict[str, dict[str, object]] = {}
        self._staged: dict[str, set[str]] = {}

    # --- read side ---------------------------------------------------------

    def load(self) -> None:
        """Read every Session's projection.  Unusable files count as absent."""

        documents: dict[str, dict[str, object]] = {}
        try:
            paths = sorted(self.root.glob(f"*/{FILE_NAME}"))
        except OSError as exc:
            logger.warning("Session projections are unreadable: %s", exc)
            return
        for path in paths:
            try:
                session_id = session_id_of(path.parent.name)
            except ValueError:
                continue
            if session_id in self._staged:
                documents[session_id] = self._documents.get(session_id, {})
                continue
            document = read_projection(path)
            if document is not None:
                documents[session_id] = document
        self._documents = documents

    def documents(self) -> Mapping[str, Mapping[str, object]]:
        return self._documents

    def row(self, session_id: str) -> Mapping[str, object] | None:
        return row_from(self._documents.get(str(session_id)))

    def record(self, session_id: str) -> Mapping[str, object] | None:
        return record_from(self._documents.get(str(session_id)))

    def rows(self) -> Mapping[str, Mapping[str, object]]:
        return {
            session_id: row
            for session_id, document in self._documents.items()
            if (row := row_from(document)) is not None
        }

    def row_is_current(self, session_id: str) -> bool:
        """Whether every unit in the row is readable at its live version.

        Freshness is a property of the whole row — how many log bytes it was
        folded from — but each unit is only usable at its own version.  A unit
        left behind by a version bump therefore sends the row back to the log
        instead of answering from it: a current byte size in one column is no
        evidence for the shape of another.
        """

        row = self.row(session_id)
        if row is None:
            return False
        units = row.get("units")
        if not isinstance(units, Mapping):
            return False
        for key, live_version in UNIT_VERSIONS.items():
            unit = units.get(key)
            if not isinstance(unit, Mapping) or unit.get("ver") != live_version:
                return False
        return True

    def unit(self, session_id: str, key: str) -> object | None:
        """One unit's value, or ``None`` when the row cannot be trusted."""

        row = self.row(session_id)
        if row is None:
            return None
        units = row.get("units")
        if not isinstance(units, Mapping):
            return None
        unit = units.get(key)
        if not isinstance(unit, Mapping):
            return None
        if unit.get("ver") != UNIT_VERSIONS.get(key):
            return None
        return unit.get("val")

    @property
    def dirty(self) -> bool:
        return bool(self._staged)

    # --- write side --------------------------------------------------------

    def stage_record(self, session_id: str, record: Mapping[str, object]) -> None:
        """Stage a Session identity for the next flush."""

        selected = session_id_of(session_id)
        document = self._documents.setdefault(selected, {})
        document["record"] = {"ver": RECORD_VERSION, **dict(record)}
        self._staged.setdefault(selected, set()).add("record")

    def stage_row(self, session_id: str, row: Mapping[str, object]) -> None:
        """Stage one Session's folded list row for the next flush."""

        selected = session_id_of(session_id)
        document = self._documents.setdefault(selected, {})
        document["row"] = {"ver": ROW_VERSION, **dict(row)}
        self._staged.setdefault(selected, set()).add("row")

    def forget(self, session_id: str) -> None:
        """Drop one Session's projection, staged or on disk."""

        selected = str(session_id)
        self._documents.pop(selected, None)
        self._staged.pop(selected, None)

    def flush(self) -> bool:
        """Rewrite every staged projection file.  Returns whether any changed."""

        written = False
        for session_id in tuple(self._staged):
            staged = self._staged.get(session_id) or set()
            document = self._documents.get(session_id) or {}
            if "record" not in staged and not (self.root / session_id).is_dir():
                # A Session deleted while this row was staged: writing it back
                # would leave a half projection in a directory with no log.
                self._staged.pop(session_id, None)
                continue
            try:
                changed = write_projection(
                    self.root / session_id / FILE_NAME,
                    record=document["record"] if "record" in staged else UNSET,
                    row=document["row"] if "row" in staged else UNSET,
                )
            except OSError as exc:
                # Fail-soft: the next flush self-heals, and a crash can only
                # leave the projection behind its log.
                logger.warning("Session projection write failed (%s): %s", session_id, exc)
                continue
            written |= changed
            self._staged.pop(session_id, None)
        return written


__all__ = [
    "FILE_NAME",
    "UNSET",
    "RECORD_VERSION",
    "ROW_VERSION",
    "UNIT_VERSIONS",
    "SessionProjections",
    "projection_path",
    "read_projection",
    "record_from",
    "row_from",
    "session_id_of",
    "unit_row",
    "write_projection",
]
