"""Which configured model can read an image, and whether any exists at all.

DeepSeek Harness decides from ``LlmModelInfo.inputModalities``: a prompt that
carries an image is rejected with ``MODEL_DOES_NOT_SUPPORT_IMAGES`` only when
the model declares its modalities and ``image`` is not among them.  A Runtime
provider record has no modality field, so the same decision is made from two
sources instead, cheapest first:

1. an explicit ``capability-vision`` model record -- the operator's declaration
   of "this is the model that reads images for this device";
2. the model name, using the same narrow family list CowAgent keeps in code for
   providers whose OpenAI-compatible endpoint accepts image input.

Order matters: the explicit record wins, so a device that pinned a dedicated
vision model keeps using it even after the chat model changes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from types import MappingProxyType

#: Capability record that binds the model reserved for image understanding.
VISION_CAPABILITY = "vision"

#: Stable reason code, matching the Harness attachment-error contract.
MODEL_DOES_NOT_SUPPORT_IMAGES = "MODEL_DOES_NOT_SUPPORT_IMAGES"

#: User-facing copy.  The Chinese sentence is the product wording; the English
#: one keeps the same meaning for the English console.
IMAGE_UNSUPPORTED_MESSAGE = "当前模型不支持图片,请切换支持图片的模型"
IMAGE_UNSUPPORTED_MESSAGE_EN = (
    "The current model does not support images. Switch to a model that accepts images."
)

#: Media types accepted for an uploaded image, keyed by lowercased suffix.
#: The set matches the Harness image admission list (png/jpeg/webp/gif).
IMAGE_MEDIA_TYPES: Mapping[str, str] = MappingProxyType(
    {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }
)

#: The same map inverted, so a sniffed media type can name its file suffix.
IMAGE_SUFFIXES: Mapping[str, str] = MappingProxyType(
    {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
)

#: One uploaded image may be this large before it is refused outright.
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: Model-name families whose endpoint accepts image input.  A name is the only
#: signal an OpenAI-compatible ``/models`` response reliably carries.  A false
#: positive stays visible (the provider rejects the request and the tool
#: reports it); a false negative would silently hide the attach affordance, so
#: the table lists whole families rather than one flagship name each.
_IMAGE_MODEL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"(?:^|[-_./])vl(?:[-_./]|$|\d)",
        r"internvl|llava|minicpm[-_.]?v|mllama|pixtral|smolvlm|molmo|ovis|idefics",
        r"vision",
        r"multimodal|omni",
        r"gpt-4o|gpt-4\.1|gpt-5|chatgpt-4o",
        r"claude-(?:3|4|5|sonnet|opus|haiku)",
        r"gemini",
        r"glm-\d+(?:\.\d+)?v",
        r"grok-(?:2|3|4)|^o[34](?:-|$)",
        r"ernie-[\d.]+-vl|hunyuan.*vision|doubao.*vision",
        r"mimo",
    )
)


def supports_image_input(model_name: object) -> bool:
    """Whether a model name is a known image-accepting family."""

    name = str(model_name or "").strip().lower()
    if not name:
        return False
    return any(pattern.search(name) for pattern in _IMAGE_MODEL_PATTERNS)


def sniff_image_media_type(payload: bytes) -> str | None:
    """Identify an image from its magic bytes, or return ``None``.

    The declared upload content type is a client claim; the bytes decide what
    is actually stored and served back.
    """

    data = bytes(payload[:16])
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def resolve_image_model(
    resolver: object,
    *,
    provider: str = "",
    model: str = "",
) -> object | None:
    """Return the model that should read an image, or ``None`` when unset.

    ``resolver`` is a ``YAMLModelResolver``-shaped object: ``resolve_capability``
    for the explicit binding and ``resolve_agent_model`` for the chat model.
    """

    resolve_capability = getattr(resolver, "resolve_capability", None)
    if callable(resolve_capability):
        bound = resolve_capability(VISION_CAPABILITY)
        if bound is not None:
            return bound
    if not supports_image_input(model):
        return None
    resolve_agent_model = getattr(resolver, "resolve_agent_model", None)
    if not callable(resolve_agent_model) or not provider or not model:
        return None
    from bianbu_agent.agents import AgentModel

    return resolve_agent_model(AgentModel(provider=provider, name=model))


def image_input_available(
    resolver: object,
    *,
    provider: str = "",
    model: str = "",
) -> bool:
    """Whether some configured model could read an image for this session."""

    return resolve_image_model(resolver, provider=provider, model=model) is not None


def attachment_note(attachments: object) -> str:
    """Render attached workspace files as the model-facing announcement.

    The model never receives the image bytes, only this note and the tool that
    can read them, so the note has to name the workspace path and the tool.
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
        lines.append(f"- {name}: {path} ({kind})")
    if not lines:
        return ""
    return "\n".join(
        [
            "用户附加了以下工作区文件，请使用当前可用的工作区工具处理"
            "（图片必须用 read_image 工具读取，你无法直接看到图片内容）：",
            *lines,
        ]
    )


__all__ = [
    "IMAGE_SUFFIXES",
    "IMAGE_MEDIA_TYPES",
    "IMAGE_UNSUPPORTED_MESSAGE",
    "IMAGE_UNSUPPORTED_MESSAGE_EN",
    "MAX_IMAGE_BYTES",
    "MODEL_DOES_NOT_SUPPORT_IMAGES",
    "VISION_CAPABILITY",
    "attachment_note",
    "image_input_available",
    "resolve_image_model",
    "sniff_image_media_type",
    "supports_image_input",
]
