from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
import httpx
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.messages import AIMessage, HumanMessage

from bianbu_agent.capture.llm import (
    MAX_RECORD_BYTES,
    PREVIEW_BYTES,
    ProviderCaptureCallback,
    ProviderCaptureStore,
)
from bianbu_agent.capture import llm as capture_llm


def _record(provider_id: str = "openai", **values: object) -> dict[str, object]:
    request_body = values.pop("input", "hello")
    response_body = values.pop("output", "world")
    return {
        "provider_id": provider_id,
        "status": "success",
        "http_request": {"body": request_body},
        "http_response": {"body": response_body},
        **values,
    }


def _raw_records(root: Path) -> list[bytes]:
    return [gzip.decompress(path.read_bytes()) for path in (root / "records").glob("*.json.gz")]


def _contains_type(value: object, expected: str) -> bool:
    if isinstance(value, dict):
        return value.get("type") == expected or any(
            _contains_type(item, expected) for item in value.values()
        )
    if isinstance(value, list):
        return any(_contains_type(item, expected) for item in value)
    return False


def _nested_mapping(depth: int) -> dict[str, object]:
    value: object = "leaf"
    for _ in range(depth):
        value = {"nested": value}
    assert isinstance(value, dict)
    return value


def test_target_selection_persists_and_stop_clears_it(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)

    assert store.target() is None
    store.select("custom:primary")
    assert ProviderCaptureStore(tmp_path).target() == "custom:primary"

    ProviderCaptureStore(tmp_path).stop()
    assert store.target() is None
    assert not (tmp_path / "target.json").exists()


def test_callback_ignores_calls_without_a_target_and_non_selected_provider(
    tmp_path: Path,
) -> None:
    store = ProviderCaptureStore(tmp_path)
    callback = ProviderCaptureCallback(store)

    callback.on_chat_model_start(
        {"name": "model"},
        [[HumanMessage(content="no target")]],
        run_id=uuid4(),
        metadata={"provider_id": "openai"},
    )
    store.select("openai")
    run_id = uuid4()
    callback.on_chat_model_start(
        {"name": "model"},
        [[HumanMessage(content="wrong provider")]],
        run_id=run_id,
        metadata={"provider_id": "deepseek"},
    )
    callback.on_llm_end(LLMResult(generations=[]), run_id=run_id)

    assert store.list() == []


def test_callback_without_selected_target_creates_no_capture_files(
    tmp_path: Path,
) -> None:
    capture_root = tmp_path / "capture-not-started"
    store = ProviderCaptureStore(capture_root)
    callback = ProviderCaptureCallback(store)
    run_id = uuid4()

    callback.on_chat_model_start(
        {"name": "model"},
        [[HumanMessage(content="must not persist")]],
        run_id=run_id,
        metadata={"provider_id": "openai"},
    )
    callback.on_llm_end(LLMResult(generations=[]), run_id=run_id)

    assert not capture_root.exists()


def test_callback_finishes_selected_in_flight_call_after_target_switch(
    tmp_path: Path,
) -> None:
    store = ProviderCaptureStore(tmp_path)
    store.select("openai")
    callback = ProviderCaptureCallback(store)
    run_id = uuid4()

    callback.on_chat_model_start(
        {"name": "gpt-test"},
        [[HumanMessage(content="request")]],
        run_id=run_id,
        metadata={
            "provider_id": "openai",
            "agent_id": "agent-1",
            "session_id": "session-1",
        },
    )
    store.select("deepseek")
    callback.on_llm_end(
        LLMResult(
            generations=[[ChatGeneration(message=AIMessage(content="response"))]],
            llm_output={"token_usage": {"total_tokens": 2}},
        ),
        run_id=run_id,
    )

    rows = store.list()
    assert len(rows) == 1
    detail = store.get(rows[0]["id"])
    assert detail is not None
    assert detail["provider_id"] == "openai"
    assert detail["model_run_id"] == str(run_id)
    assert detail["status"] == "success"
    assert detail["metadata"]["agent_id"] == "agent-1"
    assert "input" not in detail
    assert "output" not in detail


