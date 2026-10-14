from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bianbu_agent.runs import RunHandle
from bianbu_agent.security.runtime_process.protocol import (
    decode_message,
    encode_message,
    request as make_request,
    response as make_response,
)
from bianbu_agent.security.runtime_process.worker_rpc import WorkerRPCClient, WorkerRPCError, WorkerRPCServer
from bianbu_agent.security.runtime_process.worker import MemoryRunStore
from bianbu_agent.sessions import SessionInboxMessage


@pytest.mark.asyncio
async def test_worker_rpc_hello_health_and_shutdown(tmp_path: Path) -> None:
    socket_path = tmp_path / "instance" / "worker.sock"
    instance = "instance-1"
    token = "test-rpc-token"

    async def dispatch(kind, message):
        if kind == "hello":
            # The Worker hello view repeats the instance identity for
            # attestation.  The RPC envelope must de-duplicate it safely.
            return {
                "agent_id": "com.bianbu.agent", "instance_id": instance,
                "pid": 1, "status": "running",
            }
        if kind == "health":
            return {"status": "running", "active_runs": 0}
        if kind == "worker.reload":
            return {"status": "active", "generation_id": 2, "changed": True}
        if kind == "worker.catalog":
            return {
                "generation_id": 2,
                "capabilities": {"tool": [], "skill": [], "hook": []},
                "mcps": ["weather"],
                "mcp_statuses": {"weather": {"state": "running", "reason": ""}},
                "mcp_servers": [{"id": "weather", "name": "Weather"}],
            }
        if kind == "shutdown":
            return {"accepted": True}
        raise RuntimeError("not expected")

    server = WorkerRPCServer(socket_path, instance, dispatch, token=token)
    await server.start()
    try:
        client = WorkerRPCClient(socket_path, instance, token=token)
        hello = await client.hello()
        assert hello["type"] == "hello.result"
        assert hello["instance_id"] == instance
        assert (await client.health())["active_runs"] == 0
        assert (await client.reload())["changed"] is True
        catalog = await client.catalog()
        assert catalog["capabilities"] == {"tool": [], "skill": [], "hook": []}
        assert catalog["mcps"] == ["weather"]
        assert catalog["mcp_statuses"]["weather"]["state"] == "running"
        assert catalog["mcp_servers"] == [{"id": "weather", "name": "Weather"}]
        assert await client.shutdown()
    finally:
        await server.close()
    assert not socket_path.exists()


@pytest.mark.asyncio
async def test_health_socket_timeout_is_reported_as_rpc_error(monkeypatch, tmp_path: Path) -> None:
    client = WorkerRPCClient(tmp_path / "worker.sock", "instance", token="token")

    async def fail_connect():
        raise TimeoutError()

    monkeypatch.setattr(client, "_connect", fail_connect)
    with pytest.raises(WorkerRPCError, match="health RPC timed out"):
        await client.health()


@pytest.mark.asyncio
async def test_worker_rpc_run_controls_preserve_run_identity(tmp_path: Path) -> None:
    socket_path = tmp_path / "instance" / "worker.sock"
    instance = "instance-controls"
    token = "test-rpc-token"
    received: list[tuple[str, str, object]] = []

    async def dispatch(kind, message):
        received.append((kind, message["run_id"], message.get("payload")))
        if kind == "run.cancel":
            return {"accepted": True}
        if kind == "run.step_input":
            assert message["messages"] == [
                {
                    "message_id": "message-2",
                    "content": "continue",
                    "attachments": [{"name": "note.txt"}],
                    "skill_ids": ["global.skills/review"],
                    "created_at": 12.5,
                },
                {
                    "message_id": "message-3",
                    "content": "also continue",
                    "attachments": [],
                    "skill_ids": [],
                    "created_at": 13.5,
                },
            ]
            return {"accepted": True}
        if kind == "approval.result":
            return {"accepted": True}
        raise RuntimeError("not expected")

    server = WorkerRPCServer(socket_path, instance, dispatch, token=token)
    await server.start()
    try:
        client = WorkerRPCClient(socket_path, instance, token=token)
        assert await client.cancel("run-1") is True
        assert await client.step_input(
            "run-2",
            (
                SessionInboxMessage(
                    "message-2",
                    "continue",
                    attachments=({"name": "note.txt"},),
                    skill_ids=("global.skills/review",),
                    created_at=12.5,
                ),
                SessionInboxMessage(
                    "message-3",
                    "also continue",
                    created_at=13.5,
                ),
            ),
        ) is True
        assert await client.approval(
            "run-3",
            "reject",
            {"approval_id": "approval-1", "reason": "denied"},
        ) is True
    finally:
        await server.close()

    assert received == [
        ("run.cancel", "run-1", None),
        ("run.step_input", "run-2", None),
        (
            "approval.result",
            "run-3",
            {"approval_id": "approval-1", "reason": "denied"},
        ),
    ]


