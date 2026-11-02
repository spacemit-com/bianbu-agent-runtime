"""lib/edge_http.py：端侧回环边界只用标准库，且语义与旧客户端一致。

Hook 在请求路径上出生入死，导入整套 HTTP 客户端在 K3 上要 ~0.5s；这条边界只有
JSON POST 与取消 DELETE，所以换成 http.client。这里钉住三件事：请求怎么发、
"没发出去" 与 "发出去了" 怎么区分、取消时不能挂着读预算不放。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

import http.client

LIB = Path(__file__).resolve().parents[3] / "resources/plugins/bianbu.bibit.intent/lib"


def _load_client():
    spec = importlib.util.spec_from_file_location("_test_bibit_edge_http", LIB / "edge_http.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


edge_http = _load_client()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(handler):
    """本地 HTTP 服务，记录收到的请求；handler(method, path, payload) -> (status, body)。"""

    recorded: list[tuple[str, str, object, dict[str, str]]] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server 的命名约定
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def _handle(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            payload = json.loads(raw) if raw else None
            recorded.append((method, self.path, payload, dict(self.headers)))
            status, body = handler(method, self.path, payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    class _Server(ThreadingHTTPServer):
        # 客户端按设计会中途关掉连接（取消、读超时）：别把噪声打进测试输出。
        def handle_error(self, request, client_address) -> None:
            return

    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, recorded


def _ok(payload: dict) -> tuple[int, bytes]:
    return 200, json.dumps(payload).encode()


def test_post_json_sends_one_closed_json_request():
    server, recorded = _serve(lambda method, path, payload: _ok({"handled": True}))
    try:
        client = edge_http.Client(base_url=f"http://127.0.0.1:{server.server_port}/prefix/")
        response = client.post_json("/v1/runs", {"prompt": "打开终端"}, read_timeout=5.0)
    finally:
        server.shutdown()

    assert response.status == 200
    assert response.payload == {"handled": True}
    method, path, payload, headers = recorded[0]
    assert (method, path) == ("POST", "/prefix/v1/runs")
    assert payload == {"prompt": "打开终端"}
    assert headers["Content-Type"] == "application/json"
    # 每次调用都收连接：端侧边界是一次性调用，不留 keep-alive 状态。
    assert headers["Connection"] == "close"


def test_connect_failure_is_reported_as_not_sent():
    client = edge_http.Client(base_url=f"http://127.0.0.1:{_free_port()}")
    with pytest.raises(edge_http.RequestNotSent) as failure:
        client.post_json("/v1/runs", {}, read_timeout=5.0)
    # 建连都没成，重试不会重复执行端侧动作。
    assert isinstance(failure.value.cause, OSError)


def test_unsupported_base_url_never_reaches_the_network():
    client = edge_http.Client(base_url="https://edge.example.test")
    with pytest.raises(edge_http.RequestNotSent) as failure:
        client.post_json("/v1/runs", {}, read_timeout=5.0)
    assert isinstance(failure.value.cause, ValueError)


def test_non_2xx_answers_stay_submitted():
    server, _ = _serve(lambda method, path, payload: (503, b'{"detail": "busy"}'))
    try:
        client = edge_http.Client(base_url=f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(edge_http.UnexpectedStatus) as failure:
            client.post_json("/v1/runs", {}, read_timeout=5.0)
    finally:
        server.shutdown()
    assert failure.value.status == 503


def test_read_timeout_is_not_a_not_sent_failure():
    release = threading.Event()

    def slow(method, path, payload):
        release.wait(5.0)
        return _ok({"handled": True})

    server, _ = _serve(slow)
    try:
        client = edge_http.Client(base_url=f"http://127.0.0.1:{server.server_port}")
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            client.post_json("/v1/runs", {}, read_timeout=0.3)
        assert time.monotonic() - started < 3.0
    finally:
        release.set()
        server.shutdown()


def test_delete_reports_404_so_the_caller_can_tolerate_a_finished_run():
    server, recorded = _serve(lambda method, path, payload: (404, b""))
    try:
        client = edge_http.Client(base_url=f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(edge_http.UnexpectedStatus) as failure:
            client.delete(edge_http.run_path("run-1"), read_timeout=2.0)
    finally:
        server.shutdown()
    assert failure.value.status == 404
    assert recorded[0][:2] == ("DELETE", "/v1/runs/run-1")


def test_run_path_keeps_one_path_segment():
    assert edge_http.run_path("run/../etc") == "/v1/runs/run%2F..%2Fetc"


def test_connect_budget_does_not_consume_the_read_budget(monkeypatch):
    seen: dict[str, object] = {}

    class _Sock:
        def settimeout(self, value: float) -> None:
            seen["read_timeout"] = value

    class _Connection:
        def __init__(self, host, port, timeout):
            seen["connect_timeout"] = timeout
            self.sock = _Sock()

        def connect(self) -> None:
            seen["connected"] = True

        def request(self, method, target, body=None, headers=None) -> None:
            seen["request"] = (method, target, body)

        def getresponse(self):
            raise OSError("stop here")

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr(http.client, "HTTPConnection", _Connection)
    client = edge_http.Client(
        base_url="http://127.0.0.1:1/v1", connect_timeout=min(5.0, 110.0)
    )
    with pytest.raises(OSError):
        client.post_json("/v1/runs", {"handled": True}, read_timeout=110.0)

    assert seen["connected"] is True
    assert seen["connect_timeout"] == 5.0
    # 建连短预算、读用调用方的完整预算：端侧自己执行久一点不算 Hook 超时。
    assert seen["read_timeout"] == 110.0
    assert seen["closed"] is True


def test_cancelling_a_call_releases_the_socket_without_waiting_for_the_read_timeout():
    seen = threading.Event()
    release = threading.Event()

    def hang(method, path, payload):
        seen.set()
        release.wait(30.0)
        return _ok({"handled": True})

    server, _ = _serve(hang)

    async def main() -> None:
        client = edge_http.Client(base_url=f"http://127.0.0.1:{server.server_port}")
        task = asyncio.create_task(client.apost_json("/v1/runs", {}, read_timeout=30.0))
        while not seen.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        started = time.monotonic()
        # asyncio.run 退出时会等工作线程：没关掉套接字就会卡满读预算。
        asyncio.run(main())
        assert time.monotonic() - started < 5.0
    finally:
        release.set()
        server.shutdown()
