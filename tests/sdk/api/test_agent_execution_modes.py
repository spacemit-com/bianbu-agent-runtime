"""专家编辑不再携带执行模式：host / sandbox 由 securityd 按安全设置推导。

这些用例覆盖 Agent CRUD 与 Worker 生命周期的交互：哪些编辑只改展示、哪些编辑
需要重建 Worker，以及请求里出现 ``requested_mode`` 时必须被拒绝。
"""
from types import SimpleNamespace

import pytest

from bianbu_agent.security.runtime_process.supervisor import AgentWorkerPool
from tests.sdk.api.test_agents import _api


class Worker:
    def __init__(self, mode='auto', fail_host=False):
        self.requested_mode = mode
        self.ready = True
        self.identity = SimpleNamespace(effective_mode=mode)
        self.record = None
        self.fail_host = fail_host
        self.stops = 0
        self.closed = False

    async def stop(self, **kwargs):
        self.stops += 1
        self.ready = False
        self.identity = None

    async def close(self, **kwargs):
        self.closed = True
        await self.stop()

    async def start(self):
        if self.fail_host and self.requested_mode == 'host':
            raise RuntimeError('host unavailable')
        self.ready = True
        self.identity = SimpleNamespace(effective_mode=self.requested_mode)
        return self.identity

    def status_snapshot(self):
        return {'state': 'running' if self.ready else 'stopped',
                'effective_mode': self.identity.effective_mode if self.identity else None}


@pytest.mark.asyncio
async def test_agent_edit_never_restarts_worker_for_execution_mode(tmp_path):
    """安全设置决定执行模式，编辑专家本身不再触发模式切换。"""
    async with _api(tmp_path) as (client, runtime):
        pool = AgentWorkerPool(runtime.config)
        worker = Worker()
        pool._supervisors['writer'] = worker
        runtime.worker_supervisor = pool
        response = await client.patch('/api/v1/agents/writer', json={'soul': 'Updated'})
        assert response.status_code == 200, response.text
        assert worker.stops == 0
        assert not worker.closed


@pytest.mark.asyncio
async def test_agent_declaration_cannot_request_an_execution_mode(tmp_path):
    """Agent 声明不能指定边界：securityd 才是唯一写入方。"""
    async with _api(tmp_path) as (client, _runtime):
        await client.post('/api/v1/providers', json={
            'id': 'mode-provider', 'label': 'Mode',
            'base_url': 'https://model.test/v1', 'api_key': 'secret'})
        for section in ('execution', 'runtime'):
            response = await client.post('/api/v1/agents', json={
                'name': 'Mode test',
                'model': {'provider': 'mode-provider', 'name': 'test'},
                'metadata': {section: {'requested_mode': 'sandbox'}}})
            assert response.status_code == 422, response.text
            assert f'metadata.{section}.requested_mode is retired' in response.text


@pytest.mark.asyncio
async def test_execution_view_reports_the_worker_not_a_declared_mode(tmp_path):
    """``execution`` 只反映 Worker 实况，不再回显 Agent 声明的模式。"""
    async with _api(tmp_path) as (client, runtime):
        pool = AgentWorkerPool(runtime.config)
        pool._supervisors['writer'] = Worker(mode='sandbox')
        runtime.worker_supervisor = pool
        data = (await client.get('/api/v1/agents/writer')).json()['data']
        assert data['execution']['effective_mode'] == 'sandbox'
        assert 'requested_mode' not in data['execution']