@pytest.mark.asyncio
async def test_worker_rpc_rejects_wrong_instance_token(tmp_path: Path) -> None:
    socket_path = tmp_path / "instance" / "worker.sock"
    instance = "instance-auth"

    async def dispatch(kind, message):
        return {"status": "running", "active_runs": 0}

    server = WorkerRPCServer(socket_path, instance, dispatch, token="right-token")
    await server.start()
    try:
        client = WorkerRPCClient(socket_path, instance, token="wrong-token")
        with pytest.raises(Exception, match="authentication"):
            await client.health()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_run_start_surfaces_a_mid_stream_error_frame(tmp_path: Path) -> None:
    """run.accepted 之后的 error 帧必须变成一次明确的失败。

    客户端以前只认 run.event / run.result：错误帧被跳过，Worker 关闭连接后
    客户端退化成 EOFError，真正的 code/details 全部丢失，界面上只剩
    「运行时工作进程异常」这种无法定位的文案。
    """
    socket_path = tmp_path / "instance" / "worker.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    instance = "instance-error-frame"
    run_id = "run-error-frame"

    async def handle(reader, writer) -> None:
        request = decode_message(await reader.readline(), direction="request")

        async def send(kind: str, **payload) -> None:
            writer.write(encode_message(
                make_response(kind, request["request_id"], instance, **payload),
                direction="response",
            ))
            await writer.drain()

        await send("run.accepted", run_id=run_id)
        await send("error", code="upstream_unavailable",
                   message="模型服务返回 502",
                   details={"http_status": 502})
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        client = WorkerRPCClient(socket_path, instance, token="token")
        with pytest.raises(WorkerRPCError) as caught:
            await client.run_start(_run_start_payload(run_id), _noop_event)
    finally:
        server.close()
        await server.wait_closed()

    assert caught.value.code == "upstream_unavailable"
    assert "502" in str(caught.value)
    assert caught.value.details == {"http_status": 502}


@pytest.mark.asyncio
async def test_error_frame_omits_empty_details_for_older_control(tmp_path: Path) -> None:
    """空 details 不能出现在错误帧里。

    控制面与 Worker 是同一个包，但升级窗口里可能「新 Worker + 未重启的旧
    Control」；旧 Control 的严格校验会把任何未知字段当成坏帧整帧丢弃，失败
    原因随之丢失。常见错误的 details 本来就是空的。
    """
    socket_path = tmp_path / "instance" / "worker.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    instance = "instance-empty-details"

    async def dispatch(kind, message):
        raise ValueError("bad request")

    server = WorkerRPCServer(socket_path, instance, dispatch, token="token")
    await server.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.write(encode_message(
            make_request("hello", "req-1", instance, token="token"),
            direction="request",
        ))
        await writer.drain()
        frame = decode_message(await reader.readline(), direction="response")
        writer.close()
        await writer.wait_closed()
    finally:
        await server.close()

    assert frame["type"] == "error"
    assert frame["code"] == "validation_error"
    assert "details" not in frame


@pytest.mark.asyncio
async def test_rejected_run_keeps_the_worker_code_and_details(tmp_path: Path) -> None:
    """Worker 在 run.accepted 之前拒绝这次 Run 时，码和细节同样不能丢。"""
    socket_path = tmp_path / "instance" / "worker.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    instance = "instance-rejected-run"

    async def handle(reader, writer) -> None:
        request = decode_message(await reader.readline(), direction="request")
        writer.write(encode_message(
            make_response(
                "error", request["request_id"], instance,
                code="skill_unavailable",
                message="Selected Skills are unavailable: web-search",
                details={"skill_ids": ["web-search"]},
            ),
            direction="response",
        ))
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        client = WorkerRPCClient(socket_path, instance, token="token")
        with pytest.raises(WorkerRPCError) as caught:
            await client.run_start(
                _run_start_payload("run-rejected"), _noop_event
            )
    finally:
        server.close()
        await server.wait_closed()

    assert caught.value.code == "skill_unavailable"
    assert caught.value.details == {"skill_ids": ["web-search"]}


async def _noop_event(message) -> None:
    return None


def _run_start_payload(run_id: str) -> dict[str, str]:
    """协议要求的最小 run.start 载荷。"""
    return {
        "run_id": run_id,
        "session_id": "session-1",
        "thread_id": "session-1",
        "account_id": "account-1",
        "prompt": "hello",
    }


class _ExplodingHandle(RunHandle):
    """迭代中途失败：模拟 Worker 在 Run 跑了一半时挂掉。"""

    def __init__(self, *, request_id: str, session_id: str) -> None:
        super().__init__(request_id=request_id, session_id=session_id, store=None)

    async def _events(self):
        raise ConnectionError("worker stream disconnected")
        yield  # pragma: no cover - 让这个方法保持异步生成器


