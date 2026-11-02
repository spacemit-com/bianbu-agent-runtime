"""Runtime 只经 HTTP 调用端侧服务，任何失败都交还给 Runtime 模型。

端侧边界是进程外的服务：Runtime 既不导入 bibit_intent 实现，也不从异常类型猜
路由。失败时唯一的判断是「请求是否已经交给端侧」——交出去了就可能已经有副作用，
回退给模型时要带上「端侧执行状态未确认」。
"""
from __future__ import annotations

import asyncio
import builtins
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import time

import pytest

from bianbu_agent.hooks import HookLoadContext
from tests.builtin_hook_support import ScriptUnitLoader
from bianbu_agent.plugins import RunContext

ROOT = Path(__file__).resolve().parents[3] / 'resources/plugins/bianbu.bibit.intent'


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _serve(handler):
    """端侧服务替身：handler(method, path, payload) -> (status, body bytes)。"""

    recorded: list[tuple[str, str, object]] = []

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
            recorded.append((method, self.path, payload))
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


def _outcome(handled: bool, reply: str, **extra) -> tuple[int, bytes]:
    payload = {
        "handled": handled,
        "reply": reply,
        "route": "rule" if handled else "cloud",
        "reason": "writing_task",
        "metadata": {"token": "private", "latency_ms": 8},
        **extra,
    }
    return 200, json.dumps(payload).encode()


@pytest.fixture
def hook_factory(tmp_path, monkeypatch):
    # Existing source trees, injected pipelines and old mode settings must not
    # switch production back to in-process execution.
    (tmp_path / 'bibit_intent').mkdir()
    monkeypatch.setenv('BIBIT_INTENT_SRC', str(tmp_path))
    monkeypatch.setenv('BIBIT_INTENT_MODE', 'embedded')
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == 'bibit_intent' or name.startswith('bibit_intent.'):
            raise AssertionError('Runtime must not import Bibit implementation')
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', guarded)
    servers = []

    def load(handler):
        server, recorded = _serve(handler)
        servers.append(server)
        monkeypatch.setenv(
            'BIBIT_INTENT_BASE_URL', f'http://127.0.0.1:{server.server_port}'
        )
        hook = ScriptUnitLoader().load(
            ROOT / 'hooks/bibit-intent.before_run',
            context=HookLoadContext(workspace_root=tmp_path, data_root=tmp_path / 'data', owner_plugin_id='bianbu.bibit.intent', services={'bibit_pipeline': object()}),
            plugin_id='bianbu.bibit.intent',
        )
        return hook, recorded

    yield load
    for server in servers:
        server.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize('handled,reply,want', [(True, 'done', 'done'), (True, '', '端侧请求已处理。'), (False, '', None)])
async def test_http_outcome_without_source_import(hook_factory, handled, reply, want):
    before = list(sys.path)
    hook, recorded = hook_factory(lambda method, path, payload: _outcome(handled, reply))
    patch = await hook.handler(RunContext('r', 's', 'a', '搜索AI资讯并生成可视化网页报告'))
    assert sys.path == before
    assert patch.direct_reply == want
    assert 'token' not in patch.metadata
    assert patch.metadata['latency_ms'] == 8
    method, path, payload = recorded[0]
    assert (method, path) == ('POST', '/v1/runs')
    assert payload['prompt'] == '搜索AI资讯并生成可视化网页报告'
    assert payload['timeout_ms'] == 90000
    assert payload['stream'] is False


