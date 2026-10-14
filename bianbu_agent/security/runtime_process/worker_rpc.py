"""控制面与默认 Agent Worker 的 Unix socket JSON-lines RPC。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import socket
import stat
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from bianbu_agent.runs import RunError, RunHandle, RunResult, encode_result
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.sessions.inbox import SessionInboxMessage, inbox_message_to_dict
from bianbu_agent.security.runtime_process.protocol import (
    MAX_MESSAGE_BYTES,
    STREAM_LIMIT,
    ProtocolError,
    decode_message,
    encode_message,
    request as make_request,
    response as make_response,
)


class WorkerRPCError(RuntimeError):
    """An error returned by the Worker while preserving its safe code."""

    def __init__(self, code: str, message: str,
                 details: Mapping[str, Any] | None = None):
        self.code = str(code or "rpc_error")
        self.details = dict(details) if isinstance(details, Mapping) else {}
        super().__init__(str(message or "Worker RPC failed"))


def default_socket_root(data_dir: Path) -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime_dir:
        return Path(runtime_dir) / "bianbu-agent" / "instances"
    return data_dir / "runtime" / "instances"


def make_instance_socket(root: Path, instance_id: str) -> Path:
    if not instance_id or "/" in instance_id or "\\" in instance_id or ".." in instance_id:
        raise ValueError("invalid worker instance id")
    resolved_root = Path(root).expanduser().resolve()
    # Linux AF_UNIX pathname sockets accept at most 108 bytes.  Agent IDs are
    # part of the human-readable instance ID, so a valid but long Security
    # Agent ID can otherwise make the Worker start forever fail with
    # ``AF_UNIX path too long``. Keep the historical directory layout for
    # ordinary IDs, and use a collision-resistant flat name when it would
    # exceed a conservative limit. The mapping is deterministic and is used
    # by both the supervisor and the Worker CLI.
    candidate = resolved_root / instance_id / "worker.sock"
    if len(str(candidate).encode()) <= 100:
        return candidate
    full_digest = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()
    # Leave room for ``/worker-`` and ``.sock`` even when a caller configured
    # a relatively deep socket root. Eight hex characters is the minimum
    # retained identity (the normal runtime root retains up to 40).
    root_bytes = len(str(resolved_root).encode())
    digest_length = max(8, min(40, 100 - root_bytes - len("/worker-.sock")))
    digest = full_digest[:digest_length]
    flat = resolved_root / f"worker-{digest}.sock"
    if len(str(flat).encode()) > 100:
        raise ValueError("worker socket root is too long for AF_UNIX pathname")
    return flat


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    line = await reader.readline()
    if not line:
        raise EOFError
    if len(line) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message exceeds maximum size")
    return line


async def _write(writer: asyncio.StreamWriter, message: Mapping[str, Any], *, direction: str) -> None:
    writer.write(encode_message(message, direction=direction))
    await writer.drain()


class WorkerRPCClient:
    """短连接控制客户端；run.start 保持连接直到结果流结束。"""

    def __init__(self, socket_path: str | Path, instance_id: str, *, token: str,
                 timeout: float = 10.0):
        self.socket_path = Path(socket_path)
        self.instance_id = str(instance_id)
        self.token = str(token).strip()
        if not self.token:
            raise ValueError("Worker RPC token must not be empty")
        self.timeout = float(timeout)
        self.last_peer_pid: int | None = None
        self.last_peer_uid: int | None = None

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(
                str(self.socket_path), limit=STREAM_LIMIT
            ),
            timeout=self.timeout,
        )
        sock = writer.get_extra_info("socket")
        if sock is not None and hasattr(socket, "SO_PEERCRED"):
            try:
                raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                self.last_peer_pid = int.from_bytes(raw[0:4], "little", signed=True)
                self.last_peer_uid = int.from_bytes(raw[4:8], "little", signed=False)
            except OSError:
                self.last_peer_pid = self.last_peer_uid = None
        return reader, writer

    async def request(self, kind: str, **payload: Any) -> dict[str, Any]:
        request_id = secrets.token_hex(16)
        reader, writer = await self._connect()
        try:
            await _write(writer, make_request(kind, request_id, self.instance_id,
                                              token=self.token, **payload), direction="request")
            message = decode_message(await _read_line(reader), direction="response")
            if message["request_id"] != request_id or message["instance_id"] != self.instance_id:
                raise ProtocolError("response identity does not match request")
            if message["type"] == "error":
                raise WorkerRPCError(
                    str(message.get("code") or "rpc_error"),
                    str(message.get("message") or "Worker RPC failed"),
                    message.get("details"),
                )
            return message
        finally:
            writer.close()
            await writer.wait_closed()

    async def hello(self) -> dict[str, Any]:
        return await self.request("hello")

    async def health(self) -> dict[str, Any]:
        try:
            return await self.request("health")
        except TimeoutError as exc:
            # Keep an RPC/socket timeout distinct from the supervisor's outer
            # heartbeat deadline.  The latter may be deferred during an
            # active Run; a failed connection must still restart the Worker.
            raise WorkerRPCError("health_timeout", "Worker health RPC timed out") from exc

    async def activate(self) -> dict[str, Any]:
        return await self.request("worker.activate")

    async def reload(self, *, force: bool = False) -> dict[str, Any]:
        """Reload the Worker's installed capability generation."""
        return await self.request("worker.reload", force=bool(force))

    async def catalog(self) -> dict[str, Any]:
        """Return a JSON-only view of the active Worker catalog."""
        return await self.request("worker.catalog")

    async def cancel(self, run_id: str) -> bool:
        return bool((await self.request("run.cancel", run_id=str(run_id))).get("accepted"))

    async def step_input(
        self,
        run_id: str,
        messages: tuple[SessionInboxMessage, ...],
    ) -> bool:
        message = await self.request(
            "run.step_input",
            run_id=str(run_id),
            messages=[inbox_message_to_dict(item) for item in messages],
        )
        return bool(message.get("accepted"))

    async def approval(self, run_id: str, action: str, payload: Mapping[str, Any]) -> bool:
        message = await self.request("approval.result", run_id=str(run_id), action=action, payload=dict(payload))
        return bool(message.get("accepted"))

    async def shutdown(self) -> bool:
        return bool((await self.request("shutdown")).get("accepted"))

    async def run_start(self, payload: Mapping[str, Any], on_event: Callable[[dict[str, Any]], Awaitable[None]]) -> dict[str, Any]:
        request_id = secrets.token_hex(16)
        reader, writer = await self._connect()
        run_id = str(payload.get("run_id") or "")
        try:
            await _write(writer, make_request("run.start", request_id, self.instance_id,
                                              token=self.token, **dict(payload)), direction="request")
            accepted = decode_message(await _read_line(reader), direction="response")
            if accepted["request_id"] != request_id or accepted["type"] not in {"run.accepted", "error"}:
                raise ProtocolError("invalid run.accepted response")
            if accepted["type"] == "error":
                # 沿用同一套保真规则：Worker 拒绝了这次 Run 时也带码和细节，
                # 抛成普通 RuntimeError 会让 supervisor 一律盖成 worker_rpc_error。
                raise WorkerRPCError(
                    str(accepted.get("code") or "rpc_error"),
                    str(accepted.get("message") or "Worker rejected Run"),
                    accepted.get("details"),
                )
            if str(accepted.get("run_id") or "") != run_id:
                raise ProtocolError("run.accepted identity does not match request")
            await on_event(accepted)
            expected_sequence = 1
            while True:
                message = decode_message(await _read_line(reader), direction="response")
                if message["request_id"] != request_id or message["instance_id"] != self.instance_id:
                    raise ProtocolError("Run stream identity does not match request")
                if message["type"] == "run.event":
                    if str(message.get("run_id") or "") != run_id:
                        raise ProtocolError("Run event identity does not match request")
                    if int(message.get("sequence", 0)) != expected_sequence:
                        raise ProtocolError("Run event sequence is not contiguous")
                    if str(message.get("session_id") or "") != str(payload.get("session_id") or ""):
                        raise ProtocolError("Run event session does not match request")
                    expected_sequence += 1
                elif message["type"] == "run.result":
                    if str(message.get("run_id") or "") != run_id:
                        raise ProtocolError("Run result identity does not match request")
                elif message["type"] == "error":
                    # Worker 在流中途失败时发错误帧。以前这里只认 run.event /
                    # run.result，错误帧被跳过，连接随后关闭，客户端退化成
                    # EOFError，真正的 code/details 全部丢失。
                    raise WorkerRPCError(
                        str(message.get("code") or "rpc_error"),
                        str(message.get("message") or "Worker RPC failed"),
                        message.get("details"),
                    )
                await on_event(message)
                if message["type"] == "run.result":
                    return message
        finally:
            writer.close()
            await writer.wait_closed()