def test_callback_captures_raw_http_exchange(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    store.select("deepseek")
    callback = ProviderCaptureCallback(store)
    run_id = uuid4()
    callback.on_chat_model_start(
        {"name": "deepseek-v4-flash"},
        [[HumanMessage(content="request")]],
        run_id=run_id,
        metadata={"provider_id": "deepseek"},
    )
    hooks = callback.http_hooks("deepseek")
    # LangGraph may execute the HTTP client in a child context, so the hook
    # must also correlate through the provider-level active-run fallback.
    capture_llm._ACTIVE_CAPTURE_RUN.set(None)
    request = httpx.Request(
        "POST",
        "https://api.deepseek.com/v1/chat/completions",
        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        content=json.dumps({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "request"}],
            "tools": [{"type": "function", "function": {"name": "filesystem_read"}}],
        }).encode(),
    )
    hooks.on_request(request)
    hooks.on_response(httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"choices": [], "usage": {"total_tokens": 1}}',
        request=request,
    ))
    callback.on_llm_end(LLMResult(generations=[]), run_id=run_id)

    row = store.list()[0]
    detail = store.get(row["id"])
    assert detail is not None
    assert detail["http_request"]["method"] == "POST"
    assert detail["http_request"]["body"]["tools"][0]["function"]["name"] == "filesystem_read"
    assert detail["http_request"]["headers"]["authorization"] == "[REDACTED]"
    assert detail["http_response"]["status_code"] == 200
    assert detail["http_response"]["body"]["usage"]["total_tokens"] == 1


def test_callback_preserves_typed_binary_values_until_store_sanitization(
    tmp_path: Path,
) -> None:
    """Changing LangChain payload dumps to JSON mode would leak/drop binary types."""

    store = ProviderCaptureStore(tmp_path)
    store.select("openai")
    callback = ProviderCaptureCallback(store)
    run_id = uuid4()

    callback.on_chat_model_start(
        {"name": "binary-model"},
        [[
            HumanMessage(
                content="request",
                additional_kwargs={
                    "blob": b"\xffinput-secret",
                    b"input-binary-key": "input-key-value",
                },
            )
        ]],
        run_id=run_id,
        metadata={"provider_id": "openai"},
    )
    callback.on_llm_end(
        LLMResult(
            generations=[[
                ChatGeneration(
                    message=AIMessage(
                        content="response",
                        additional_kwargs={
                            "blob": b"\xfeoutput-secret",
                            b"output-binary-key": "output-key-value",
                        },
                    )
                )
            ]],
            llm_output={
                "raw_blob": b"\xfdprovider-secret",
            },
        ),
        run_id=run_id,
    )

    raw = _raw_records(tmp_path)
    assert len(raw) == 1
    assert b"input-secret" not in raw[0]
    assert b"output-secret" not in raw[0]
    assert b"provider-secret" not in raw[0]
    assert b"input-binary-key" not in raw[0]
    assert b"output-binary-key" not in raw[0]
    row = store.list()[0]
    detail = store.get(row["id"])
    assert detail is not None
    assert "input" not in detail
    assert "output" not in detail


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            LLMResult(
                generations=[[
                    ChatGeneration(message=AIMessage(content="response"))
                ]],
                llm_output={
                    "token_usage": {
                        "prompt_tokens": 5,
                        "completion_tokens": 6,
                        "total_tokens": 11,
                        "cached_tokens": 10**100,
                        "reasoning_tokens": True,
                    }
                },
            ),
            {
                "prompt_tokens": 5,
                "completion_tokens": 6,
                "total_tokens": 11,
            },
        ),
        (
            LLMResult(
                generations=[[
                    ChatGeneration(
                        message=AIMessage(
                            content="response",
                            usage_metadata={
                                "input_tokens": 3,
                                "output_tokens": 4,
                                "total_tokens": 7,
                            },
                        )
                    )
                ]],
            ),
            {
                "input_tokens": 3,
                "output_tokens": 4,
                "total_tokens": 7,
            },
        ),
    ],
)
def test_callback_projects_numeric_usage_into_store_and_collection(
    tmp_path: Path,
    response: LLMResult,
    expected: dict[str, int],
) -> None:
    """Removing either real LangChain usage path would hide list token metrics."""

    store = ProviderCaptureStore(tmp_path)
    store.select("openai")
    callback = ProviderCaptureCallback(store)
    run_id = uuid4()
    callback.on_chat_model_start(
        {"name": "usage-model"},
        [[HumanMessage(content="request")]],
        run_id=run_id,
        metadata={"provider_id": "openai"},
    )

    callback.on_llm_end(response, run_id=run_id)

    row = store.list()[0]
    detail = store.get(row["id"])
    assert detail is not None
    assert detail["usage"] == expected
    assert row["usage"] == expected


