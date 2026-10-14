import builtins
import asyncio
import sys
from pathlib import Path

import httpx
import pytest

from bianbu_agent.hooks import HookLoadContext
from tests.builtin_hook_support import ScriptUnitLoader
from bianbu_agent.plugins import RunContext

ROOT = Path(__file__).resolve().parents[3] / 'resources/plugins/bianbu.bibit.intent'


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
    native_client = httpx.AsyncClient

    def load(handler):
        monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: native_client(transport=httpx.MockTransport(handler), **kwargs))
        return ScriptUnitLoader().load(
            ROOT / 'hooks/bibit-intent.before_run',
            context=HookLoadContext(workspace_root=tmp_path, data_root=tmp_path / 'data', owner_plugin_id='bianbu.bibit.intent', services={'bibit_pipeline': object()}),
            plugin_id='bianbu.bibit.intent',
        )
    return load


@pytest.mark.asyncio
@pytest.mark.parametrize('handled,reply,want', [(True, 'done', 'done'), (True, '', '端侧请求已处理。'), (False, '', None)])
async def test_http_outcome_without_source_import(hook_factory, handled, reply, want):
    async def respond(request):
        return httpx.Response(200, json={'handled': handled, 'reply': reply, 'route': 'rule' if handled else 'cloud', 'reason': 'writing_task', 'metadata': {'token': 'private', 'latency_ms': 8}})
    before = list(sys.path)
    hook = hook_factory(respond)
    patch = await hook.handler(RunContext('r', 's', 'a', '搜索AI资讯并生成可视化网页报告'))
    assert sys.path == before
    assert patch.direct_reply == want
    assert 'token' not in patch.metadata
    assert patch.metadata['latency_ms'] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize('error,submitted', [(httpx.ConnectError, False), (httpx.ConnectTimeout, False), (httpx.ReadTimeout, True), (httpx.ReadError, True)])
async def test_any_service_failure_hands_the_request_to_the_runtime(hook_factory, error, submitted, caplog):
    async def respond(request):
        raise error('private request detail', request=request)
    patch = await hook_factory(respond).handler(RunContext('r', 's', 'a', '执行操作'))
    assert patch.direct_reply is None
    assert patch.metadata['route'] == 'cloud'
    assert patch.metadata['reason'] == 'edge_error'
    assert patch.metadata['edge_error_type'] == error.__name__
    # 提交后的失败可能已产生副作用，云端需要先核实再补做。
    assert ('端侧执行状态未确认' in ''.join(patch.context_sections)) == submitted
    assert 'private request detail' not in caplog.text


@pytest.mark.asyncio
async def test_cancellation_stops_edge_run_and_falls_back_to_runtime(hook_factory):
    started = asyncio.Event()
    stopped = asyncio.Event()
    run_ids = []
    async def respond(request):
        import json
        if request.method == 'DELETE':
            assert request.url.path == '/v1/runs/' + run_ids[0]
            stopped.set()
            return httpx.Response(204)
        run_ids.append(json.loads(request.content)['run_id'])
        started.set()
        await asyncio.Event().wait()
    hook = hook_factory(respond)
    task = asyncio.create_task(hook.handler(RunContext('r', 's', 'a', '执行操作')))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    patch = await asyncio.wait_for(task, 1)
    assert stopped.is_set()
    assert patch.direct_reply is None
    assert patch.metadata['reason'] == 'edge_error'


@pytest.mark.asyncio
async def test_runtime_hook_deadline_stops_edge_run_and_falls_back_to_runtime(hook_factory):
    from dataclasses import replace
    from bianbu_agent.plugins import HookPoint, PluginContributions, PluginGeneration, PluginRuntime

    cancelled = asyncio.Event()
    async def respond(request):
        if request.method == 'DELETE':
            cancelled.set()
            return httpx.Response(204)
        await asyncio.Event().wait()

    hook = replace(hook_factory(respond), timeout_seconds=0.02)
    generation = PluginGeneration(
        id=1,
        plugins={'bianbu.bibit.intent': PluginRuntime(
            plugin_id='bianbu.bibit.intent', instance=object(), config={},
            contributions=PluginContributions(hooks=(hook,)),
        )}, statuses={},
    )
    patch = await generation.run_hooks(HookPoint.BEFORE_RUN, RunContext('r', 's', 'a', '执行操作'))
    assert cancelled.is_set()
    assert patch.direct_reply is None
    assert patch.metadata['bianbu.bibit.intent']['reason'] == 'edge_error'


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [[], {'handled': 'false'}, {'handled': False, 'metadata': []}])
async def test_malformed_result_falls_back_to_runtime(hook_factory, payload):
    async def respond(request):
        if request.method == 'DELETE':
            return httpx.Response(404)
        return httpx.Response(200, json=payload)
    patch = await hook_factory(respond).handler(RunContext('r', 's', 'a', '执行操作'))
    assert patch.direct_reply is None
    assert patch.metadata['reason'] == 'edge_error'


@pytest.mark.asyncio
async def test_local_connect_deadline_does_not_shorten_execution_timeout(hook_factory):
    async def respond(request):
        timeout = request.extensions["timeout"]
        assert timeout["connect"] <= 5.0
        assert timeout["read"] == 110.0
        return httpx.Response(200, json={"handled": True, "reply": "done", "metadata": {}})
    patch = await hook_factory(respond).handler(RunContext('r', 's', 'a', '执行操作'))
    assert patch.direct_reply == "done"
