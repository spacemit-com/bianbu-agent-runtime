import pytest

from bianbu_agent.models.context_profiles import discovered_profiles, cached_profile
from bianbu_agent.models import OpenAICompatibleModel
from bianbu_agent.engines.default import to_langchain_model
from bianbu_agent.context.config import CompressionSettingsProvider


def test_llama_serving_context_overrides_training_capacity():
    data = [{"id": "local-model", "owned_by": "llamacpp", "meta": {"n_ctx_train": 131072}}]
    assert discovered_profiles(data) == {}
    profiles = discovered_profiles(data, {"default_generation_settings": {"n_ctx": 8192}})
    assert profiles["local-model"]["max_input_tokens"] == 8192
    assert profiles["local-model"]["context_source"] == "provider.props"


def test_model_metadata_reaches_selected_langchain_model():
    selected = OpenAICompatibleModel(model="custom", base_url="http://localhost:8080/v1", profile={"max_input_tokens": 8192, "context_source": "provider.props"})
    model = to_langchain_model(selected)
    settings = CompressionSettingsProvider().read(model)
    assert settings.context_window_tokens == 8192
    assert settings.window_source == "provider.props"


def test_cached_metadata_is_url_scoped_and_expires():
    cache = {"base_url": "http://local/v1", "observed_at": 100, "models": {"small": {"max_input_tokens": 4096}}}
    assert cached_profile(cache, "http://local/v1", "small", now=110)["max_input_tokens"] == 4096
    assert cached_profile(cache, "http://other/v1", "small", now=110) == {}
    assert cached_profile(cache, "http://local/v1", "other", now=110) == {}
    assert cached_profile(cache, "http://local/v1", "small", now=100000) == {}


@pytest.mark.asyncio
async def test_discovery_caches_serving_limit_for_new_worker_and_replaces_stale_metadata(tmp_path):
    import httpx
    from tests.sdk.api.test_runs import _api
    from bianbu_agent.agents import AgentModel
    from bianbu_agent.models.file_resolver import YAMLModelResolver

    observed = []
    provide_limits = True
    def respond(request):
        observed.append(request.url.path)
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {"n_ctx": 4096}})
        item = {"id": "local"}
        if provide_limits:
            item.update(owned_by="llamacpp", meta={"n_ctx_train": 131072})
        return httpx.Response(200, json={"data": [item]})

    async with _api(tmp_path) as (client, runtime):
        runtime.provider_model_transport = httpx.MockTransport(respond)
        assert (await client.post("/api/v1/providers", json={
            "id": "discovery", "label": "Discovery", "base_url": "http://local/v1",
            "api_key": "private-secret", "parameters": {"protocol": "openai-compatible"},
        })).status_code == 201
        response = await client.get("/api/v1/providers/discovery/models")
        assert response.status_code == 200
        assert "private-secret" not in response.text
        resolver = YAMLModelResolver(runtime.home.models_dir)
        selected = resolver.resolve_agent_model(AgentModel(provider="discovery", name="local"))
        assert selected.profile["max_input_tokens"] == 4096
        assert observed == ["/v1/models", "/props"]
        # Resolution performs no HTTP request, including in a fresh resolver.
        assert len(observed) == 2
        provide_limits = False
        assert (await client.get("/api/v1/providers/discovery/models")).status_code == 200
        selected = resolver.resolve_agent_model(AgentModel(provider="discovery", name="local"))
        assert not selected.profile
        assert observed == ["/v1/models", "/props", "/v1/models"]


@pytest.mark.asyncio
async def test_llama_props_failure_does_not_invent_training_limit_or_fail_discovery(tmp_path):
    import httpx
    from tests.sdk.api.test_runs import _api
    from bianbu_agent.agents import AgentModel
    from bianbu_agent.models.file_resolver import YAMLModelResolver

    def respond(request):
        if request.url.path == "/props":
            raise httpx.ReadTimeout("props unavailable", request=request)
        return httpx.Response(200, json={"data": [{"id": "local", "owned_by": "llamacpp", "meta": {"n_ctx_train": 131072}}]})

    async with _api(tmp_path) as (client, runtime):
        runtime.provider_model_transport = httpx.MockTransport(respond)
        assert (await client.post("/api/v1/providers", json={
            "id": "local", "label": "Local", "base_url": "http://local/v1",
            "api_key": "unused", "parameters": {"protocol": "openai-compatible"},
        })).status_code == 201
        assert (await client.get("/api/v1/providers/local/models")).status_code == 200
        selected = YAMLModelResolver(runtime.home.models_dir).resolve_agent_model(AgentModel(provider="local", name="local"))
        assert not selected.profile
        assert CompressionSettingsProvider().read(to_langchain_model(selected)).window_source == "fallback_conservative"


