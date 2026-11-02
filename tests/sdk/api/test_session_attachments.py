"""Console image attachments: vision gate, upload, preview, and prompt note."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from bianbu_agent.agents import AgentModel, AgentSpec, InMemoryAgentRegistry
from bianbu_agent.api.contracts import ProviderCreateRequest
from bianbu_agent.api.models import create_provider_resource
from bianbu_agent.api.attachments import session_image_input_available
from bianbu_agent.config import RuntimeConfig
from bianbu_agent.models import ModelConfig, YAMLModelStore
from bianbu_agent.models.vision import (
    IMAGE_UNSUPPORTED_MESSAGE,
    MODEL_DOES_NOT_SUPPORT_IMAGES,
    ImageSupport,
    image_input_available,
    image_support,
    resolve_image_model,
    sniff_image_media_type,
)
from bianbu_agent.sessions.attachments import (
    MAX_FILE_BYTES,
    attachment_note,
    attachment_preview_url,
    classify_attachment,
    enrich_attachments,
)
from bianbu_agent.plugins import ToolCallContext
from bianbu_agent.runs.manager import _prepare_prompt
from bianbu_agent.server.app import create_app
from bianbu_agent.server.runtime import BianbuAgentRuntime
from bianbu_agent.sessions.inbox import SessionInboxMessage
from bianbu_agent.tools.builtins import load_builtin
from bianbu_agent.tools.models import ToolLoadContext


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MAX_IMAGE_BYTES = 10 * 1024 * 1024
PROVIDER_BODY = {
    "id": "custom:test",
    "label": "Test",
    "base_url": "https://models.example.test/v1",
    "api_key": "test-secret",
}


class _Engine:
    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        yield


@asynccontextmanager
async def _api(root: Path, *, model_name: str = "deepseek-chat"):
    # The Agent's model is materialized at startup, so its Provider credential
    # has to exist before the Runtime starts.
    store = YAMLModelStore(root / ".bianbu-agent" / "models")
    await store.open()
    try:
        await create_provider_resource(store, ProviderCreateRequest(**PROVIDER_BODY))
    finally:
        await store.close()
    agents = (
        AgentSpec(
            id="default",
            name="Default",
            model=AgentModel(provider="custom:test", name=model_name),
        ),
    )
    runtime = BianbuAgentRuntime(
        config=RuntimeConfig(data_dir=root / ".bianbu-agent"),
        default_model=None,
        agent_id="default",
        agent_registry=InMemoryAgentRegistry(agents),
        engine_builder=lambda *args: _Engine(),
    )
    await runtime.start()
    transport = httpx.ASGITransport(app=create_app(runtime, manage_lifespan=False))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/sessions",
                json={"id": "session-1", "agent_id": "default"},
            )
            yield client, runtime
    finally:
        await runtime.close()


def test_image_support_comes_from_declarations_not_name_shapes() -> None:
    # An exact declared alias decides, in either direction.
    assert image_support("deepseek-flash") is ImageSupport.SUPPORTED
    assert image_support("deepseek-v4-pro") is ImageSupport.UNSUPPORTED

    # A name that merely looks like a vision model is not evidence either way.
    # `deepseek-flash` proves a vision-capable model need not say so.
    assert image_support("qwen2.5-vl-7b") is ImageSupport.UNKNOWN
    assert image_support("gpt-4o") is ImageSupport.UNKNOWN
    assert image_support("kimi-k2.6") is ImageSupport.UNKNOWN

    # Nor is an unreviewed alias of a declared family.
    assert image_support("deepseek-flash-2027-01") is ImageSupport.UNKNOWN
    assert image_support("") is ImageSupport.UNKNOWN

    # Unknown must never be reported as the explicit negative.
    assert image_support("unreviewed").is_known is False
    assert image_support("unreviewed").allows_attempt is True
    assert image_support("deepseek-v4-pro").allows_attempt is False


def test_sniff_image_media_type_reads_magic_bytes() -> None:
    assert sniff_image_media_type(PNG) == "image/png"
    assert sniff_image_media_type(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert sniff_image_media_type(b"GIF89a") == "image/gif"
    assert sniff_image_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_media_type(b"%PDF-1.7") is None


def test_attachment_note_names_the_workspace_path_and_tool() -> None:
    note = attachment_note(
        [
            {"file_path": "attachments/a.png", "file_name": "a.png", "file_type": "image"},
            {"file_path": ""},
        ]
    )

    assert "- a.png: attachments/a.png (image)" in note
    assert "read_image" in note
    assert attachment_note([]) == ""


def test_prepare_prompt_appends_the_attachment_note_to_its_own_message() -> None:
    messages = [
        SessionInboxMessage(
            message_id="m1",
            content="look",
            attachments=(
                {"file_path": "attachments/a.png", "file_name": "a.png",
                 "file_type": "image"},
            ),
        ),
        SessionInboxMessage(message_id="m2", content="plain"),
    ]

    prepared = _prepare_prompt(messages, ["SECTION"])

    # The injected context is separated from the user's own words by the
    # current-request heading, exactly as in the single-message branch.
    assert prepared[0].content.startswith(
        "SECTION\n\n## 当前请求\nlook\n\n用户附加了")
    assert "attachments/a.png" in prepared[0].content
    assert prepared[1].content == "plain"
    assert prepared[0].message_id == "m1"


@pytest.mark.asyncio
async def test_an_explicitly_text_only_model_warns_but_still_stores_the_image(
    tmp_path: Path,
) -> None:
    """A model declared text-only warns -- but nothing blocks.

    The bytes still land in the workspace for a Tool or a later model to use.
    Only a declaration of *unsupported* reaches this branch; an undeclared
    model is unknown and keeps the affordance (see the test below).
    """

    async with _api(tmp_path, model_name="deepseek-v4-pro") as (client, runtime):
        support = await client.get("/api/v1/sessions/session-1/vision")
        assert support.status_code == 200
        body = support.json()["data"]
        assert body["available"] is False
        assert body["known"] is True
        assert body["state"] == "unsupported"
        assert body["reason"] == MODEL_DOES_NOT_SUPPORT_IMAGES
        assert body["message"] == IMAGE_UNSUPPORTED_MESSAGE

        uploaded = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("a.png", PNG, "image/png")},
        )
        assert uploaded.status_code == 201
        stored = runtime.home.workspace_dir / uploaded.json()["data"]["file_path"]
        assert stored.read_bytes() == PNG

        prompted = await client.post(
            "/api/v1/sessions/session-1/prompt",
            json={
                "message_id": "m1",
                "content": "what is this",
                "attachments": [
                    {"file_path": uploaded.json()["data"]["file_path"],
                     "file_name": "a.png", "file_type": "image"}
                ],
            },
        )
        assert prompted.status_code == 202


@pytest.mark.asyncio
async def test_an_undeclared_model_is_unknown_and_keeps_the_image_affordance(
    tmp_path: Path,
) -> None:
    """Unknown is not the same claim as unsupported.

    A model the catalog never reviewed may well accept images -- `deepseek-flash`
    would have been refused under the old name heuristic -- so the console keeps
    offering images and the endpoint gets to decide.
    """

    async with _api(tmp_path, model_name="deepseek-flash-2027-01") as (client, runtime):
        support = await client.get("/api/v1/sessions/session-1/vision")
        body = support.json()["data"]

        assert body["available"] is True
        assert body["known"] is False
        assert body["state"] == "unknown"
        assert body["reason"] == ""
        assert body["message"] == ""

        uploaded = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("a.png", PNG, "image/png")},
        )
        assert uploaded.status_code == 201


@pytest.mark.asyncio
async def test_vision_capability_binding_overrides_a_text_only_chat_model(
    tmp_path: Path,
) -> None:
    async with _api(tmp_path) as (client, runtime):
        await runtime.model_configs.create_model_resource(
            ModelConfig(
                id="vision-model",
                provider="custom:test",
                model="deepseek-v4-flash-vision-exp",
            )
        )
        await runtime.model_configs.upsert_capability_binding(
            capability="vision",
            model_id="vision-model",
            options={},
        )

        support = await client.get("/api/v1/sessions/session-1/vision")

        assert support.json()["data"]["available"] is True
        assert await session_image_input_available(runtime, "session-1") is True


def test_image_input_available_starts_from_the_explicit_binding() -> None:
    bound = object()

    class Resolver:
        def resolve_capability(self, name):
            assert name == "vision"
            return bound

        def resolve_agent_model(self, selected):  # pragma: no cover - must not run
            raise AssertionError("the explicit capability must win")

    resolver = Resolver()
    assert resolve_image_model(resolver, provider="custom:test", model="gpt-4o") is bound
    assert image_input_available(resolver, provider="custom:test", model="deepseek-chat")


@pytest.mark.asyncio
async def test_vision_model_accepts_a_previewable_upload(tmp_path: Path) -> None:
    async with _api(tmp_path, model_name="gpt-4o") as (client, runtime):
        uploaded = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("photo.png", PNG, "image/png")},
        )

        assert uploaded.status_code == 201
        data = uploaded.json()["data"]
        assert data["file_type"] == "image"
        assert data["file_name"] == "photo.png"
        assert data["file_path"].startswith("attachments/")
        stored = runtime.home.workspace_dir / data["file_path"]
        assert stored.is_file()
        assert data["preview_url"] == (
            f"/api/v1/sessions/session-1/attachments/{stored.name}"
        )
        # The stored path is workspace-relative, so the read_image Tool resolves
        # the very same file from its own workspace root.
        assert (runtime.home.workspace_dir / data["file_path"]).read_bytes() == PNG

        preview = await client.get(data["preview_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/png"
        assert preview.content == PNG

        oversized = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("big.png", PNG + b"0" * MAX_IMAGE_BYTES, "image/png")},
        )
        assert oversized.status_code == 413

        missing = await client.get("/api/v1/sessions/session-1/attachments/nope.png")
        assert missing.status_code == 404


def test_preview_url_is_derived_from_the_stored_path_never_trusted() -> None:
    stored = "attachments/0123456789abcdef0123456789abcdef.png"

    assert attachment_preview_url("s1", stored) == (
        "/api/v1/sessions/s1/attachments/0123456789abcdef0123456789abcdef.png"
    )
    # A directory, an unknown name and an empty path have no single-file preview.
    assert attachment_preview_url("s1", "attachments/deadbeef/proj") == ""
    assert attachment_preview_url("s1", "attachments/not-a-stored-name.png") == ""
    assert attachment_preview_url("s1", "") == ""

    enriched = enrich_attachments(
        "s1",
        [
            {"file_path": stored, "file_name": "a.png", "file_type": "image",
             "preview_url": "https://evil.example.test/x.png"},
            {"file_path": "attachments/deadbeef/proj", "file_name": "proj",
             "file_type": "directory", "file_count": 2},
            {"file_path": "/etc/passwd", "file_name": "passwd", "file_type": "file"},
            "not-a-mapping",
        ],
    )
    # The image gets a server-derived preview, not the client's URL...
    assert enriched[0]["preview_url"] == (
        "/api/v1/sessions/s1/attachments/0123456789abcdef0123456789abcdef.png"
    )
    # ...a directory does not, and a foreign path is never served...
    assert "preview_url" not in enriched[1]
    assert "preview_url" not in enriched[2]
    assert enriched[1]["file_count"] == 2
    # ...and a non-mapping entry is dropped rather than crashing the replay.
    assert len(enriched) == 3


def test_replayed_events_get_the_preview_back(
    tmp_path: Path,
) -> None:
    """A Session logged before the server derived previews still shows images."""

    from bianbu_agent.sessions.events import (
        SessionEvent,
        session_event_to_dict,
    )

    stored = "attachments/0123456789abcdef0123456789abcdef.png"
    event = SessionEvent(
        session_id="s1",
        seq=3,
        type="user/message",
        time=1.0,
        run_id="r1",
        data={
            "message_id": "m1",
            "content": "look",
            "attachments": [
                {"file_path": stored, "file_name": "a.png", "file_type": "image"}
            ],
        },
    )

    replayed = session_event_to_dict(event)["data"]["attachments"][0]

    assert replayed["preview_url"] == (
        "/api/v1/sessions/s1/attachments/0123456789abcdef0123456789abcdef.png"
    )


def test_classify_attachment_reads_the_kind_it_can_trust() -> None:
    assert classify_attachment("a.PNG") == "image"
    assert classify_attachment("clip.MP4") == "video"
    assert classify_attachment("notes.txt") == "file"
    assert classify_attachment("noext") == "file"
    # A sniffed media type wins over a misleading name.
    assert classify_attachment("a.txt", media_type="image/png") == "image"


@pytest.mark.asyncio
async def test_any_file_and_a_whole_directory_can_be_uploaded(tmp_path: Path) -> None:
    """Files and directories reach the workspace, typed for the model note."""

    async with _api(tmp_path) as (client, runtime):
        document = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("notes.pdf", b"%PDF-1.7 hello", "application/pdf")},
        )
        assert document.status_code == 201
        stored = document.json()["data"]
        assert stored["file_type"] == "file"
        assert stored["file_name"] == "notes.pdf"
        assert stored["file_path"].startswith("attachments/")
        assert stored["file_path"].endswith(".pdf")
        assert (runtime.home.workspace_dir / stored["file_path"]).read_bytes() == b"%PDF-1.7 hello"
        served = await client.get(stored["preview_url"])
        assert served.status_code == 200
        assert served.content == b"%PDF-1.7 hello"

        video = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
        )
        assert video.status_code == 201
        assert video.json()["data"]["file_type"] == "video"

        directory = await client.post(
            "/api/v1/sessions/session-1/attachments",
            data={"upload_id": "dir_1", "relative_paths": ["a/one.txt", "a/b/two.txt"]},
            files=[
                ("files", ("one.txt", b"one", "text/plain")),
                ("files", ("two.txt", b"two", "text/plain")),
            ],
        )
        assert directory.status_code == 201
        folder = directory.json()["data"]
        assert folder["file_type"] == "directory"
        assert folder["file_name"] == "a"
        assert folder["file_count"] == 2
        root = runtime.home.workspace_dir / folder["file_path"]
        assert (root / "one.txt").read_bytes() == b"one"
        assert (root / "b" / "two.txt").read_bytes() == b"two"

        escaped = await client.post(
            "/api/v1/sessions/session-1/attachments",
            data={"upload_id": "dir_2", "relative_paths": ["../evil.txt"]},
            files=[("files", ("evil.txt", b"nope", "text/plain"))],
        )
        assert escaped.status_code == 422

        oversized = await client.post(
            "/api/v1/sessions/session-1/attachments",
            files={"file": ("big.bin", b"0" * (MAX_FILE_BYTES + 1), "application/octet-stream")},
        )
        assert oversized.status_code == 413


@pytest.mark.asyncio
async def test_image_tool_refuses_before_reading_when_the_model_cannot_see(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "attachments").mkdir(parents=True)
    (workspace / "attachments" / "a.png").write_bytes(PNG)
    (tmp_path / "outside.png").write_bytes(PNG)
    context = ToolLoadContext(workspace_root=workspace, services={"home_root": tmp_path})
    contribution = load_builtin("read_image", context=context)
    call = ToolCallContext(run_id="run", session_id="session", agent_id="default")

    unsupported = json.loads(
        (await contribution.handler(call, {"path": "attachments/a.png"})).content
    )
    assert unsupported["code"] == MODEL_DOES_NOT_SUPPORT_IMAGES
    assert unsupported["error"] == IMAGE_UNSUPPORTED_MESSAGE

    outside = json.loads(
        (await contribution.handler(call, {"path": str(tmp_path / "outside.png")})).content
    )
    assert outside["code"] == MODEL_DOES_NOT_SUPPORT_IMAGES
    assert outside["error"] == IMAGE_UNSUPPORTED_MESSAGE

    wrong_type = json.loads(
        (await contribution.handler(call, {"path": "attachments/a.txt"})).content
    )
    assert wrong_type["code"] == "unsupported_image_type"


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_tokens,expected", [
    (254, "delivered"),   # an encoded image adds hundreds of tokens
    (31, "refused"),      # no larger than the text alone: the block was dropped
    (None, "unreported"),  # the route does not report usage
])
async def test_an_unknown_model_is_judged_by_what_the_route_actually_did(
    prompt_tokens, expected, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attempt decides, and a silently dropped image is not a success.

    `deepseek-v4-pro` accepts an image block, discards it, and answers as if
    none was sent, so an unknown model cannot be trusted to report the truth.
    """

    from bianbu_agent.models import vision as vision_module
    from bianbu_agent.tools.vision import read_image as tool_module

    workspace = tmp_path / "workspace"
    (workspace / "attachments").mkdir(parents=True)
    (workspace / "attachments" / "a.png").write_bytes(PNG)
    context = ToolLoadContext(workspace_root=workspace, services={"home_root": tmp_path})

    async def no_agent_model(_context):
        return "custom:test", "unreviewed-model"

    async def fake_describe(_path, _media_type, _question, _model):
        return "a red square", prompt_tokens

    class _Resolver:
        def resolve_capability(self, _name):
            return None

        def resolve_agent_model(self, selected):
            return selected

    monkeypatch.setattr(tool_module, "_agent_model", no_agent_model)
    monkeypatch.setattr(tool_module, "YAMLModelResolver", lambda _root: _Resolver())
    monkeypatch.setattr(tool_module, "_describe", fake_describe)

    contribution = load_builtin("read_image", context=context)
    call = ToolCallContext(run_id="run", session_id="session", agent_id="default")
    payload = json.loads(
        (await contribution.handler(call, {"path": "attachments/a.png"})).content
    )

    if expected == "refused":
        assert payload["success"] is False
        assert payload["code"] == MODEL_DOES_NOT_SUPPORT_IMAGES
        assert payload["image_delivered"] is False
    else:
        assert payload["success"] is True
        assert payload["image_delivered"] is (True if expected == "delivered" else None)


