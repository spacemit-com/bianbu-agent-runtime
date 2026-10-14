"""Attachment admission: typing, limits, and the model-facing announcement.

An attachment the user adds in the console never reaches the chat model as
bytes.  It is stored in the Agent workspace, its workspace-relative path is
announced to the model, and the model opens it with a workspace Tool: images
through ``read_image`` (which needs a vision-capable model), everything else
through ``read_file``, ``search_files`` or ``terminal``.

Images keep a magic-byte check so the stored suffix always matches the bytes;
every other file is typed by extension, because there is no reliable sniffer
for "any file" and the kind is only a hint to the model either way.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType

from bianbu_agent.models.vision import IMAGE_MEDIA_TYPES

#: The workspace directory the console stores attachments in.
ATTACHMENT_DIR = "attachments"
_ATTACHMENT_DIR = ATTACHMENT_DIR

#: A stored single file: 32 hex characters plus an optional extension.
_STORED_NAME = re.compile(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,12})?")

#: One uploaded non-image file may be this large before it is refused.
MAX_FILE_BYTES = 64 * 1024 * 1024
#: One directory upload may carry this many bytes across all of its files.
MAX_DIRECTORY_BYTES = 256 * 1024 * 1024
#: One directory upload may contain this many files.
MAX_DIRECTORY_FILES = 2000

#: Video suffixes, typed by extension alone: a video is served back to the
#: console as a file chip, and only the model decides whether to open it.
VIDEO_MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "mp4": "video/mp4",
        "m4v": "video/mp4",
        "webm": "video/webm",
        "mov": "video/quicktime",
        "mkv": "video/x-matroska",
        "avi": "video/x-msvideo",
    }
)

VIDEO_SUFFIXES: Mapping[str, str] = MappingProxyType(
    {
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/quicktime": "mov",
        "video/x-matroska": "mkv",
        "video/x-msvideo": "avi",
    }
)

#: Every suffix the console may serve back with a concrete content type.
MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {**IMAGE_MEDIA_TYPES, **VIDEO_MEDIA_TYPES}
)

#: A stored file's extension: short, lowercased, never a path.
_STORED_SUFFIX = re.compile(r"[a-z0-9]{1,12}")

#: A directory name that survives a round trip to the filesystem and back.
_SAFE_DIRECTORY_NAME = re.compile(r"[^\w.\- ]", re.UNICODE)


def normalize_suffix(raw: object) -> str:
    """Return a storable extension (no dot), or ``""`` when unusable."""

    suffix = str(raw or "").strip().lstrip(".").lower()
    return suffix if _STORED_SUFFIX.fullmatch(suffix) else ""


def media_type_for(suffix: str) -> str | None:
    """Content type for a stored file's extension, or ``None`` when unknown."""

    return MEDIA_TYPES.get(str(suffix or "").lower().lstrip("."))


def classify_attachment(name: object, *, media_type: str = "") -> str:
    """The model-facing kind: ``image``, ``video`` or ``file``.

    A sniffed ``media_type`` wins when it is present, so an image keeps its
    true kind even when the client named it something else.
    """

    sniffed = str(media_type or "").lower()
    if sniffed.startswith("image/"):
        return "image"
    if sniffed.startswith("video/"):
        return "video"
    suffix = str(PurePosixPath(str(name or "")).suffix).lstrip(".").lower()
    if suffix in IMAGE_MEDIA_TYPES:
        return "image"
    if suffix in VIDEO_MEDIA_TYPES:
        return "video"
    return "file"


def sanitize_directory_name(raw: object) -> str:
    """A directory root name with separators and control characters removed."""

    name = str(raw or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_DIRECTORY_NAME.sub("_", name).strip(" .")
    return name[:120] or "upload"


def attachment_preview_url(session_id: object, file_path: object) -> str:
    """The console URL for a stored single file, or ``""`` when there is none.

    The URL is derived from the stored name, not echoed from the client: the
    client used to omit it on send, so a replayed Session lost the preview even
    though the bytes were still on disk.  A directory has no single-file
    preview and gets no URL.
    """

    path = PurePosixPath(str(file_path or ""))
    if path.parent.name != _ATTACHMENT_DIR:
        return ""
    name = path.name
    if _STORED_NAME.fullmatch(name) is None:
        return ""
    return f"/api/v1/sessions/{session_id}/attachments/{name}"


def enrich_attachments(session_id: object, attachments: object) -> list[dict[str, object]]:
    """Normalize attachments before they are logged and replayed.

    ``preview_url`` is derived from the stored path, never copied from the
    client: the client used to omit it on send, so a replayed Session lost the
    preview even though the bytes were still on disk.  Deriving it here makes
    the server authoritative and keeps history and live rendering identical.
    """

    enriched: list[dict[str, object]] = []
    for item in attachments or ():
        if not isinstance(item, Mapping):
            continue
        entry = dict(item)
        path = str(entry.get("file_path") or "").strip()
        if not path:
            enriched.append(entry)
            continue
        entry["file_path"] = path
        # A client-supplied URL is dropped either way: it is rebuilt from the
        # path or left absent, never echoed.
        entry.pop("preview_url", None)
        preview = attachment_preview_url(session_id, path)
        if preview:
            entry["preview_url"] = preview
        enriched.append(entry)
    return enriched


def attachment_note(attachments: object) -> str:
    """Render attached workspace files as the model-facing announcement.

    The model never receives the bytes, only this note and the Tools that can
    open them, so the note has to name each workspace path and how to read it.
    """

    lines: list[str] = []
    for item in attachments or ():
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("file_path") or "").strip()
        if not path:
            continue
        name = str(item.get("file_name") or "").strip() or PurePosixPath(path).name
        kind = str(item.get("file_type") or "").strip() or "file"
        count = item.get("file_count")
        if kind == "directory" and isinstance(count, int) and not isinstance(count, bool):
            lines.append(f"- {name}: {path} (directory, {count} files)")
        else:
            lines.append(f"- {name}: {path} ({kind})")
    if not lines:
        return ""
    return "\n".join(
        [
            "用户附加了以下工作区文件，请使用当前可用的工作区工具处理：",
            "- 图片必须用 read_image 工具读取，你无法直接看到图片内容；",
            "- 目录用 search_files 或 terminal 查看内容，其他文件用 read_file 读取。",
            *lines,
        ]
    )


__all__ = [
    "ATTACHMENT_DIR",
    "MAX_DIRECTORY_BYTES",
    "MAX_DIRECTORY_FILES",
    "MAX_FILE_BYTES",
    "MEDIA_TYPES",
    "VIDEO_MEDIA_TYPES",
    "VIDEO_SUFFIXES",
    "attachment_note",
    "attachment_preview_url",
    "classify_attachment",
    "enrich_attachments",
    "media_type_for",
    "normalize_suffix",
    "sanitize_directory_name",
]