def test_custom_endpoint_does_not_inherit_public_model_training_profile():
    selected = OpenAICompatibleModel(model="gpt-4o", provider_id="openai", base_url="http://localhost:8080/v1")
    settings = CompressionSettingsProvider().read(to_langchain_model(selected))
    assert settings.context_window_tokens == 8192
    assert settings.window_source == "fallback_conservative"


@pytest.mark.asyncio
async def test_discovered_capacity_publishes_generation_and_next_run_model(tmp_path):
    import asyncio
    from dataclasses import replace
    from tests.sdk.api.test_runs import _api
    from bianbu_agent import RunEvent
    from bianbu_agent.agents import AgentModel
    from bianbu_agent.models.providers import ProviderConfig

    started = asyncio.Event()
    release = asyncio.Event()
    built = []
    class Engine:
        def __init__(self, generation, model):
            self.generation = generation
            self.model = model
        async def stream(self, prompt, **kwargs):
            if len(built) == 1:
                started.set()
                await release.wait()
            yield RunEvent("token", {"text": str(self.model.profile["max_input_tokens"])})

    def build(agent, generation, model):
        engine = Engine(generation, model)
        built.append(engine)
        return engine

    async with _api(tmp_path) as (_client, runtime):
        runtime.runs._engine_builder = build
        await runtime.model_configs.create_provider(ProviderConfig(
            id="profiles", name="Profiles", protocol="openai-compatible",
            base_url="http://local/v1", api_key="unused",
        ))
        await runtime.agents.update(replace(await runtime.agents.get("default"), model=AgentModel(provider="profiles", name="local")))
        await runtime.model_configs.cache_provider_model_profiles("profiles", {"local": {"max_input_tokens": 4096}}, base_url="http://local/v1")
        revision = runtime.model_configs.capability_revision
        first_session = await runtime.sessions.create("first", agent_id="default")
        first = await first_session.run("first task")
        try:
            await asyncio.wait_for(started.wait(), timeout=3)
            old = built[0]
            await runtime.model_configs.cache_provider_model_profiles("profiles", {"local": {"max_input_tokens": 8192}}, base_url="http://local/v1")
            assert runtime.model_configs.capability_revision > revision
            assert runtime.plugin_runtime.current_generation.id > old.generation.id
            second_session = await runtime.sessions.create("second", agent_id="default")
            second = await second_session.run("next task")
            await second.wait()
            assert built[1].model.profile["max_input_tokens"] == 8192
            assert old.model.profile["max_input_tokens"] == 4096
        finally:
            release.set()
            await first.wait()


def test_already_built_model_uses_conservative_budget_after_cached_profile_expires(monkeypatch):
    from types import SimpleNamespace
    import bianbu_agent.context.config as settings_module
    cache = {"base_url": "http://local/v1", "observed_at": 100, "models": {"local": {"max_input_tokens": 32768, "context_source": "provider.models"}}}
    model = to_langchain_model(OpenAICompatibleModel(
        model="local", base_url="http://local/v1",
        profile=cached_profile(cache, "http://local/v1", "local", now=110),
    ))
    monkeypatch.setattr(settings_module, "time", SimpleNamespace(time=lambda: 110), raising=False)
    assert CompressionSettingsProvider().read(model).context_window_tokens == 32768
    monkeypatch.setattr(settings_module, "time", SimpleNamespace(time=lambda: 100000))
    settings = CompressionSettingsProvider().read(model)
    assert settings.context_window_tokens == 8192
    assert settings.window_source == "fallback_conservative"