@pytest.mark.asyncio
async def test_image_tool_describes_the_image_with_the_bound_vision_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bianbu_agent.models import YAMLModelStore
    from bianbu_agent.tools.vision import read_image as tool_module

    workspace = tmp_path / "workspace"
    (workspace / "attachments").mkdir(parents=True)
    (workspace / "attachments" / "a.png").write_bytes(PNG)
    context = ToolLoadContext(workspace_root=workspace, services={"home_root": tmp_path})
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    try:
        await store.upsert_project_capability(
            capability="vision",
            model="deepseek-v4-flash-vision-exp",
            base_url="https://models.example.test/v1",
            api_key="test-secret",
        )
    finally:
        await store.close()

    seen: list[object] = []

    async def fake_describe(path, media_type, question, model):
        seen.append((path.name, media_type, question, getattr(model, "model", model)))
        return "a red square", None

    monkeypatch.setattr(tool_module, "_describe", fake_describe)
    contribution = load_builtin("read_image", context=context)
    call = ToolCallContext(run_id="run", session_id="session", agent_id="default")

    payload = json.loads(
        (
            await contribution.handler(
                call, {"path": "attachments/a.png", "question": "what colour?"}
            )
        ).content
    )

    assert payload["success"] is True
    assert payload["analysis"] == "a red square"
    assert payload["path"] == "attachments/a.png"
    # The route reported no usage, so delivery cannot be confirmed either way.
    assert payload["image_delivered"] is None
    assert seen == [("a.png", "image/png", "what colour?", "deepseek-v4-flash-vision-exp")]