def test_callback_records_selected_errors_after_stop(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    store.select("openai")
    callback = ProviderCaptureCallback(store)
    run_id = uuid4()

    callback.on_chat_model_start(
        {"name": "gpt-test"},
        [[HumanMessage(content="request")]],
        run_id=run_id,
        metadata={"provider_id": "openai"},
    )
    store.stop()
    callback.on_llm_error(RuntimeError("Bearer secret-error-token"), run_id=run_id)

    detail = store.get(store.list()[0]["id"])
    assert detail is not None
    assert detail["status"] == "error"
    assert detail["error"] == "Bearer [REDACTED]"


def test_public_write_sanitizes_every_header_value_and_secret_key_value(
    tmp_path: Path,
) -> None:
    store = ProviderCaptureStore(tmp_path)
    secrets = {
        "authorization": "Bearer bearer-value-1234567890",
        "trace": "ordinary-header-value",
        "api_key": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "password": "password-value",
    }

    record_id = store.write(
        _record(
            request={
                "headers": {
                    "Authorization": secrets["authorization"],
                    "X-Trace": secrets["trace"],
                },
                "httpHeaders": {"X-Request-ID": "camel-case-header-secret"},
                "api_key": secrets["api_key"],
                "password": secrets["password"],
            }
        )
    )

    assert record_id is not None
    raw = _raw_records(tmp_path)[0].decode("utf-8")
    assert all(secrets[key] not in raw for key in ("authorization", "api_key", "password"))
    assert secrets["trace"] in raw
    detail = store.get(record_id)
    assert detail is not None
    assert detail["request"]["headers"] == {
        "Authorization": "[REDACTED]",
        "X-Trace": secrets["trace"],
    }
    assert detail["request"]["httpHeaders"] == {"X-Request-ID": "camel-case-header-secret"}
    assert detail["request"]["[REDACTED_KEY]"] == "[REDACTED]"


def test_public_write_sanitizes_mapping_keys_and_token_shapes(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    api_key = "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
    opaque = "aB3dE5fG7hJ9kL2mN4pQ6rS8tV0wX1yZ"
    basic = "Basic dXNlcjpwYXNzd29yZA=="

    record_id = store.write(
        _record(
            input={
                api_key: "mapping-key-secret",
                "nested": f"credentials: {jwt} {api_key} {opaque} {basic}",
            }
        )
    )

    raw = _raw_records(tmp_path)[0].decode("utf-8")
    for secret in (jwt, api_key, basic):
        assert secret not in raw
    assert opaque in raw
    detail = store.get(record_id)
    assert detail is not None
    assert detail["http_request"]["body"]["[REDACTED_KEY]"] == "[REDACTED]"


def test_public_write_redacts_nonnumeric_known_usage_value(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    secret = "short-unpatterned-secret"

    record_id = store.write(_record(total_tokens=secret))

    raw = _raw_records(tmp_path)[0].decode("utf-8")
    assert secret not in raw
    detail = store.get(record_id)
    assert detail is not None
    assert detail["total_tokens"] == "[REDACTED]"


def test_public_write_redacts_unknown_nested_token_usage_schema(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    secret = "nested-unpatterned-secret"

    record_id = store.write(_record(token_usage={"raw": secret}))

    raw = _raw_records(tmp_path)[0].decode("utf-8")
    assert secret not in raw
    detail = store.get(record_id)
    assert detail is not None
    assert detail["token_usage"] == "[REDACTED]"


def test_public_write_sanitizes_data_url_mapping_and_header_pair_keys(
    tmp_path: Path,
) -> None:
    store = ProviderCaptureStore(tmp_path)
    mapping_key = "data:text/plain;base64,bWFwcGluZy1rZXktc2VjcmV0"
    header_key = "data:text/plain;base64,aGVhZGVyLWtleS1zZWNyZXQ="

    record_id = store.write(
        _record(
            input={mapping_key: "mapping-value"},
            request={"headers": [(header_key, "header-value")]},
        )
    )

    raw = _raw_records(tmp_path)[0].decode("utf-8")
    assert mapping_key in raw
    assert header_key in raw
    detail = store.get(record_id)
    assert detail is not None
    assert detail["http_request"]["body"] == {mapping_key: "mapping-value"}
    assert detail["request"]["headers"] == [[header_key, "header-value"]]


def test_public_write_replaces_binary_data_urls_and_arbitrary_objects(
    tmp_path: Path,
) -> None:
    class Dangerous:
        def __repr__(self) -> str:
            return "repr-secret-must-never-be-written"

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(
        _record(
            input={
                "blob": b"binary-secret",
                "image": "data:image/png;base64,cGljdHVyZS1zZWNyZXQ=",
                "client": Dangerous(),
            }
        )
    )

    raw = _raw_records(tmp_path)[0].decode("utf-8")
    assert "binary-secret" not in raw
    assert "cGljdHVyZS1zZWNyZXQ=" in raw
    assert "repr-secret-must-never-be-written" not in raw
    detail = store.get(record_id)
    assert detail is not None
    assert detail["http_request"]["body"]["blob"] == {"bytes": 13, "type": "binary"}
    assert detail["http_request"]["body"]["image"] == "data:image/png;base64,cGljdHVyZS1zZWNyZXQ="
    assert detail["http_request"]["body"]["client"] == {
        "python_type": "Dangerous",
        "type": "unsupported",
    }


def test_public_write_replaces_excessive_depth_before_publishing(
    tmp_path: Path,
) -> None:
    """Writer and reader depth policies must never disagree on a stored record."""

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(_record(input=_nested_mapping(70)))

    rows = store.list()
    detail = store.get(record_id)
    assert [row["id"] for row in rows] == [record_id]
    assert detail is not None
    assert _contains_type(detail["http_request"]["body"], "max-depth")


def test_write_validates_the_exact_final_document_before_any_file_is_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future bounding bug cannot publish JSON that the strict reader rejects."""

    import bianbu_agent.capture.llm as capture

    def invalid_final_document(document: dict[str, object]) -> dict[str, object]:
        return {
            "captured_at": document["captured_at"],
            "id": document["id"],
            "input": _nested_mapping(70),
        }

    monkeypatch.setattr(capture, "_bound_document", invalid_final_document)
    store = ProviderCaptureStore(tmp_path)

    with pytest.raises(ValueError, match="unsupported JSON values"):
        store.write(_record())

    assert not (tmp_path / "records").exists()


def test_list_returns_only_four_kib_head_and_tail_previews(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    input_text = "start-" + ("x " * 5000) + "-input-end"
    output_text = "output-start-" + ("y " * 5000) + "-output-end"
    record_id = store.write(
        _record(
            input=input_text,
            output=output_text,
            metadata={"private": "detail-only"},
            invocation_parameters={"temperature": 0.5},
            request={"body": "detail-only"},
        )
    )

    row = store.list()[0]
    assert row["id"] == record_id
    assert "input" not in row
    assert "output" not in row
    assert "metadata" not in row
    assert "invocation_parameters" not in row
    assert "request" not in row
    assert row["input_preview"]["head"] == input_text[:PREVIEW_BYTES]
    assert row["input_preview"]["tail"] == input_text[-PREVIEW_BYTES:]
    assert row["output_preview"]["head"] == output_text[:PREVIEW_BYTES]
    assert row["output_preview"]["tail"] == output_text[-PREVIEW_BYTES:]
    assert row["input_preview"]["truncated"] is True
    assert row["input_preview"]["omitted_bytes"] == (
        len(input_text.encode("utf-8"))
        - len(row["input_preview"]["head"].encode("utf-8"))
        - len(row["input_preview"]["tail"].encode("utf-8"))
    )


@pytest.mark.parametrize(
    "text",
    [
        "a" * PREVIEW_BYTES,
        "b" * (PREVIEW_BYTES + 1),
        "c" * (PREVIEW_BYTES * 2),
        "d" * (PREVIEW_BYTES * 2 + 1),
        ("界" * 1_365) + "xy",
        ("界" * 2_730) + "xy",
        ("🙂" * 2_048),
    ],
    ids=(
        "ascii-4096",
        "ascii-4097",
        "ascii-8192",
        "ascii-8193",
        "multibyte-4097",
        "multibyte-8192-split",
        "multibyte-8192-exact",
    ),
)
def test_preview_reconstructs_or_explicitly_accounts_for_every_utf8_byte(
    tmp_path: Path,
    text: str,
) -> None:
    """A false truncated flag must never make the Console silently drop a tail."""

    store = ProviderCaptureStore(tmp_path)
    store.write(_record(input=text))

    preview = store.list()[0]["input_preview"]
    head = preview["head"]
    tail = preview["tail"]
    displayed_bytes = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    source_bytes = len(text.encode("utf-8"))
    assert len(head.encode("utf-8")) <= PREVIEW_BYTES
    assert len(tail.encode("utf-8")) <= PREVIEW_BYTES
    assert preview["omitted_bytes"] == source_bytes - displayed_bytes
    assert preview["truncated"] is (preview["omitted_bytes"] > 0)
    if preview["truncated"]:
        assert preview["omitted_bytes"] > 0
    else:
        assert head + tail == text


@pytest.mark.parametrize(
    ("input_text", "output_text"),
    [
        ("\0" * (PREVIEW_BYTES * 2), "\0" * (PREVIEW_BYTES * 2)),
        (
            ("\x01\x02\n\t" * (PREVIEW_BYTES // 2)),
            ("\x03\x04\r\b" * (PREVIEW_BYTES // 2)),
        ),
        (
            ("界\0🙂\x1f" * 1_400),
            ("🙂\x00界\x07" * 1_400),
        ),
    ],
    ids=("nul", "mixed-controls", "multibyte-controls"),
)
def test_encoded_preview_sidecar_is_bounded_without_silent_clipping(
    tmp_path: Path,
    input_text: str,
    output_text: str,
) -> None:
    """JSON escaping cannot make a generated sidecar unreadable by collection."""

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(_record(input=input_text, output=output_text))
    sidecar = tmp_path / "records" / f"{record_id}.preview.json"

    assert sidecar.stat().st_size <= 64 * 1024
    row = store.list()[0]
    for field, source in (("input", input_text), ("output", output_text)):
        preview = row[f"{field}_preview"]
        displayed = preview["head"] + preview["tail"]
        displayed_bytes = len(displayed.encode("utf-8"))
        assert preview["bytes"] == len(source.encode("utf-8"))
        assert preview["characters"] == len(source)
        assert preview["omitted_bytes"] == preview["bytes"] - displayed_bytes
        assert preview["truncated"] is (preview["omitted_bytes"] > 0)
        assert source.startswith(preview["head"])
        assert source.endswith(preview["tail"])
        if not preview["truncated"]:
            assert displayed == source


def test_maintenance_repairs_an_adversarial_preview_within_encoded_limit(
    tmp_path: Path,
) -> None:
    """Repair must use the same encoded bound as the initial pair publication."""

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(
        _record(
            input="\0" * (PREVIEW_BYTES * 2),
            output="\x01" * (PREVIEW_BYTES * 2),
        )
    )
    sidecar = tmp_path / "records" / f"{record_id}.preview.json"
    sidecar.write_text("{broken", encoding="utf-8")

    ProviderCaptureStore(tmp_path).maintain()

    assert sidecar.stat().st_size <= 64 * 1024
    rows = ProviderCaptureStore(tmp_path).list()
    assert [row["id"] for row in rows] == [record_id]
    assert rows[0]["input_preview"]["omitted_bytes"] > 0
    assert rows[0]["output_preview"]["omitted_bytes"] > 0


def test_list_bounds_and_validates_every_direct_write_summary_field(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    oversized = "summary words " * 20_000
    record_id = store.write(
        _record(
            provider_id=oversized,
            status={"nested": oversized},
            model=oversized,
            model_run_id=oversized,
            usage={
                "total_tokens": 42,
                "prompt_tokens": {"nested": oversized},
                "raw": oversized,
            },
            truncation={
                "type": "truncated",
                "original_bytes": 300_000,
                "omitted_bytes": 200_000,
                "head": oversized,
            },
            metadata={
                "agent_id": oversized,
                "session_id": oversized,
                "run_id": oversized,
            },
        )
    )

    row = store.list()[0]
    encoded = json.dumps(row, ensure_ascii=False).encode("utf-8")
    assert row["id"] == record_id
    assert len(encoded) < PREVIEW_BYTES
    assert oversized.encode() not in encoded
    assert row["usage"] == {"total_tokens": 42}
    assert row["truncation"] == {
        "type": "truncated",
        "original_bytes": 300_000,
        "omitted_bytes": 200_000,
    }
    for field in (
        "provider_id",
        "model",
        "model_run_id",
        "agent_id",
        "session_id",
        "run_id",
    ):
        assert len(row[field]) < 80
    assert "status" not in row


def test_write_bounds_uncompressed_json_and_marks_omitted_content(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(_record(input="safe words " * 900_000))

    raw = _raw_records(tmp_path)[0]
    assert len(raw) <= MAX_RECORD_BYTES
    detail = store.get(record_id)
    assert detail is not None
    assert _contains_type(detail, "truncated")


def test_field_level_bound_adds_root_truncation_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the root summary would hide full-record truncation from detail UI."""

    import bianbu_agent.capture.llm as capture

    monkeypatch.setattr(capture, "MAX_RECORD_BYTES", 64 * 1024)
    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(_record(input="x" * (80 * 1024)))

    detail = store.get(record_id)
    row = store.list()[0]
    assert detail is not None
    assert detail["http_request"]["type"] == "truncated"
    assert detail["truncation"]["type"] == "truncated"
    assert detail["truncation"]["fields"] == ["http_request"]
    assert detail["truncation"]["field_count"] == 1
    assert detail["truncation"]["omitted_bytes"] > 0
    assert row["truncation"]["type"] == "truncated"
    assert row["truncation"]["field_count"] == 1


def test_write_bounds_oversized_identity_fields_at_public_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bianbu_agent.capture.llm as capture

    monkeypatch.setattr(capture, "MAX_RECORD_BYTES", 1024)
    store = ProviderCaptureStore(tmp_path)

    record_id = store.write(_record(provider_id="provider words " * 1000))

    raw = _raw_records(tmp_path)[0]
    assert len(raw) <= 1024
    detail = store.get(record_id)
    assert detail is not None
    assert _contains_type(detail, "truncated")


def test_list_is_newest_first_and_clamps_requests_to_one_hundred(
    tmp_path: Path,
) -> None:
    store = ProviderCaptureStore(tmp_path)
    identifiers = [store.write(_record(sequence=index)) for index in range(105)]

    rows = store.list(limit=1000)
    assert len(rows) == 100
    assert rows[0]["id"] == identifiers[-1]
    assert rows[-1]["id"] == identifiers[-100]


def test_list_reads_atomic_preview_sidecars_without_decompressing_retained_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring gzip-backed list reads would expand every highly compressible row."""

    store = ProviderCaptureStore(tmp_path)
    identifiers = [
        store.write(_record(input=(str(index % 10) * (64 * 1024))))
        for index in range(105)
    ]
    assert len(list((tmp_path / "records").glob("*.preview.json"))) == 105

    preview_reads = 0
    original_preview_read = store._read_preview

    def counted_preview_read(path: Path) -> dict[str, object] | None:
        nonlocal preview_reads
        preview_reads += 1
        return original_preview_read(path)

    def unexpected_detail_read(_path: Path) -> None:
        raise AssertionError("collection decompressed a full capture record")

    monkeypatch.setattr(store, "_read", unexpected_detail_read)
    monkeypatch.setattr(store, "_read_preview", counted_preview_read)
    monkeypatch.setattr(
        store,
        "_record_paths",
        lambda: (_ for _ in ()).throw(
            AssertionError("collection enumerated the records directory")
        ),
    )
    rows = store.list(limit=1_000)

    assert len(rows) == 100
    assert preview_reads == 100
    assert [row["id"] for row in rows] == list(reversed(identifiers[-100:]))


def test_restart_repairs_a_corrupt_bounded_manifest_without_unbounded_list_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collection stays bounded when the persisted newest-record index is corrupt."""

    store = ProviderCaptureStore(tmp_path)
    identifiers = [store.write(_record(sequence=index)) for index in range(6)]
    manifest = tmp_path / "records" / "index.json"
    assert manifest.is_file()
    manifest.write_text("{broken", encoding="utf-8")
    restarted = ProviderCaptureStore(tmp_path)

    assert restarted.list() == []
    restarted.maintain()
    original_iterdir = Path.iterdir

    def bounded_iterdir(path: Path):
        if path == tmp_path / "records":
            raise AssertionError("collection enumerated the records directory")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", bounded_iterdir)
    assert [row["id"] for row in restarted.list()] == list(reversed(identifiers))


def test_corrupt_preview_sidecar_is_skipped_then_repaired_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt index cannot fail collection or permanently hide a valid gzip."""

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(_record(input="restart repair"))
    preview_path = tmp_path / "records" / f"{record_id}.preview.json"
    preview_path.write_text("{not-json", encoding="utf-8")
    restarted = ProviderCaptureStore(tmp_path)

    assert restarted.list() == []
    restarted.maintain()

    monkeypatch.setattr(
        restarted,
        "_read",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("repaired collection read the full gzip")
        ),
    )
    assert [row["id"] for row in restarted.list()] == [record_id]


def test_concurrent_store_instances_publish_complete_record_and_preview_pairs(
    tmp_path: Path,
) -> None:
    """Removing unique atomic sidecars would expose partial rows across instances."""

    stores = [ProviderCaptureStore(tmp_path), ProviderCaptureStore(tmp_path)]

    def write(index: int) -> str:
        return stores[index % 2].write(
            _record(input=(f"concurrent-{index}-" * 2_000))
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        identifiers = list(executor.map(write, range(40)))

    records = tmp_path / "records"
    rows = ProviderCaptureStore(tmp_path).list()
    assert {row["id"] for row in rows} == set(identifiers)
    assert len(list(records.glob("*.json.gz"))) == 40
    assert len(list(records.glob("*.preview.json"))) == 40
    assert list(records.glob(".capture-*.tmp")) == []
    manifest = json.loads((records / "index.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in manifest["records"]} == set(identifiers)


def test_concurrent_store_instances_enforce_record_and_inode_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent store locks cannot lose index entries or retain extra pairs."""

    import bianbu_agent.capture.llm as capture

    monkeypatch.setattr(capture, "MAX_RETAINED_RECORDS", 25, raising=False)
    stores = [ProviderCaptureStore(tmp_path) for _ in range(3)]

    def write(index: int) -> str:
        return stores[index % len(stores)].write(_record(sequence=index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(write, range(60)))

    records = tmp_path / "records"
    gzip_ids = {path.name.removesuffix(".json.gz") for path in records.glob("*.json.gz")}
    preview_ids = {
        path.name.removesuffix(".preview.json")
        for path in records.glob("*.preview.json")
    }
    manifest = json.loads((records / "index.json").read_text(encoding="utf-8"))
    manifest_ids = {item["id"] for item in manifest["records"]}
    assert len(gzip_ids) == len(preview_ids) == len(manifest_ids) == 25
    assert gzip_ids == preview_ids == manifest_ids
    assert manifest_ids <= set(identifiers)
    assert list(records.glob(".capture-*.tmp")) == []


def test_write_prunes_records_older_than_seven_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    old_id = store.write(_record(input="old"))
    old_path = tmp_path / "records" / f"{old_id}.json.gz"
    old_time = capture.time.time() - capture.RETENTION_SECONDS - 1
    os.utime(old_path, (old_time, old_time))

    new_id = store.write(_record(input="new"))

    assert store.get(old_id) is None
    assert store.get(new_id) is not None


def test_list_prunes_expired_records_after_store_restart(tmp_path: Path) -> None:
    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    old_id = store.write(_record(input="old"))
    old_path = tmp_path / "records" / f"{old_id}.json.gz"
    old_time = capture.time.time() - capture.RETENTION_SECONDS - 1
    os.utime(old_path, (old_time, old_time))

    assert ProviderCaptureStore(tmp_path).list() == []
    assert not old_path.exists()


def test_write_prunes_oldest_records_to_total_storage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    first = store.write(
        _record(input=" ".join(f"first-word-{index:05d}" for index in range(5000)))
    )
    second = store.write(_record(input="second words " * 100))
    records = tmp_path / "records"
    second_size = sum(
        (records / f"{second}{suffix}").stat().st_size
        for suffix in (".json.gz", ".preview.json")
    )
    monkeypatch.setattr(capture, "MAX_STORAGE_BYTES", (second_size * 2) + 1024)

    third = store.write(_record(input="third words " * 100))

    assert store.get(first) is None
    assert store.get(second) is not None
    assert store.get(third) is not None


def test_storage_quota_counts_preview_sidecars_and_removes_whole_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tiny gzip cannot retain a large escaped preview outside the quota."""

    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(
        _record(input="\0" * (PREVIEW_BYTES * 2), output="\1" * (PREVIEW_BYTES * 2))
    )
    records = tmp_path / "records"
    gzip_path = records / f"{record_id}.json.gz"
    preview_path = records / f"{record_id}.preview.json"
    quota = gzip_path.stat().st_size + 1024
    assert preview_path.stat().st_size > 1024
    monkeypatch.setattr(capture, "MAX_STORAGE_BYTES", quota, raising=False)

    store.maintain()

    assert not gzip_path.exists()
    assert not preview_path.exists()
    assert store.list() == []


def test_storage_quota_cleans_capture_temps_that_cannot_fit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crashed live-temp bytes cannot leave physical storage above its limit."""

    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    store.write(_record(input="retained"))
    records = tmp_path / "records"
    temporary = records / ".capture-interrupted.tmp"
    temporary.write_bytes(b"x" * 8192)
    monkeypatch.setattr(capture, "MAX_STORAGE_BYTES", 4096, raising=False)

    store.maintain()

    capture_files = [
        path
        for path in records.iterdir()
        if path.name.endswith((".json.gz", ".preview.json", ".tmp"))
    ]
    assert sum(path.stat().st_size for path in capture_files) <= 4096
    assert not temporary.exists()


def test_maintenance_removes_noncanonical_capture_files_from_inode_budget(
    tmp_path: Path,
) -> None:
    """Invalid-ID gzip/sidecar names cannot accumulate outside the pair cap."""

    store = ProviderCaptureStore(tmp_path)
    retained_id = store.write(_record(input="retained"))
    records = tmp_path / "records"
    invalid_paths: list[Path] = []
    for index in range(12):
        invalid_paths.extend(
            (
                records / f"invalid-{index}.json.gz",
                records / f"invalid-{index}.preview.json",
            )
        )
    for path in invalid_paths:
        path.write_bytes(b"orphan")

    store.maintain()

    assert all(not path.exists() for path in invalid_paths)
    assert {path.name for path in records.glob("*.json.gz")} == {
        f"{retained_id}.json.gz"
    }
    assert {path.name for path in records.glob("*.preview.json")} == {
        f"{retained_id}.preview.json"
    }


def test_prune_accounts_for_a_record_concurrently_removed_before_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A FileNotFound race must not make quota pruning delete a second record."""

    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    identifiers = [
        store.write(_record(input=(f"record-{index}-" * 4_000)))
        for index in range(3)
    ]
    paths = [tmp_path / "records" / f"{record_id}.json.gz" for record_id in identifiers]
    pair_sizes = [
        path.stat().st_size
        + (tmp_path / "records" / f"{record_id}.preview.json").stat().st_size
        for path, record_id in zip(paths, identifiers, strict=True)
    ]
    monkeypatch.setattr(
        capture,
        "MAX_STORAGE_BYTES",
        pair_sizes[1] + pair_sizes[2],
    )
    original_unlink = Path.unlink
    raced = False

    def concurrent_unlink(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal raced
        if path == paths[0] and not raced:
            raced = True
            original_unlink(path, *args, **kwargs)
            raise FileNotFoundError(str(path))
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", concurrent_unlink)
    store.maintain()

    assert [path.exists() for path in paths] == [False, True, True]


def test_prune_removes_old_orphan_capture_temp_before_quota_accounting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crashed highly compressible temp cannot live forever outside the quota."""

    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    record_id = store.write(_record(input="retained"))
    record_path = tmp_path / "records" / f"{record_id}.json.gz"
    orphan = tmp_path / "records" / ".capture-orphan.tmp"
    orphan.write_bytes(b"x" * (256 * 1024))
    expired = capture.time.time() - capture.RETENTION_SECONDS - 1
    os.utime(orphan, (expired, expired))
    preview_path = tmp_path / "records" / f"{record_id}.preview.json"
    monkeypatch.setattr(
        capture,
        "MAX_STORAGE_BYTES",
        record_path.stat().st_size + preview_path.stat().st_size,
    )

    store.maintain()

    assert not orphan.exists()
    assert store.get(record_id) is not None


def test_record_write_uses_replace_and_removes_failed_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(capture.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        store.write(_record())

    records = tmp_path / "records"
    assert list(records.glob("*.json.gz")) == []
    assert list(records.iterdir()) == []


def test_preview_publish_failure_rolls_back_record_and_every_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bianbu_agent.capture.llm as capture

    store = ProviderCaptureStore(tmp_path)
    original_replace = capture.os.replace
    replace_calls = 0

    def fail_second_replace(source: object, destination: object) -> None:
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("preview replace failed")
        original_replace(source, destination)

    monkeypatch.setattr(capture.os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="preview replace failed"):
        store.write(_record())

    records = tmp_path / "records"
    assert list(records.iterdir()) == []


@pytest.mark.parametrize(
    "record_id",
    ["A" * 32, "../" + ("a" * 32), "a" * 31, "g" * 32, "a/b"],
)
def test_get_rejects_noncanonical_record_ids(tmp_path: Path, record_id: str) -> None:
    with pytest.raises(ValueError, match="invalid capture record id"):
        ProviderCaptureStore(tmp_path).get(record_id)


def test_list_and_get_skip_corrupt_or_concurrently_removed_records(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    corrupt_id = store.write(_record(input="corrupt me"))
    valid_id = store.write(_record(input="keep me"))
    corrupt_path = tmp_path / "records" / f"{corrupt_id}.json.gz"
    corrupt_path.write_bytes(b"not gzip")

    rows = store.list()

    assert [row["id"] for row in rows] == [valid_id]
    assert store.get(corrupt_id) is None
    (tmp_path / "records" / f"{valid_id}.json.gz").unlink()
    assert store.get(valid_id) is None


def test_corrupt_surrogate_target_is_treated_as_inactive(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b'{"provider_id":"\\ud800"}')

    assert ProviderCaptureStore(tmp_path).target() is None


def test_list_skips_gzip_json_with_invalid_record_shape(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    record_id = "a" * 32
    records = tmp_path / "records"
    records.mkdir()
    path = records / f"{record_id}.json.gz"
    path.write_bytes(
        gzip.compress(
            json.dumps({"id": record_id, "captured_at": "not-a-number"}).encode()
        )
    )

    assert store.list() == []
    assert store.get(record_id) is None


@pytest.mark.parametrize(
    "invalid_value",
    [
        "NaN",
        "Infinity",
        "-Infinity",
        "9" * 5_000,
        '"\\ud800"',
        ("[" * 200) + "0" + ("]" * 200),
    ],
    ids=("nan", "infinity", "negative-infinity", "huge-int", "surrogate", "deep"),
)
def test_list_and_get_skip_strictly_invalid_json_values(
    tmp_path: Path,
    invalid_value: str,
) -> None:
    """Relaxing strict JSON validation must not turn corrupt detail into a 500."""

    record_id = "b" * 32
    records = tmp_path / "records"
    records.mkdir()
    document = (
        f'{{"id":"{record_id}","captured_at":1,"input":{invalid_value}}}'
    ).encode("utf-8")
    (records / f"{record_id}.json.gz").write_bytes(gzip.compress(document))
    store = ProviderCaptureStore(tmp_path)

    assert store.list() == []
    assert store.get(record_id) is None


def test_clear_removes_records_without_stopping_capture(tmp_path: Path) -> None:
    store = ProviderCaptureStore(tmp_path)
    store.select("openai")
    store.write(_record())

    store.clear()

    assert store.list() == []
    assert store.target() == "openai"


def test_callback_is_fail_open_when_target_read_or_write_fails() -> None:
    class FailingStore:
        fail_target = True

        def target(self) -> str | None:
            if self.fail_target:
                raise OSError("target failed")
            return "openai"

        def write(self, record: dict[str, object]) -> str:
            raise OSError("write failed")

    store = FailingStore()
    callback = ProviderCaptureCallback(store)  # type: ignore[arg-type]

    callback.on_chat_model_start(
        {}, [], run_id=uuid4(), metadata={"provider_id": "openai"}
    )
    store.fail_target = False
    run_id = uuid4()
    callback.on_chat_model_start(
        {}, [], run_id=run_id, metadata={"provider_id": "openai"}
    )
    callback.on_llm_end(LLMResult(generations=[]), run_id=run_id)