@pytest.mark.asyncio
async def test_connect_failure_leaves_the_request_unsubmitted(hook_factory, monkeypatch, caplog):
    # 端侧服务没起来：请求从未发出，回退给模型时不必先确认端侧状态。
    monkeypatch.setenv('BIBIT_INTENT_BASE_URL', f'http://127.0.0.1:{_free_port()}')
    hook = ScriptUnitLoader().load(
        ROOT / 'hooks/bibit-intent.before_run',
        context=HookLoadContext(workspace_root=ROOT, data_root=ROOT / 'data', owner_plugin_id='bianbu.bibit.intent', services={'bibit_pipeline': object()}),
        plugin_id='bianbu.bibit.intent',
    )
    patch = await hook.handler(RunContext('r', 's', 'a', '执行操作'))
    assert patch.direct_reply is None
    assert patch.metadata['route'] == 'cloud'
    assert patch.metadata['reason'] == 'edge_error'
    assert patch.metadata['edge_error_type'] == 'ConnectionRefusedError'
    assert patch.context_sections == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('respond', [
    lambda method, path, payload: (500, b'{"detail": "boom"}'),
    lambda method, path, payload: (200, b'not json'),
    lambda method, path, payload: (200, b'[]'),
    lambda method, path, payload: (200, b'{"handled": "false"}'),
    lambda method, path, payload: (200, b'{"handled": false, "metadata": []}'),
])
async def test_a_submitted_request_that_fails_asks_the_model_to_verify(hook_factory, respond, caplog):
    hook, _ = hook_factory(respond)
    patch = await hook.handler(RunContext('r', 's', 'a', '执行操作'))
    assert patch.direct_reply is None
    assert patch.metadata['route'] == 'cloud'
    assert patch.metadata['reason'] == 'edge_error'
    # 端侧可能已经动手了，云端要先核实再补做。
    assert '端侧执行状态未确认' in ''.join(patch.context_sections)
    assert 'boom' not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_stops_edge_run_and_falls_back_to_runtime(hook_factory):
    started = threading.Event()
    stopped = threading.Event()
    release = threading.Event()

    def respond(method, path, payload):
        if method == 'DELETE':
            stopped.set()
            return 204, b''
        started.set()
        release.wait(10.0)
        return _outcome(True, 'done')

    hook, recorded = hook_factory(respond)
    task = asyncio.create_task(hook.handler(RunContext('r', 's', 'a', '执行操作')))
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        patch = await asyncio.wait_for(task, 5)
    finally:
        release.set()
    assert stopped.is_set()
    assert patch.direct_reply is None
    assert patch.metadata['reason'] == 'edge_error'
    assert recorded[-1][0] == 'DELETE'


@pytest.mark.asyncio
async def test_runtime_hook_deadline_stops_edge_run_and_falls_back_to_runtime(hook_factory):
    from dataclasses import replace
    from bianbu_agent.plugins import HookPoint, PluginContributions, PluginGeneration, PluginRuntime

    cancelled = threading.Event()
    release = threading.Event()

    def respond(method, path, payload):
        if method == 'DELETE':
            cancelled.set()
            return 204, b''
        release.wait(10.0)
        return _outcome(True, 'done')

    hook = replace(hook_factory(respond)[0], timeout_seconds=0.02)
    generation = PluginGeneration(
        id=1,
        plugins={'bianbu.bibit.intent': PluginRuntime(
            plugin_id='bianbu.bibit.intent', instance=object(), config={},
            contributions=PluginContributions(hooks=(hook,)),
        )}, statuses={},
    )
    try:
        patch = await generation.run_hooks(HookPoint.BEFORE_RUN, RunContext('r', 's', 'a', '执行操作'))
    finally:
        release.set()
    assert cancelled.is_set()
    assert patch.direct_reply is None
    assert patch.metadata['bianbu.bibit.intent']['reason'] == 'edge_error'


@pytest.mark.asyncio
async def test_a_slow_but_legal_edge_answer_still_reaches_the_user(hook_factory):
    # 端侧自己执行久一点不算建连超时：读预算远大于建连预算。
    def slow(method, path, payload):
        time.sleep(0.3)
        return _outcome(True, 'done')

    hook, _ = hook_factory(slow)
    patch = await hook.handler(RunContext('r', 's', 'a', '执行操作'))
    assert patch.direct_reply == 'done'


def test_plugin_ships_no_http_client_dependency():
    """Hook 是短命进程：端侧边界不该再拉一整套 HTTP 客户端（K3 上 ~0.5s）。"""
    scripts = ROOT / 'scripts'
    for name in ('before_run.py', 'after_tool.py', 'after_appcli.py'):
        source = (scripts / name).read_text(encoding='utf-8')
        assert 'import httpx' not in source
    spec = importlib.util.spec_from_file_location(
        '_test_bibit_edge_http_cost', ROOT / 'lib' / 'edge_http.py'
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.Client is not None
    # 客户端只依赖标准库：它的模块命名空间里不应出现 httpx 这类第三方名。
    third_party = [
        name for name, value in vars(module).items()
        if getattr(value, '__name__', '') == 'httpx'
    ]
    assert third_party == []