@pytest.mark.asyncio
async def test_create_persists_agent_without_starting_worker(tmp_path, monkeypatch):
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    async def unavailable_start(self):
        raise RuntimeError('slow or unavailable worker must not block creation')

    monkeypatch.setattr(WorkerSupervisor, 'start', unavailable_start)
    async with _api(tmp_path) as (client, runtime):
        await client.post('/api/v1/providers', json={
            'id': 'mode-provider', 'label': 'Mode',
            'base_url': 'https://model.test/v1', 'api_key': 'secret'})
        pool = AgentWorkerPool(runtime.config)
        runtime.worker_supervisor = pool
        response = await client.post('/api/v1/agents', json={
            'name': 'Mode test', 'model': {'provider': 'mode-provider', 'name': 'test'}})
        assert response.status_code == 201, response.text
        data = response.json()['data']
        assert data['execution']['state'] == 'stopped'
        assert data['execution'].get('effective_mode') is None
        stored = await runtime.agents.get(data['id'])
        assert runtime._requested_execution_mode(stored) == 'auto'
        from bianbu_agent.security.runtime_process.supervisor import ControlRunManager
        manager = ControlRunManager(runtime._run_store, pool)
        handle = await manager.run('first-use', 'hello', agent_id=data['id'])
        result = await handle.wait()
        assert result.status == 'failed'
        assert 'slow or unavailable worker' in result.error.message
        assert (await runtime.agents.get(data['id'])).id == data['id']
        await manager.close()


@pytest.mark.asyncio
async def test_failed_registration_retires_worker_and_removes_definition(tmp_path):
    async with _api(tmp_path) as (client, runtime):
        await client.post('/api/v1/providers', json={
            'id': 'mode-provider', 'label': 'Mode',
            'base_url': 'https://model.test/v1', 'api_key': 'secret'})
        pool = AgentWorkerPool(runtime.config)
        created_ids = []

        async def fail_registration(spec):
            created_ids.append(spec.id)
            raise RuntimeError('securityd unavailable')

        runtime.register_logical_agent = fail_registration
        runtime.worker_supervisor = pool
        response = await client.post('/api/v1/agents', json={
            'name': 'Fail registration',
            'model': {'provider': 'mode-provider', 'name': 'test'}})
        assert response.status_code == 503
        agent_id = created_ids[0]
        assert agent_id not in pool._supervisors
        assert agent_id in pool._retired_agents
        assert not any(a.id == agent_id for a in await runtime.agents.list())


@pytest.mark.asyncio
async def test_delete_waits_for_an_in_flight_registration(tmp_path):
    import asyncio
    async with _api(tmp_path) as (client, runtime):
        pool = AgentWorkerPool(runtime.config)
        worker = Worker()
        pool._supervisors['writer'] = worker
        runtime.worker_supervisor = pool
        registering, release = asyncio.Event(), asyncio.Event()

        async def register(spec):
            registering.set()
            await release.wait()

        runtime.register_logical_agent = register
        patch = asyncio.create_task(client.patch('/api/v1/agents/writer', json={'name': 'Renamed'}))
        await registering.wait()
        delete = asyncio.create_task(client.delete('/api/v1/agents/writer'))
        await asyncio.sleep(0.02)
        assert not delete.done()
        release.set()
        assert (await patch).status_code == 200
        assert (await delete).status_code == 204
        assert 'writer' not in pool._supervisors


@pytest.mark.asyncio
async def test_edit_idle_expert_keeps_it_stopped(tmp_path, monkeypatch):
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    async def fail_start(worker):
        raise RuntimeError('editing must not start an idle expert')

    monkeypatch.setattr(WorkerSupervisor, 'start', fail_start)
    async with _api(tmp_path) as (client, runtime):
        runtime.worker_supervisor = AgentWorkerPool(runtime.config)
        response = await client.patch('/api/v1/agents/writer', json={'name': 'Renamed'})
        assert response.status_code == 200, response.text
        assert response.json()['data']['execution']['state'] == 'stopped'
        assert runtime.worker_supervisor.for_agent('writer').requested_mode == 'auto'


