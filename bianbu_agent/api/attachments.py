"""Session attachments: upload, preview, and vision availability.

An attachment the user adds never reaches the chat model directly.  It is
stored in the Agent workspace, its workspace-relative path is announced to the
model, and the model opens it with a workspace Tool: images through
``read_image``, everything else through ``read_file``/``search_files``.

Nothing here refuses an upload.  A model without vision cannot describe an
image, but the user may still attach one for a later model or for a Tool to
handle, so ``GET .../vision`` only answers the question the console asks to
*warn* the user; the upload and prompt routes accept the bytes either way.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from bianbu_agent.agents import AgentModel
from bianbu_agent.api.errors import success
from bianbu_agent.models import YAMLModelResolver
from bianbu_agent.models.vision import (
    IMAGE_SUFFIXES,
    IMAGE_UNSUPPORTED_MESSAGE,
    IMAGE_UNSUPPORTED_MESSAGE_EN,
    MAX_IMAGE_BYTES,
    MODEL_DOES_NOT_SUPPORT_IMAGES,
    image_input_available,
    sniff_image_media_type,
)
from bianbu_agent.sessions.attachments import (
    ATTACHMENT_DIR,
    MAX_DIRECTORY_BYTES,
    MAX_DIRECTORY_FILES,
    MAX_FILE_BYTES,
    classify_attachment,
    media_type_for,
    normalize_suffix,
    enrich_attachments,
    sanitize_directory_name,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions")

#: Uploads live beside the other workspace files so Agent tools can read them.
_ATTACHMENT_DIR = ATTACHMENT_DIR
#: A stored single file: the uploaded name is normalized, never trusted.
_STORED_FILE = re.compile(r"[0-9a-f]{32}(?:\.[a-z0-9]{1,12})?")
_MAX_DISPLAY_NAME = 200
_CHUNK = 1024 * 1024


def _resolver(runtime) -> YAMLModelResolver:
    return YAMLModelResolver(runtime.home.models_dir)


def _attachments_dir(runtime, *, create: bool) -> Path:
    target = runtime.home.workspace_dir / _ATTACHMENT_DIR
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target


async def _selected_model(runtime, session_id: str) -> AgentModel | None:
    session = await runtime.sessions.get(session_id)
    try:
        agent = await runtime.agents.get(session.agent_id)
    except (KeyError, FileNotFoundError):
        return None
    selected = getattr(agent, "model", None)
    return selected if isinstance(selected, AgentModel) else None


async def session_image_input_available(runtime, session_id: str) -> bool:
    """Whether any configured model could read an image for this session.

    The console asks this to *warn* the user before an image is sent; a
    malformed ``capability-vision`` record must not take the whole console
    down, so a probing failure reads as "unavailable".
    """

    selected = await _selected_model(runtime, session_id)
    provider = selected.provider if selected is not None else ""
    model = selected.name if selected is not None else ""
    try:
        return image_input_available(_resolver(runtime), provider=provider, model=model)
    except Exception:
        logger.warning("Vision capability probe failed", exc_info=True)
        return False


def _display_name(upload, fallback: str) -> str:
    raw = str(getattr(upload, "filename", "") or fallback)
    name = Path(raw).name
    return "".join(
        character for character in name if character.isprintable()
    ).strip()[:_MAX_DISPLAY_NAME] or fallback


async def _read_capped(upload, limit: int) -> bytes:
    """Read one uploaded file, refusing anything larger than ``limit``."""

    payload = b""
    while len(payload) <= limit:
        chunk = await upload.read(min(_CHUNK, limit + 1 - len(payload)))
        if not chunk:
            break
        payload += chunk
    if len(payload) > limit:
        raise HTTPException(413, f"file exceeds {limit} bytes")
    return payload


async def _store_file(runtime, session_id: str, upload) -> JSONResponse:
    head = await upload.read(MAX_IMAGE_BYTES + 1)
    media_type = sniff_image_media_type(head)
    if media_type is not None:
        if len(head) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"image exceeds {MAX_IMAGE_BYTES} bytes")
        payload, suffix = head, IMAGE_SUFFIXES[media_type]
    else:
        payload = head
        while len(payload) <= MAX_FILE_BYTES:
            chunk = await upload.read(min(_CHUNK, MAX_FILE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) > MAX_FILE_BYTES:
            raise HTTPException(413, f"file exceeds {MAX_FILE_BYTES} bytes")
        suffix = normalize_suffix(Path(str(getattr(upload, "filename", "") or "")).suffix)

    stored_name = f"{uuid.uuid4().hex}{'.' + suffix if suffix else ''}"
    display_name = _display_name(upload, stored_name)
    target = _attachments_dir(runtime, create=True) / stored_name
    target.write_bytes(payload)
    logger.info("Attachment uploaded: %s (%s bytes)", stored_name, len(payload))
    return success(
        enrich_attachments(
            session_id,
            [
                {
                    "file_path": f"{_ATTACHMENT_DIR}/{stored_name}",
                    "file_name": display_name,
                    "file_type": classify_attachment(
                        display_name, media_type=media_type or ""
                    ),
                }
            ],
        )[0],
        status_code=201,
    )


def _relative_parts(raw: object) -> list[str]:
    path = PurePosixPath(str(raw or "").replace("\\", "/"))
    parts = [part for part in path.parts if part not in ("", ".", "/")]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(422, "invalid relative path in directory upload")
    return [sanitize_directory_name(part) for part in parts]


async def _store_directory(
    runtime, session_id: str, uploads: list, relative_paths: list
) -> JSONResponse:
    if len(uploads) != len(relative_paths):
        raise HTTPException(422, "directory upload payload mismatch")
    if len(uploads) > MAX_DIRECTORY_FILES:
        raise HTTPException(413, f"directory exceeds {MAX_DIRECTORY_FILES} files")

    root_name = ""
    total = 0
    entries: list[tuple[list[str], bytes]] = []
    for upload, relative in zip(uploads, relative_paths):
        parts = _relative_parts(relative)
        if not root_name:
            root_name = parts[0]
        elif parts[0] != root_name:
            raise HTTPException(422, "directory upload must use a single root folder")
        payload = await _read_capped(upload, MAX_FILE_BYTES)
        total += len(payload)
        if total > MAX_DIRECTORY_BYTES:
            raise HTTPException(413, f"directory exceeds {MAX_DIRECTORY_BYTES} bytes")
        entries.append((parts, payload))

    stored_root = uuid.uuid4().hex
    directory = (_attachments_dir(runtime, create=True) / stored_root).resolve()
    for parts, payload in entries:
        target = directory.joinpath(*parts).resolve()
        if not target.is_relative_to(directory):
            raise HTTPException(422, "invalid relative path in directory upload")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    logger.info("Directory uploaded: %s (%s files)", root_name, len(entries))
    return success(
        {
            "file_path": f"{_ATTACHMENT_DIR}/{stored_root}/{root_name}",
            "file_name": root_name,
            "file_type": "directory",
            "file_count": len(entries),
        },
        status_code=201,
    )


@router.get("/{session_id}/vision")
async def session_vision(session_id: str, request: Request) -> JSONResponse:
    """Answer whether the console may offer an image for this session."""

    runtime = request.app.state.runtime
    await runtime.sessions.get(session_id)
    selected = await _selected_model(runtime, session_id)
    available = await session_image_input_available(runtime, session_id)
    return success(
        {
            "available": available,
            "provider": selected.provider if selected is not None else "",
            "model": selected.name if selected is not None else "",
            "reason": "" if available else MODEL_DOES_NOT_SUPPORT_IMAGES,
            "message": "" if available else IMAGE_UNSUPPORTED_MESSAGE,
            "message_en": "" if available else IMAGE_UNSUPPORTED_MESSAGE_EN,
        }
    )


@router.post("/{session_id}/attachments", status_code=201)
async def upload_attachment(session_id: str, request: Request) -> JSONResponse:
    runtime = request.app.state.runtime
    await runtime.sessions.get(session_id)
    form = await request.form()
    uploads = [item for item in form.getlist("files") if hasattr(item, "read")]
    if uploads:
        return await _store_directory(
            runtime, session_id, uploads, list(form.getlist("relative_paths"))
        )
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "file is required")
    return await _store_file(runtime, session_id, upload)


@router.get("/{session_id}/attachments/{name}")
async def read_attachment(session_id: str, name: str, request: Request) -> Response:
    runtime = request.app.state.runtime
    await runtime.sessions.get(session_id)
    if _STORED_FILE.fullmatch(name) is None:
        raise HTTPException(404, "attachment was not found")
    target = _attachments_dir(runtime, create=False) / name
    if not target.is_file():
        raise HTTPException(404, "attachment was not found")
    media_type = media_type_for(name.rsplit(".", 1)[-1]) or "application/octet-stream"
    return Response(
        content=target.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )


__all__ = ["router", "session_image_input_available"]
