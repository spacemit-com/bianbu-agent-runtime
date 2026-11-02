"""版本化 Worker RPC 的严格 JSON 消息契约。

通信只发生在控制面创建的私有 Unix socket。所有消息都带协议版本、请求 ID
和实例 ID；事件还必须带单调递增的 sequence。未知字段和未知消息类型拒绝，
避免把未来/伪造消息误当成可执行命令。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
# asyncio StreamReader and start_unix_server default to a 64 KiB line limit,
# far below MAX_MESSAGE_BYTES.  Without an explicit limit a catalog payload
# larger than that fails with LimitOverrunError before the protocol check can
# judge it, which silently degrades Worker observations.
STREAM_LIMIT = MAX_MESSAGE_BYTES + 1

REQUEST_TYPES = frozenset({
    "hello", "health", "run.start", "run.cancel", "run.step_input",
    "approval.result", "worker.activate", "worker.reload", "worker.catalog",
    "session.terminate", "title.generate", "shutdown",
})
RESPONSE_TYPES = frozenset({
    "hello.result", "health.result", "run.accepted", "run.event",
    "run.result", "run.cancel.result", "run.step_input.result", "approval.result",
    "approval.request", "worker.activate.result", "shutdown.ack", "error",
    "worker.reload.result", "worker.catalog.result",
    "session.terminate.result", "title.generate.result",
})

_COMMON_FIELDS = frozenset({"protocol", "type", "request_id", "instance_id"})
_REQUEST_COMMON_FIELDS = _COMMON_FIELDS | frozenset({"token"})
_REQUEST_FIELDS = {
    "hello": _REQUEST_COMMON_FIELDS,
    "health": _REQUEST_COMMON_FIELDS,
    "shutdown": _REQUEST_COMMON_FIELDS,
    "run.start": _REQUEST_COMMON_FIELDS | frozenset({
        "run_id", "session_id", "agent_id", "account_id", "prompt", "idempotency_key",
        "idempotency_fingerprint", "metadata", "thread_id",
        "input_messages",
    }),
    "run.cancel": _REQUEST_COMMON_FIELDS | frozenset({"run_id"}),
    "run.step_input": _REQUEST_COMMON_FIELDS | frozenset({"run_id", "messages"}),
    "approval.result": _REQUEST_COMMON_FIELDS | frozenset({"run_id", "action", "payload"}),
    "worker.activate": _REQUEST_COMMON_FIELDS,
    "worker.reload": _REQUEST_COMMON_FIELDS | frozenset({"force"}),
    "worker.catalog": _REQUEST_COMMON_FIELDS,
    # Session deletion drops private DLP state; it carries a reason so a future
    # lifecycle point can distinguish close, delete and Agent removal.
    "session.terminate": _REQUEST_COMMON_FIELDS | frozenset({
        "session_id", "agent_id", "reason",
    }),
    # 标题是辅助请求，不需要 Run/lease，但仍要走与其它 Worker 操作相同的
    # 严格消息契约，否则控制面会在本地校验阶段就把它拒掉。
    "title.generate": _REQUEST_COMMON_FIELDS | frozenset({"prompt", "agent_id"}),
}
_RESPONSE_FIELDS = {
    "hello.result": _COMMON_FIELDS | frozenset({
        "agent_id", "instance_id", "lease_id", "pid", "sandboxed",
        "container_id", "effective_mode",
        "backend", "status", "capabilities", "generation_id",
    }),
    "health.result": _COMMON_FIELDS | frozenset({
        "status", "pid", "active_runs", "waiting_runs", "generation_id",
    }),
    "run.accepted": _COMMON_FIELDS | frozenset({"run_id"}),
    "run.event": _COMMON_FIELDS | frozenset({
        "run_id", "session_id", "sequence", "event",
    }),
    "run.result": _COMMON_FIELDS | frozenset({"run_id", "result"}),
    "run.cancel.result": _COMMON_FIELDS | frozenset({"run_id", "accepted"}),
    "run.step_input.result": _COMMON_FIELDS | frozenset({"run_id", "accepted"}),
    "approval.result": _COMMON_FIELDS | frozenset({"run_id", "accepted"}),
    "shutdown.ack": _COMMON_FIELDS | frozenset({"accepted"}),
    "worker.activate.result": _COMMON_FIELDS | frozenset({"status", "generation_id"}),
    "worker.reload.result": _COMMON_FIELDS | frozenset({"status", "generation_id", "changed"}),
    "worker.catalog.result": _COMMON_FIELDS | frozenset({
        "generation_id", "capabilities", "mcps", "mcp_servers", "mcp_statuses",
    }),
    "session.terminate.result": _COMMON_FIELDS | frozenset({
        "session_id", "cleared",
    }),
    # 标题回复同时带回精确请求与解析后的 provider 路由，控制面无需 Run
    # 就能把两者写进 Session 日志。
    "title.generate.result": _COMMON_FIELDS | frozenset({
        "title", "agent_id", "request", "route",
    }),
    # 错误帧必须能带上定位信息（哪个工具/哪条能力/哪个上游状态码）；少了
    # details，服务端写出的帧会被本文件的严格校验拒绝、静默丢弃，客户端
    # 只剩一个连接关闭。
    "error": _COMMON_FIELDS | frozenset({"code", "message", "run_id", "details"}),
    "approval.request": _COMMON_FIELDS | frozenset({
        "run_id", "session_id", "approval_id", "capability", "detail",
    }),
}


class ProtocolError(ValueError):
    """消息不符合 Worker RPC 契约。"""


def _text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a string")
    result = value.strip()
    if not result and not allow_empty:
        raise ProtocolError(f"{field} must not be empty")
    if "\x00" in result:
        raise ProtocolError(f"{field} contains NUL")
    return result


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ProtocolError(f"{field} keys must be strings")
    return dict(value)


def _common(message: Mapping[str, Any]) -> None:
    if message.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    _text(message.get("type"), "type")
    _text(message.get("request_id"), "request_id")
    _text(message.get("instance_id"), "instance_id")


def validate_message(message: Mapping[str, Any], *, direction: str = "request") -> dict[str, Any]:
    """Validate and return a detached message dictionary."""

    value = _object(message, "message")
    _common(value)
    kind = str(value["type"])
    allowed = REQUEST_TYPES if direction == "request" else RESPONSE_TYPES
    if kind not in allowed:
        raise ProtocolError(f"unknown {direction} type: {kind}")
    field_map = _REQUEST_FIELDS if direction == "request" else _RESPONSE_FIELDS
    fields = field_map.get(kind)
    if fields is not None:
        unknown = set(value) - fields
        if unknown:
            raise ProtocolError(f"unknown fields for {kind}: {', '.join(sorted(unknown))}")
    if direction == "request":
        _text(value.get("token"), "token")
    if kind == "run.event":
        sequence = value.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ProtocolError("run.event sequence must be a positive integer")
        _text(value.get("run_id"), "run_id")
        _text(value.get("session_id"), "session_id")
        _object(value.get("event"), "event")
    if kind in {"run.start", "run.cancel", "run.step_input", "approval.result", "run.accepted", "run.result"}:
        _text(value.get("run_id"), "run_id")
    if kind == "run.result":
        _object(value.get("result"), "result")
    if kind == "error" and "details" in value:
        _object(value.get("details"), "details")
    if kind == "run.start":
        _text(value.get("session_id"), "session_id")
        _text(value.get("thread_id"), "thread_id")
        _text(value.get("account_id"), "account_id")
        _text(value.get("prompt"), "prompt")
        raw_messages = value.get("input_messages", [])
        if not isinstance(raw_messages, list) or any(
            not isinstance(item, Mapping) for item in raw_messages
        ):
            raise ProtocolError("run.start input_messages must be an array of objects")
        from bianbu_agent.sessions.inbox import inbox_message_from_dict
        try:
            for item in raw_messages:
                inbox_message_from_dict(item)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"invalid run.start input message: {exc}") from exc
    if kind == "run.step_input":
        raw_messages = value.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages or any(
            not isinstance(item, Mapping) for item in raw_messages
        ):
            raise ProtocolError("run.step_input messages must be a non-empty array")
        from bianbu_agent.sessions.inbox import inbox_message_from_dict
        try:
            messages = tuple(inbox_message_from_dict(item) for item in raw_messages)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(f"invalid step input: {exc}") from exc
    if direction == "request" and kind == "approval.result":
        _text(value.get("action"), "action")
        _object(value.get("payload"), "payload")
    if direction == "request" and kind == "session.terminate":
        _text(value.get("session_id"), "session_id")
        _text(value.get("agent_id"), "agent_id")
        _text(value.get("reason"), "reason")
    if direction == "response" and kind == "session.terminate.result":
        _text(value.get("session_id"), "session_id")
        if type(value.get("cleared")) is not bool:
            raise ProtocolError("cleared must be a boolean")
    if direction == "request" and kind == "title.generate":
        _text(value.get("prompt"), "prompt")
        _text(value.get("agent_id"), "agent_id")
    if direction == "response" and kind == "title.generate.result":
        _text(value.get("title"), "title", allow_empty=True)
        _text(value.get("agent_id"), "agent_id")
        _object(value.get("request"), "request")
        _object(value.get("route"), "route")
    return value


def encode_message(message: Mapping[str, Any], *, direction: str = "request") -> bytes:
    value = validate_message(message, direction=direction)
    payload = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds maximum size")
    return payload


def decode_message(payload: bytes | str, *, direction: str = "request") -> dict[str, Any]:
    if isinstance(payload, bytes):
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ProtocolError("message exceeds maximum size")
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("message is not UTF-8") from exc
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProtocolError("message is not valid JSON") from exc
    return validate_message(value, direction=direction)


def request(kind: str, request_id: str, instance_id: str, *, token: str = "",
            **payload: Any) -> dict[str, Any]:
    return {"protocol": PROTOCOL_VERSION, "type": kind, "request_id": request_id,
            "instance_id": instance_id, "token": token, **payload}


def response(kind: str, request_id: str, instance_id: str, **payload: Any) -> dict[str, Any]:
    return {"protocol": PROTOCOL_VERSION, "type": kind, "request_id": request_id,
            "instance_id": instance_id, **payload}


__all__ = [
    "MAX_MESSAGE_BYTES", "PROTOCOL_VERSION", "ProtocolError", "REQUEST_TYPES",
    "STREAM_LIMIT",
    "RESPONSE_TYPES", "decode_message", "encode_message", "request", "response",
    "validate_message",
]