@pytest.mark.asyncio
async def test_failed_run_after_accept_is_reported_as_a_run_result(tmp_path: Path) -> None:
    """已经 run.accepted 的流不能再发 error 帧。

    否则客户端把它当坏帧、连接关闭后退化成 EOFError。失败必须以一次
    run.result 收尾，让终态自带 code 与原因。
    """
    socket_path = tmp_path / "instance" / "worker.sock"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    instance = "instance-failed-run"
    run_id = "run-exploding"

    async def dispatch(kind, message):
        if kind == "run.start":
            return _ExplodingHandle(request_id=run_id, session_id="session-1")
        raise RuntimeError("not expected")

    server = WorkerRPCServer(socket_path, instance, dispatch, token="token")
    await server.start()
    try:
        client = WorkerRPCClient(socket_path, instance, token="token")
        result = await client.run_start(_run_start_payload(run_id), _noop_event)
    finally:
        await server.close()

    assert result["type"] == "run.result"
    assert result["result"]["status"] == "failed"
    assert result["result"]["error"] == {
        "code": "upstream_unavailable",
        "message": "worker stream disconnected",
        "retryable": True,
        "details": {},
    }


@pytest.mark.asyncio
async def test_worker_rpc_delivers_catalog_larger_than_default_stream_limit(
    tmp_path: Path,
) -> None:
    """A Worker catalog can exceed the 64 KiB asyncio default line limit.

    The catalog carries every capability plus every remote MCP tool, so a
    packaged Skill set with a large MCP server already passes the default
    StreamReader limit.  Reading must be sized by the protocol contract
    instead, otherwise Control silently degrades to unknown MCP status.
    """
    socket_path = tmp_path / "instance" / "worker.sock"
    instance = "instance-large-catalog"
    token = "test-rpc-token"
    tools = [
        {"id": f"bianbu.bibit.intent/bibit-intent/tool-{index:04d}",
         "name": f"tool-{index:04d}",
         "display_name": f"Tool {index:04d} number {index:04d}"}
        for index in range(2000)
    ]

    async def dispatch(kind, message):
        if kind == "worker.catalog":
            return {
                "generation_id": 1,
                "capabilities": {"tool": [], "skill": [], "hook": []},
                "mcps": ["bianbu.bibit.intent/bibit-intent"],
                "mcp_statuses": {},
                "mcp_servers": [{
                    "id": "bianbu.bibit.intent/bibit-intent",
                    "status": "healthy",
                    "tools": tools,
                }],
            }
        raise RuntimeError("not expected")

    server = WorkerRPCServer(socket_path, instance, dispatch, token=token)
    await server.start()
    try:
        client = WorkerRPCClient(socket_path, instance, token=token)
        catalog = await client.catalog()
    finally:
        await server.close()
    assert len(catalog["mcp_servers"][0]["tools"]) == len(tools)
    assert catalog["mcp_servers"][0]["status"] == "healthy"
    assert len(json.dumps(catalog)) > 64 * 1024


def _inbox_message(message_id: str, content: str) -> SessionInboxMessage:
    return SessionInboxMessage(message_id=message_id, content=content, created_at=1.0)


def _input_row(message: SessionInboxMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "content": message.content,
        "created_at": message.created_at,
        "attachments": [],
        "skill_ids": [],
    }


@pytest.mark.asyncio
async def test_new_run_forgets_the_step_input_it_consumed_as_its_own_input() -> None:
    """K3 把「调到1」在上一轮结束前投进 inbox，下一轮又把它当 step input 应用。

    控制面在 Run 准入时就把 inbox 前缀 claim 成本轮输入；Worker 侧还留着同一
    条 step input 镜像，于是下一轮结束时又报一次 input_applied，被
    "applied input is not the next-step prefix" 顶掉整轮。
    """

    store = MemoryRunStore()
    steering = _inbox_message("message-1", "调到1")
    await store.start("run-1", "session-1", {"prompt": "声音调到99"})
    await store.replace_step_input("session-1", (steering,))
    assert (await store.pending_input("session-1")).next_step == (steering,)

    await store.start(
        "run-2",
        "session-1",
        {"prompt": "调到1", "input_messages": [_input_row(steering)]},
    )

    assert (await store.pending_input("session-1")).next_step == ()


@pytest.mark.asyncio
async def test_run_without_an_inbox_prefix_keeps_pending_step_input() -> None:
    """生成的标题 Run 与用户 Run 同 Session，不能顺手丢掉后者的转向输入。"""

    store = MemoryRunStore()
    steering = _inbox_message("message-1", "调到1")
    await store.start("run-1", "session-1", {"prompt": "声音调到99"})
    await store.replace_step_input("session-1", (steering,))

    await store.start(
        "title-run",
        "session-1",
        {"prompt": "generate a title", "metadata": {"console_operation": "generate_title"}},
    )

    assert (await store.pending_input("session-1")).next_step == (steering,)