@pytest.mark.asyncio
async def test_existing_run_keeps_model_and_next_run_reads_file_update(tmp_path):
    """Exercise real materialization/resolution, not just the persisted API view."""
    import asyncio
    from dataclasses import replace
    from bianbu_agent import RunEvent
    from bianbu_agent.agents import AgentModel, ManifestAgentStore
    from bianbu_agent.agents.manager import AgentManager
    from tests.sdk.api.test_agents import _file_backed_api

    started, finish = asyncio.Event(), asyncio.Event()
    seen = []
    class Engine:
        def __init__(self, agent, model):
            self.agent, self.model = agent, model
        async def stream(self, prompt, **kwargs):
            started.set()
            await finish.wait()
            seen.append((self.agent.soul, self.model.model))
            yield RunEvent('token', {'text': self.model.model})
            yield RunEvent('done', {})

    async with _file_backed_api(tmp_path) as (client, runtime):
        await client.post('/api/v1/providers', json={
            'id': 'edit-provider', 'base_url': 'https://model.test/v1', 'api_key': 'secret'})
        current = await runtime.agents.get('default')
        await runtime.agents.update(replace(current, soul='original prompt',
            model=AgentModel('edit-provider', 'original-model')))
        # Simulate the Worker's independently created file store, sharing only files.
        runtime.runs._agents = AgentManager(
            store=ManifestAgentStore(runtime.home), model_registry=runtime._models,
            generation_provider=lambda: runtime.plugin_runtime.current_generation,
        )
        runtime.runs._engine_builder = lambda agent, generation, model: Engine(agent, model)
        first = await runtime.runs.run('first', 'hello', agent_id='default')
        await asyncio.wait_for(started.wait(), 2)
        response = await client.patch('/api/v1/agents/default', json={
            'soul': 'updated prompt', 'model': {'provider': 'edit-provider', 'name': 'updated-model'}})
        assert response.status_code == 200, response.text
        finish.set()
        assert (await first.wait()).status == 'completed'
        second = await runtime.runs.run('second', 'hello', agent_id='default')
        assert (await second.wait()).status == 'completed'
        assert seen == [('original prompt', 'original-model'), ('updated prompt', 'updated-model')]

@pytest.mark.asyncio
async def test_capability_selection_applies_without_restarting_idle_worker(tmp_path):
    async with _api(tmp_path) as (client, runtime):
        pool = AgentWorkerPool(runtime.config)
        worker = Worker(mode='auto')
        pool._supervisors['writer'] = worker
        runtime.worker_supervisor = pool
        response = await client.patch('/api/v1/agents/writer', json={'tools': ['*'], 'skills': ['*']})
        assert response.status_code == 200, response.text
        assert worker.stops == 0
        spec = await runtime.agents.materialize('writer', runtime.plugin_runtime.current_generation)
        assert set(spec.tools) == set(runtime.plugin_runtime.current_generation.tools)
        assert set(spec.skills) == set(runtime.plugin_runtime.current_generation.skills)


@pytest.mark.asyncio
async def test_failed_name_registration_does_not_stop_unchanged_worker(tmp_path):
    async with _api(tmp_path) as (client, runtime):
        pool = AgentWorkerPool(runtime.config)
        worker = Worker(mode='auto')
        pool._supervisors['writer'] = worker
        runtime.worker_supervisor = pool
        async def fail_registration(spec):
            if spec.name == 'Renamed':
                raise RuntimeError('registration unavailable')
        runtime.register_logical_agent = fail_registration
        response = await client.patch('/api/v1/agents/writer', json={'name': 'Renamed'})
        assert response.status_code == 503
        assert (await runtime.agents.get('writer')).name == 'Writer'
        assert worker.stops == 0
        assert worker.ready


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', [False, True])
async def test_explicit_restart_rebuilds_only_idle_selected_worker(tmp_path, busy):
    async with _api(tmp_path) as (client, runtime):
        pool=AgentWorkerPool(runtime.config)
        worker=Worker(mode='auto'); other=Worker()
        pool._supervisors.update(writer=worker, other=other)
        pool.run_manager=SimpleNamespace(_active={'run':SimpleNamespace(agent_id='writer')} if busy else {})
        runtime.worker_supervisor=pool
        response=await client.post('/api/v1/agents/writer/restart',json={})
        assert response.status_code==(409 if busy else 200),response.text
        assert worker.stops==(0 if busy else 1)
        assert other.stops==0
        if not busy: assert response.json()['data']['execution']['state']=='running'
