"""Read one workspace image with a vision-capable model.

The chat model of an Expert is not always able to accept image input, so an
uploaded image is delivered to the model as a workspace path (see
``RunManager._prepare_prompt``) and this tool turns that path into an answer.
CowAgent resolves its vision provider the same way: the main model first when
it is known to accept images, otherwise the explicitly configured vision model.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Mapping
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
    ImageSupport,
    image_support,
    resolve_image_model,
)
from bianbu_agent.tools import tool
from bianbu_agent.tools.files.paths import display_path, resolve_path


DEFAULT_QUESTION = "Describe this image in detail."

#: A prompt at or below this size cannot contain an encoded image.  Real image
#: payloads add hundreds of tokens, so the bound only separates "text arrived"
#: from "text plus bytes arrived"; it is not a model-specific threshold.
_TEXT_ONLY_PROMPT_TOKENS = 64


def _security_enabled() -> bool:
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return False
    from bianbu_agent.security.enablement import security_agent_enabled

    return security_agent_enabled()


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


async def _describe(
    resolved_path: Path, media_type: str, question: str, model: object
) -> tuple[str, int | None]:
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
    # A model without image support may accept the block and discard it rather
    # than reject it, so the prompt size is the only signal that the bytes were
    # actually seen.  ``None`` means the route did not report usage.
    usage = getattr(reply, "usage_metadata", None)
    prompt_tokens = None
    if isinstance(usage, Mapping):
        reported = usage.get("input_tokens") or usage.get("prompt_tokens")
        if isinstance(reported, int) and not isinstance(reported, bool):
            prompt_tokens = reported
    content = getattr(reply, "content", reply)
    if isinstance(content, list):
        content = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return str(content), prompt_tokens


def create_tool(context):

    @tool(parse_docstring=True)
    async def read_image(path: str, question: str = "") -> str:
        """Read an image file and answer a question about it.

        Use this for any image the user attached or that already exists as a
        file: the image is not visible to you directly, so describe what you
        need to know and this tool returns the visual answer. Requires a
        vision-capable model; when none is configured the tool reports
        MODEL_DOES_NOT_SUPPORT_IMAGES instead of guessing.

        Args:
            path: Image path; a relative path resolves against the workspace.
            question: What to ask about the image. Defaults to a full description.
        """
        try:
            resolved_path = resolve_path(context.workspace_root, path)
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

        if question and _security_enabled():
            from bianbu_agent.security.security_dlp import scan_text

            question = await asyncio.to_thread(
                scan_text, question, agent_id=context.call.agent_id)

        try:
            analysis, prompt_tokens = await _describe(
                resolved_path, media_type, question, selected
            )
        except Exception as exc:  # provider/transport failures are user-visible
            return json.dumps(
                {
                    "success": False,
                    "code": "image_read_failed",
                    "error": f"Failed to read the image: {exc}",
                },
                ensure_ascii=False,
            )

        # An endpoint that accepts the image block but discards it answers
        # normally, so a caller cannot tell a description from a guess.  When
        # the route reports usage, a prompt no larger than the text alone means
        # the bytes never reached the model; say so instead of returning a
        # confident answer about an image nothing saw.
        declared = image_support(model_name)
        if (
            declared is ImageSupport.UNKNOWN
            and prompt_tokens is not None
            and prompt_tokens <= _TEXT_ONLY_PROMPT_TOKENS
        ):
            return json.dumps(
                {
                    "success": False,
                    "code": MODEL_DOES_NOT_SUPPORT_IMAGES,
                    "error": IMAGE_UNSUPPORTED_MESSAGE,
                    "error_en": IMAGE_UNSUPPORTED_MESSAGE_EN,
                    "image_delivered": False,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "success": True,
                "path": Path(display_path(context.workspace_root, resolved_path)).as_posix(),
                "analysis": analysis,
                "image_delivered": (
                    None if prompt_tokens is None
                    else prompt_tokens > _TEXT_ONLY_PROMPT_TOKENS
                ),
            },
            ensure_ascii=False,
        )

    return read_image
