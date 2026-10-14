import json
from types import SimpleNamespace

import httpx
import pytest

from bianbu_agent.context.config import CompressionSettingsProvider
from bianbu_agent.engines.default import _ReasoningChatOpenAI


@pytest.mark.parametrize('window,declared,requested,expected', [
    (131072, None, None, 32768),
    (8192, None, 4096, 2048),
    (131072, 8192, 16384, 8192),
    (131072, 8192, 1024, 1024),
    (1001, None, None, 250),
])
def test_output_cap_and_compaction_share_capacity(window, declared, requested, expected):
    profile = {'context_window': window}
    if declared is not None:
        profile['max_output_tokens'] = declared
    settings = CompressionSettingsProvider().read(SimpleNamespace(profile=profile, max_tokens=requested))
    assert settings.output_reserve == expected
    assert settings.threshold_tokens == window - expected - settings.safety_reserve
    assert settings.target_tokens == settings.usable_input_tokens // 2


@pytest.mark.parametrize('request_cap,expected', [(None, 32768), (100000, 32768), (1000, 1000)])
def test_actual_http_request_enforces_output_cap(request_cap, expected):
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'test', 'object': 'chat.completion', 'created': 1, 'model': 'custom',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': 'ready'}}],
        })
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = _ReasoningChatOpenAI(model='custom', api_key='test',
            base_url='http://provider.test/v1', http_client=client,
            profile={'max_input_tokens': 131072})
        options = {} if request_cap is None else {'max_tokens': request_cap}
        assert model.invoke('hello', **options).content == 'ready'
    body = sent[0]
    assert body.get('max_completion_tokens', body.get('max_tokens')) == expected
    assert not ('max_tokens' in body and 'max_completion_tokens' in body)


def test_unknown_capacity_does_not_invent_a_serving_limit():
    model = _ReasoningChatOpenAI(model='custom', api_key='test', profile={})
    payload = model._get_request_payload('hello')
    assert not any(key in payload for key in ('max_tokens', 'max_completion_tokens'))
    assert model._get_request_payload('hello', max_tokens=1234).get('max_completion_tokens') == 1234

@pytest.mark.parametrize('name,window', [
    ('deepseek-flash', 1_000_000), ('deepseek-v4-flash', 1_000_000),
    ('MiniMax-M3', 1_000_000), ('MiniMax-M2.7', 204_800),
    ('kimi-k2.6', 256_000), ('glm-5', 200_000),
])
def test_builtin_context_fallback_works_for_gateway_aliases(name, window):
    model = _ReasoningChatOpenAI(model=name, api_key='test',
        base_url='http://gateway.test/v1', profile={})
    settings = CompressionSettingsProvider().read(model)
    assert settings.context_window_tokens == window
    assert settings.window_source == 'builtin.model_catalog'
    assert settings.output_reserve <= window // 4
    assert model._get_request_payload('hello')['max_completion_tokens'] == settings.output_reserve


def test_serving_context_overrides_builtin_and_expiry_returns_to_catalog(monkeypatch):
    import bianbu_agent.context.config as config
    model = _ReasoningChatOpenAI(model='deepseek-flash', api_key='test',
        profile={'max_input_tokens': 8192},
        metadata={'model_profile': {'max_input_tokens': 8192,
            'context_source': 'provider.models', 'context_expires_at': 200}})
    monkeypatch.setattr(config.time, 'time', lambda: 100)
    assert CompressionSettingsProvider().read(model).output_reserve == 2048
    monkeypatch.setattr(config.time, 'time', lambda: 300)
    assert CompressionSettingsProvider().read(model).context_window_tokens == 1_000_000


def test_mapping_does_not_guess_unknown_versions_or_quantized_names():
    for name in ('deepseek-future', 'my-deepseek-flash', 'MiniMax-M3-Q4_K_M'):
        model = SimpleNamespace(model=name, profile={})
        assert CompressionSettingsProvider().read(model).window_source == 'fallback_conservative'


def test_extra_body_cannot_bypass_output_cap():
    model = _ReasoningChatOpenAI(model='deepseek-flash', api_key='test', profile={})
    payload = model._get_request_payload('hello', extra_body={
        'max_tokens': 900000, 'thinking': {'type': 'enabled'},
    })
    assert payload['max_completion_tokens'] == 250000
    assert 'max_tokens' not in payload.get('extra_body', {})
    assert payload['extra_body']['thinking'] == {'type': 'enabled'}


def test_compaction_triggers_at_reserved_input_budget_and_keeps_current_task():
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import HumanMessage, AIMessage
    from bianbu_agent.context.middleware import ContextCompressionMiddleware, CompressionEventSink

    model = FakeMessagesListChatModel(responses=[], profile={'max_input_tokens': 10000})
    middleware = ContextCompressionMiddleware(model, CompressionSettingsProvider(), CompressionEventSink())
    prefix = [HumanMessage(content='old request', id='old')]
    current = HumanMessage(content='Keep my files and finish the app', id='current')
    # ~6900 input tokens fits within 7000; ~7100 requires compaction.
    assert middleware.before_model({'messages': [*prefix, AIMessage(content='x' * 27400), current]}, None) is None
    result = middleware.before_model({'messages': [*prefix, AIMessage(content='x' * 28400), current]}, None)
    assert result is not None
    assert any(message.id == 'current' for message in result['messages'])


@pytest.mark.parametrize('extra_cap,expected', [(10000, 2048), (1000, 1000)])
def test_sdk_native_chat_model_uses_same_wire_budget_in_agent_loop(extra_cap, expected):
    from langchain_openai import ChatOpenAI
    from bianbu_agent.engines.langgraph import build_langgraph_agent
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'test', 'object': 'chat.completion', 'created': 1, 'model': 'native',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': 'ready'}}],
        })
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = ChatOpenAI(model='native', api_key='test', http_client=client,
            profile={'max_input_tokens': 8192}, max_tokens=10000,
            extra_body={'max_completion_tokens': extra_cap, 'thinking': {'type': 'enabled'}})
        graph = build_langgraph_agent(model=model, system_prompt='Test')
        result = graph.invoke({'messages': [{'role': 'user', 'content': 'hello'}]})
        assert result['messages'][-1].content == 'ready'
    assert sent[0]['max_completion_tokens'] == expected
    assert sent[0]['thinking'] == {'type': 'enabled'}
    assert model.extra_body['max_completion_tokens'] == extra_cap
