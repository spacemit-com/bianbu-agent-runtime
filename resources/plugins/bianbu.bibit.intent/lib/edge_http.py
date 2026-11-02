"""Bibit 端侧服务回环 HTTP 边界（只依赖标准库）。

Hook 是在请求路径上的短命进程：进程内导入整套 HTTP 客户端在 K3（riscv64）
上要 ~0.47s，而 `http.client` 只要 ~0.07s，这段导入时间直接落在用户可见的
首字延迟上（"打开终端" 实测占端到端约 1/4）。

端侧边界是固定的回环调用：JSON POST `/v1/runs`、取消用 DELETE，没有重定向、
Cookie 与代理，所以标准库客户端就够了。请求在线程里执行；Hook 被取消时
`abort()` 直接关掉套接字，避免 `asyncio.run` 退出时还要等剩下的读超时。
"""
from __future__ import annotations

import asyncio
import http.client
from dataclasses import dataclass
import json
import socket
import threading
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

#: 连本地服务不该消耗它的执行预算。
CONNECT_TIMEOUT_SECONDS = 5.0

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "Client",
    "RequestNotSent",
    "Response",
    "UnexpectedStatus",
]


class RequestNotSent(RuntimeError):
    """请求没有发出去（建连失败）：重试不会重复执行端侧动作。"""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(f"edge request was not sent: {type(cause).__name__}")
        self.cause = cause


class UnexpectedStatus(RuntimeError):
    """服务返回了 2xx 之外的应答。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"edge service answered {status}")
        self.status = int(status)


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    payload: Any


class Client:
    """一个回环 base URL 的同步客户端，按需异步化。"""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = str(base_url).strip().rstrip("/")
        self._connect_timeout = float(connect_timeout)
        self._lock = threading.Lock()
        self._connection: http.client.HTTPConnection | None = None

    @property
    def base_url(self) -> str:
        return self._base_url

    def abort(self) -> None:
        """丢掉在途请求的套接字，让被取消的 Hook 立刻退出。

        `asyncio.run` 退出时会等待工作线程，而被取消的请求还在等读超时；这里
        shutdown 套接字让那次读立刻返回，进程不必等满整个读预算。
        """
        with self._lock:
            connection, self._connection = self._connection, None
        if connection is None:
            return
        sock = getattr(connection, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        try:
            connection.close()
        except OSError:
            pass

    def post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        read_timeout: float,
    ) -> Response:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._request("POST", path, body=body, read_timeout=read_timeout)

    def delete(self, path: str, *, read_timeout: float) -> Response:
        return self._request("DELETE", path, body=None, read_timeout=read_timeout)

    async def apost_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        read_timeout: float,
    ) -> Response:
        return await self._in_thread(self.post_json, path, payload, read_timeout=read_timeout)

    async def adelete(self, path: str, *, read_timeout: float) -> Response:
        return await self._in_thread(self.delete, path, read_timeout=read_timeout)

    async def _in_thread(self, function, *args, **kwargs):
        try:
            return await asyncio.to_thread(function, *args, **kwargs)
        except asyncio.CancelledError:
            # 被取消的请求不再占着连接，调用方随后还要用同一客户端发 DELETE。
            self.abort()
            raise

    # -- 请求构造 --------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None,
        read_timeout: float,
    ) -> Response:
        connection = self._connect()
        try:
            # 建连是本地动作：读预算留给服务自己的执行时间，建连用短预算。
            connection.sock.settimeout(read_timeout)
            headers = {"Accept": "application/json", "Connection": "close"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            connection.request(method, self._target(path), body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            status = int(response.status)
        finally:
            with self._lock:
                if self._connection is connection:
                    self._connection = None
            connection.close()
        if not 200 <= status < 300:
            raise UnexpectedStatus(status)
        return Response(status=status, payload=_payload(raw))

    def _connect(self) -> http.client.HTTPConnection:
        parts = urlsplit(self._base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise RequestNotSent(ValueError(f"unsupported edge URL: {self._base_url}"))
        connection = http.client.HTTPConnection(
            parts.hostname, parts.port or 80, timeout=self._connect_timeout
        )
        try:
            connection.connect()
        except OSError as exc:
            raise RequestNotSent(exc) from exc
        with self._lock:
            self._connection = connection
        return connection

    def _target(self, path: str) -> str:
        prefix = urlsplit(self._base_url).path.rstrip("/")
        suffix = "/" + str(path).lstrip("/")
        return f"{prefix}{suffix}"


def run_path(run_id: str) -> str:
    """`/v1/runs/<run_id>`：run_id 直接来自调用方，按路径段转义。"""
    return f"/v1/runs/{quote(str(run_id), safe='')}"


def _payload(raw: bytes) -> Any:
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))