async def _vision_workspace(tmp_path: Path, base_url: str):
    from bianbu_agent.models import YAMLModelStore

    workspace = tmp_path / "workspace"
    (workspace / "attachments").mkdir(parents=True)
    (workspace / "attachments" / "a.png").write_bytes(PNG)
    store = YAMLModelStore(tmp_path / "models")
    await store.open()
    try:
        await store.upsert_project_capability(
            capability="vision",
            model="vision-model",
            base_url=base_url,
            api_key="test-secret",
        )
    finally:
        await store.close()
    return workspace


@pytest.mark.asyncio
async def test_image_tool_sends_to_a_remote_vision_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remote endpoint is a normal target, not a blocked one.

    Image bytes cannot be text-scanned, but that is why the operator's model
    selection decides where they go: refusing every non-loopback endpoint made
    the shipped Bibit provider unable to read images at all.
    """

    from bianbu_agent.security import security_runtime
    from bianbu_agent.tools.vision import read_image as tool_module

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(
        security_runtime,
        "security_client_call",
        lambda method, *args: {"mode": "normal"},
    )
    workspace = await _vision_workspace(tmp_path, "https://models.example.test/v1")
    context = ToolLoadContext(workspace_root=workspace, services={"home_root": tmp_path})
    called: list[object] = []

    async def fake_describe(*args):
        called.append(args)
        return "a red square", 254

    monkeypatch.setattr(tool_module, "_describe", fake_describe)
    contribution = load_builtin("read_image", context=context)
    call = ToolCallContext(run_id="run", session_id="session", agent_id="default")

    payload = json.loads(
        (await contribution.handler(call, {"path": "attachments/a.png"})).content
    )

    assert payload["success"] is True
    assert payload["analysis"] == "a red square"
    assert payload["image_delivered"] is True
    assert len(called) == 1


@pytest.mark.asyncio
async def test_image_tool_masks_the_question_for_a_local_vision_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bianbu_agent.security import security_runtime
    from bianbu_agent.tools.vision import read_image as tool_module

    secret = "13824681357"
    scans: list[tuple[str, str]] = []

    def call(method, *args):
        if method == "get_agent_security_mode":
            return {"mode": "normal"}
        assert method == "dlp_scan"
        agent_id, text = args
        scans.append((agent_id, text))
        start = text.find(secret)
        return {"findings": [] if start < 0 else [dict(
            start=start,
            end=start + len(secret),
            pii_type="phone",
            action="mask",
        )]}

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setattr(security_runtime, "security_client_call", call)
    workspace = await _vision_workspace(tmp_path, "http://127.0.0.1:11434/v1")
    context = ToolLoadContext(workspace_root=workspace, services={"home_root": tmp_path})
    seen: list[str] = []

    async def fake_describe(_path, _media_type, question, _model):
        seen.append(question)
        return "a red square", None

    monkeypatch.setattr(tool_module, "_describe", fake_describe)
    contribution = load_builtin("read_image", context=context)
    call_context = ToolCallContext(
        run_id="run", session_id="session", agent_id="default")

    payload = json.loads(
        (
            await contribution.handler(
                call_context,
                {"path": "attachments/a.png", "question": f"证件号 {secret}"},
            )
        ).content
    )

    assert payload["success"] is True
    assert secret not in seen[0]
    # The model sees a natural stand-in that this Session can resolve back.
    from bianbu_agent.security.security_dlp import resolve_references

    restored, count = resolve_references(seen[0])
    assert count == 1
    assert secret in restored
    assert any(agent_id == "default" and secret in text for agent_id, text in scans)