WorkerDispatch = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any] | RunHandle | None]]


class WorkerRPCServer:
    """Worker-side RPC server.

    ``dispatch`` returns a RunHandle for ``run.start``; all other messages
    return ordinary response payloads. The server owns no policy decisions.
    """

    def __init__(self, socket_path: str | Path, instance_id: str,
                 dispatch: WorkerDispatch, *, token: str):
        self.socket_path = Path(socket_path)
        self.instance_id = str(instance_id)
        self.token = str(token).strip()
        if not self.token:
            raise ValueError("Worker RPC token must not be empty")
        self.dispatch = dispatch
        self._server: asyncio.AbstractServer | None = None
        self._closed = asyncio.Event()
        self._run_sequences: dict[str, int] = {}

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.socket_path), limit=STREAM_LIMIT
        )
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self._closed.set()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("socket")
        # 已发出 run.accepted 的 Run：此后协议只允许一次 run.result 收尾。
        accepted_run_id: str | None = None
        try:
            if peer is not None and hasattr(socket, "SO_PEERCRED"):
                # The socket is 0600, but retain a peer credential check so a
                # future chmod/configuration error cannot silently widen trust.
                creds = peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                uid = int.from_bytes(creds[4:8], "little")
                if uid != os.getuid():
                    raise ProtocolError("Worker RPC peer uid mismatch")
            message = decode_message(await _read_line(reader), direction="request")
            if message["instance_id"] != self.instance_id:
                raise ProtocolError("Worker RPC instance mismatch")
            supplied_token = str(message.get("token") or "")
            if not hmac.compare_digest(supplied_token, self.token):
                raise WorkerRPCError("auth_error", "Worker RPC authentication failed")
            # The dispatch contract never receives the transport credential.
            # Keep it out of plugin-facing messages and any downstream logs.
            message = dict(message)
            message.pop("token", None)
            kind = str(message["type"])
            value = await self.dispatch(kind, message)
            if kind == "run.start":
                if not isinstance(value, RunHandle):
                    raise ProtocolError("run.start dispatch did not return RunHandle")
                run_id = str(message["run_id"])
                await _write(writer, make_response("run.accepted", message["request_id"], self.instance_id, run_id=run_id), direction="response")
                accepted_run_id = run_id
                sequence = 0
                async for event in value:
                    sequence += 1
                    previous = self._run_sequences.get(run_id, 0)
                    if sequence != previous + 1:
                        raise ProtocolError("Worker event sequence is not contiguous")
                    self._run_sequences[run_id] = sequence
                    await _write(writer, make_response(
                        "run.event", message["request_id"], self.instance_id,
                        run_id=run_id, session_id=value.session_id, sequence=sequence,
                        event={"kind": event.kind, "payload": dict(event.payload)},
                    ), direction="response")
                result = await value.wait()
                await _write(writer, make_response(
                    "run.result", message["request_id"], self.instance_id,
                    run_id=run_id, result=encode_result(result),
                ), direction="response")
                self._run_sequences.pop(run_id, None)
            else:
                response_kind = {
                    "hello": "hello.result", "health": "health.result", "shutdown": "shutdown.ack",
                    "worker.activate": "worker.activate.result",
                    "worker.reload": "worker.reload.result", "worker.catalog": "worker.catalog.result",
                    "run.cancel": "run.cancel.result", "run.step_input": "run.step_input.result",
                    "approval.result": "approval.result",
                }.get(kind, "error")
                if response_kind == "error":
                    raise ProtocolError(f"unsupported request type: {kind}")
                # ``dispatch`` implementations may return a complete view
                # (hello includes instance_id for attestation).  Protocol
                # envelope fields are owned by the RPC server; do not pass a
                # duplicate ``instance_id``/request header into response().
                payload = dict(value or {})
                for field in ("protocol", "type", "request_id", "instance_id"):
                    payload.pop(field, None)
                if kind in {"run.cancel", "run.step_input", "approval.result"}:
                    # Run-control response contracts carry the controlled Run
                    # identity.  The Worker dispatch result intentionally only
                    # contains its business value, so restore the kernel-local
                    # request identity at the RPC boundary.
                    payload["run_id"] = str(message["run_id"])
                await _write(writer, make_response(
                    response_kind, message["request_id"], self.instance_id,
                    **payload,
                ), direction="response")
                if kind == "shutdown":
                    await self.close()
        except (EOFError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001 - protocol boundary
            try:
                if isinstance(exc, WorkerRPCError):
                    code = exc.code
                    details = exc.details
                elif isinstance(exc, (FileNotFoundError, KeyError)):
                    code = "not_found"
                    details = {}
                elif isinstance(exc, (TypeError, ValueError)):
                    code = "validation_error"
                    details = {}
                elif isinstance(exc, ConnectionError):
                    code = "upstream_unavailable"
                    details = {}
                else:
                    code = "rpc_error"
                    details = {}
                request_id = (
                    str(message.get("request_id", "error"))
                    if "message" in locals() else "error"
                )
                reason = str(exc).strip() or type(exc).__name__
                if accepted_run_id is not None:
                    # 已经 run.accepted 的流再发 error 帧，客户端会当作坏帧，
                    # 连接关闭后退化成 EOFError，失败原因就丢了。改发一次
                    # 失败的 run.result，让终态自带 code/details。
                    await _write(writer, make_response(
                        "run.result", request_id, self.instance_id,
                        run_id=accepted_run_id,
                        result=encode_result(RunResult(
                            request_id=accepted_run_id,
                            status="failed",
                            error=RunError(code=code, message=reason,
                                           details=details),
                        )),
                    ), direction="response")
                    self._run_sequences.pop(accepted_run_id, None)
                else:
                    # 空 details 不出现在帧里：控制面与 Worker 虽是同一个包，
                    # 升级窗口里仍可能「新 Worker + 未重启的旧 Control」，而旧
                    # Control 的严格校验会把未知字段当成坏帧整帧丢弃。常见错误
                    # 的 details 本来就是空的，省掉它就足以让这条路径保持兼容。
                    frame = {"code": code, "message": reason}
                    if details:
                        frame["details"] = details
                    await _write(writer, make_response(
                        "error", request_id, self.instance_id,
                        **frame,
                    ), direction="response")
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


__all__ = ["WorkerRPCClient", "WorkerRPCError", "WorkerRPCServer", "default_socket_root", "make_instance_socket"]
