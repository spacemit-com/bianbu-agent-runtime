"""Which configured model can read an image, and whether any exists at all.

DeepSeek Harness decides from ``LlmModelInfo.inputModalities``: a prompt that
carries an image is rejected with ``MODEL_DOES_NOT_SUPPORT_IMAGES`` only when
the model declares its modalities and ``image`` is not among them.  An absent
field means *unknown*, which is not the same claim as an explicit negative.

Runtime records the same decision in a declaration, never in a model name.  A
name is not evidence: ``deepseek-flash`` accepts images while its name says
nothing about them, and ``deepseek-v4-pro`` reads like a stronger sibling while
being text-only.  Guessing from names therefore fails in both directions.

Three declaration sources, strongest first:

1. an explicit ``capability-vision`` binding -- the operator's declaration of
   "this is the model that reads images for this device", and the only way to
   override a catalog entry;
2. the installed model catalog, an exact-alias table of reviewed capabilities;
3. nothing -- ``UNKNOWN``, which callers decide how to treat.

``UNKNOWN`` is deliberately not folded into ``UNSUPPORTED``.  Refusing an image
the endpoint actually accepts hides a working feature with no way for the user
to recover; attempting one the endpoint rejects costs a single failed request
and an accurate error message.  The read path therefore attempts on unknown and
reports what actually happened.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
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


class ImageSupport(Enum):
    """What is known about one model's image input, not merely a yes/no.

    ``UNKNOWN`` exists so a missing declaration never acquires the meaning of a
    negative one.  Callers that must decide keep the distinction; callers that
    only warn can treat unknown as "try it".
    """

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"

    @property
    def is_known(self) -> bool:
        return self is not ImageSupport.UNKNOWN

    @property
    def allows_attempt(self) -> bool:
        """Whether an image may be sent without contradicting a declaration."""

        return self is not ImageSupport.UNSUPPORTED


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

#: Model identities whose endpoint accepts image input, keyed by exact alias.
#: Membership is a reviewed declaration, never a name pattern: an alias that
#: is absent stays unknown instead of being guessed either way.
_DECLARED_IMAGE_MODELS: Mapping[str, ImageSupport] = MappingProxyType(
    {
        # DeepSeek's current OpenAI-compatible endpoint, verified against
        # api.deepseek.com on 2026-10-28 by comparing prompt tokens with and
        # without an image. `deepseek-v4-pro` is the explicit negative: it
        # accepts an image block, silently discards it, and answers as if none
        # was sent, so no error surfaces to correct a guess.
        "deepseek-flash": ImageSupport.SUPPORTED,
        "deepseek-v4-flash": ImageSupport.SUPPORTED,
        "deepseek-v4-flash-vision-exp": ImageSupport.SUPPORTED,
        "deepseek-chat": ImageSupport.SUPPORTED,
        "deepseek-reasoner": ImageSupport.SUPPORTED,
        "deepseek-v4-pro": ImageSupport.UNSUPPORTED,
    }
)


def declared_image_support(model_name: object) -> ImageSupport:
    """Return the reviewed declaration for one exact model alias.

    Exact alias only.  A version, quantization or deployment suffix the catalog
    did not review is ``UNKNOWN``, because the reviewed entry says nothing
    about it.
    """

    name = str(model_name or "").strip().casefold()
    if not name:
        return ImageSupport.UNKNOWN
    return _DECLARED_IMAGE_MODELS.get(name, ImageSupport.UNKNOWN)


def image_support(model_name: object) -> ImageSupport:
    """Return what is known about image input without binding a model.

    Used by callers that only warn, such as the console attachment picker, and
    by the read path before it resolves the exact model.
    """

    return declared_image_support(model_name)


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
    """Return the model that should read an image, or ``None`` when it cannot.

    ``resolver`` is a ``YAMLModelResolver``-shaped object: ``resolve_capability``
    for the explicit binding and ``resolve_agent_model`` for the chat model.

    The explicit binding wins outright.  Otherwise the chat model is used only
    when its declaration permits an attempt: supported, or unknown.  An unknown
    model is returned rather than refused so the failure, if any, is the
    endpoint's own and names the real reason.
    """

    resolve_capability = getattr(resolver, "resolve_capability", None)
    if callable(resolve_capability):
        bound = resolve_capability(VISION_CAPABILITY)
        if bound is not None:
            return bound
    if not image_support(model).allows_attempt:
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
    "ImageSupport",
    "attachment_note",
    "declared_image_support",
    "image_input_available",
    "image_support",
    "resolve_image_model",
    "sniff_image_media_type",
]
