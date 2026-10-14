"""Read one workspace image with a vision-capable model.

The chat model of an Expert is not always able to accept image input, so an
uploaded image is delivered to the model as a workspace path (see
``RunManager._prepare_prompt``) and this tool turns that path into an answer.
CowAgent resolves its vision provider the same way: the main model first when
it is known to accept images, otherwise the explicitly configured vision model.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from bianbu_agent.agents import ManifestAgentStore
from bianbu_agent.home import AgentHome
from bianbu_agent.models import YAMLModelResolver
from bianbu_agent.models.vision import (
    IMAGE_MEDIA_TYPES,
    IMAGE_UNSUPPORTED_MESSAGE,
    IMAGE_UNSUPPORTED_MESSAGE_EN,
    MAX_IMAGE_BYTES,
    MODEL_DOES_NOT_SUPPORT_IMAGES,
    resolve_image_model,
)
from bianbu_agent.tools import tool


DEFAULT_QUESTION = "Describe this image in detail."


def _resolve(workspace: Path, raw_path: str) -> Path:
    if not raw_path:
        raise ValueError("path must be a non-empty string")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise PermissionError("path is outside the Tool workspace")
    return resolved


async def _agent_model(context) -> tuple[str, str]:
    """Return the (provider, model) selected by the calling Agent, if any."""

    home = AgentHome(context.home_root)
    try:
        spec = await ManifestAgentStore(home).get(context.call.agent_id)
    except (KeyError, ValueError, OSError):
        return "", ""
    selected = getattr(spec, "model", None)
    if selected is None:
        return "", ""
    return str(selected.provider), str(selected.name)


async def _describe(resolved_path: Path, media_type: str, question: str, model: object) -> str:
    from langchain_core.messages import HumanMessage

    from bianbu_agent.engines.default import to_langchain_model

    encoded = base64.b64encode(resolved_path.read_bytes()).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": question or DEFAULT_QUESTION},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{encoded}"},
            },
        ]
    )
    reply = await to_langchain_model(model).ainvoke([message])
    content = getattr(reply, "content", reply)
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return str(content)


def create_tool(context):

    @tool(parse_docstring=True)
    async def read_image(path: str, question: str = "") -> str:
        """Read an image file from the workspace and answer a question about it.

        Use this for any image the user attached or that already exists in the
        workspace: the image is not visible to you directly, so describe what
        you need to know and this tool returns the visual answer. Requires a
        vision-capable model; when none is configured the tool reports
        MODEL_DOES_NOT_SUPPORT_IMAGES instead of guessing.

        Args:
            path: Absolute or workspace-relative path to the image file.
            question: What to ask about the image. Defaults to a full description.
        """
        try:
            resolved_path = _resolve(context.workspace_root, path)
        except PermissionError as exc:
            return json.dumps(
                {"success": False, "code": "path_outside_workspace", "error": str(exc)},
                ensure_ascii=False,
            )
        except (OSError, TypeError, ValueError) as exc:
            return json.dumps(
                {"success": False, "code": "invalid_request", "error": str(exc)},
                ensure_ascii=False,
            )

        suffix = resolved_path.suffix.lower().lstrip(".")
        media_type = IMAGE_MEDIA_TYPES.get(suffix)
        if media_type is None:
            return json.dumps(
                {
                    "success": False,
                    "code": "unsupported_image_type",
                    "error": (
                        f"Unsupported image type: {suffix or '(none)'}. "
                        f"Supported: {', '.join(sorted(IMAGE_MEDIA_TYPES))}."
                    ),
                },
                ensure_ascii=False,
            )
        if not resolved_path.is_file():
            return json.dumps(
                {"success": False, "code": "image_not_found", "error": f"File not found: {path}"},
                ensure_ascii=False,
            )
        size = resolved_path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return json.dumps(
                {
                    "success": False,
                    "code": "image_too_large",
                    "error": f"{size} bytes exceeds the {MAX_IMAGE_BYTES}-byte image limit.",
                },
                ensure_ascii=False,
            )

        provider, model_name = await _agent_model(context)
        resolver = YAMLModelResolver(AgentHome(context.home_root).models_dir)
        selected = resolve_image_model(resolver, provider=provider, model=model_name)
        if selected is None:
            return json.dumps(
                {
                    "success": False,
                    "code": MODEL_DOES_NOT_SUPPORT_IMAGES,
                    "error": IMAGE_UNSUPPORTED_MESSAGE,
                    "error_en": IMAGE_UNSUPPORTED_MESSAGE_EN,
                },
                ensure_ascii=False,
            )

        try:
            analysis = await _describe(resolved_path, media_type, question, selected)
        except Exception as exc:  # provider/transport failures are user-visible
            return json.dumps(
                {
                    "success": False,
                    "code": "image_read_failed",
                    "error": f"Failed to read the image: {exc}",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "path": resolved_path.relative_to(context.workspace_root).as_posix(),
                "analysis": analysis,
            },
            ensure_ascii=False,
        )

    return read_image
