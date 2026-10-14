from types import SimpleNamespace
import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from bianbu_agent.security.security_middleware import build_security_middleware


@pytest.mark.asyncio
async def test_yolo_bypasses_model_scanners_and_restores_normal_checks(monkeypatch):
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import DLPDenied, scan_text
    mode = 'yolo'
    audits = []
    scans = []
    def call(method, *args):
        if method == 'get_agent_security_mode':
            return {'mode': mode}
        if method == 'audit_log':
            audits.append(args)
            return 1
        assert method == 'dlp_scan'
        scans.append(args)
        raise RuntimeError('DLP scanner unavailable')
    monkeypatch.setattr(security_runtime, 'security_client_call', call)
    request = ModelRequest(model=FakeListChatModel(responses=['ok']),
                           messages=[HumanMessage(content='13824681357')], runtime=SimpleNamespace())
    async def handler(req):
        assert req is request
        return ModelResponse(result=[AIMessage(content='13824681357')])
    middleware = build_security_middleware()
    result = await middleware.awrap_model_call(request, handler)
    assert result.result[0].content == '13824681357'
    assert scan_text('summary containing 13824681357') == 'summary containing 13824681357'
    assert not scans
    assert audits
    mode = 'normal'
    with pytest.raises(DLPDenied):
        await middleware.awrap_model_call(request, handler)
    assert scans


def test_yolo_skill_text_bypasses_scanner_initialization_and_cache(monkeypatch):
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    from bianbu_agent.security import security_runtime, security_skill_dlp
    monkeypatch.setattr(security_runtime, 'security_client_call',
                        lambda method, *args: {'mode': 'yolo'} if method == 'get_agent_security_mode' else 1)
    def initialize():
        pytest.fail('YOLO must not initialize a skill scanner')
    monkeypatch.setattr(security_skill_dlp, '_ensure_engine', initialize)
    assert security_skill_dlp.scan_skill_text('agent.demo', 'guide', '13824681357') == '13824681357'


def test_yolo_sync_model_and_audit_outage_do_not_block(monkeypatch):
    from bianbu_agent.security import security_runtime
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    def call(method, *args):
        if method == 'get_agent_security_mode':
            return {'mode': 'yolo'}
        if method == 'audit_log':
            raise RuntimeError('audit unavailable')
        pytest.fail('YOLO must not invoke the scanner')
    monkeypatch.setattr(security_runtime, 'security_client_call', call)
    request = ModelRequest(model=FakeListChatModel(responses=['ok']),
                           messages=[HumanMessage(content='hello')], runtime=SimpleNamespace())
    def handler(prepared):
        assert prepared is request
        return ModelResponse(result=[AIMessage(content='unaltered')])
    result = build_security_middleware().wrap_model_call(request, handler)
    assert result.result[0].content == 'unaltered'


def test_skill_mode_roundtrip_preserves_normal_mask_and_does_not_cache_yolo(monkeypatch):
    from bianbu_agent.security import security_runtime, security_skill_dlp
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    mode = 'normal'
    scans = []
    monkeypatch.setattr(security_runtime, 'security_client_call',
                        lambda method, *args: {'mode': mode} if method == 'get_agent_security_mode' else 1)
    def scan(aid, text):
        scans.append(text)
        return {'findings': [{'start': 0, 'end': 11, 'pii_type': 'phone', 'action': 'mask'}]}
    client = SimpleNamespace(dlp_scan=scan)
    monkeypatch.setattr(security_skill_dlp, '_ensure_engine', lambda: (client, None))
    monkeypatch.setattr(security_skill_dlp, '_CACHE', {})
    def read():
        return security_skill_dlp.scan_skill_text('agent.demo', 'guide', '13824681357')
    assert '13824681357' not in read()
    mode = 'yolo'
    assert read() == '13824681357'
    mode = 'normal'
    assert '13824681357' not in read()
    assert len(scans) == 1


@pytest.mark.asyncio
async def test_every_outgoing_message_is_scanned_without_changing_pairing(monkeypatch):
    from bianbu_agent.security import security_runtime
    secret = '13824681357'
    calls = []
    def scan(method, aid, text):
        assert method == 'dlp_scan'
        calls.append(text)
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(start=start,end=start+len(secret),pii_type='phone',action='mask')]}
    monkeypatch.setenv('BIANBU_SECURITY','1')
    monkeypatch.setattr(security_runtime,'security_client_call',scan)
    history=[HumanMessage(content=secret,id='u'),
             AIMessage(content='',id='a',tool_calls=[dict(id='call',name='echo',args={'value':secret})]),
             ToolMessage(content=secret,id='t',tool_call_id='call'),
             HumanMessage(content=[{'type':'text','text':secret}],id='n')]
    request=ModelRequest(model=FakeListChatModel(responses=['ok']), messages=history,
                         system_message=SystemMessage(content=secret),runtime=SimpleNamespace())
    async def handler(req):
        assert secret not in str(req.messages)
        assert secret not in req.system_message.content
        assert [m.id for m in req.messages]==['u','a','t','n']
        assert req.messages[1].tool_calls[0]['id']==req.messages[2].tool_call_id=='call'
        return ModelResponse(result=[AIMessage(content='ok')])
    await build_security_middleware().awrap_model_call(request,handler)
    assert calls
    assert history[0].content==secret


@pytest.mark.asyncio
async def test_policy_change_rechecks_history_and_block_never_calls_model(monkeypatch):
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import DLPDenied
    action = 'allow'
    def scan(*args):
        return {'findings':[dict(start=0,end=11,pii_type='phone',action=action)]}
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    request = ModelRequest(model=FakeListChatModel(responses=['ok']),
                           messages=[HumanMessage(content='13824681357')],runtime=SimpleNamespace())
    invoked=[]
    async def handler(req):
        invoked.append(req.messages[0].content)
        return ModelResponse(result=[AIMessage(content='ok')])
    middleware=build_security_middleware()
    await middleware.awrap_model_call(request,handler)
    action='block'
    with pytest.raises(DLPDenied):
        await middleware.awrap_model_call(request,handler)
    assert invoked==['13824681357']


def test_scanner_outage_fails_closed(monkeypatch):
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import scan_text, DLPDenied
    def unavailable(*args): raise RuntimeError('unavailable')
    monkeypatch.setattr(security_runtime,'security_client_call',unavailable)
    with pytest.raises(DLPDenied): scan_text('history')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unavailable', 'malformed'])
async def test_scan_failure_reports_service_failure_and_never_calls_model(monkeypatch, failure):
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import DLPDenied
    from bianbu_agent.security.security_errors import classify_run_exception

    cause = RuntimeError('scanner unavailable')
    def scan(*args):
        if failure == 'unavailable':
            raise cause
        return {'findings': None}

    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    request = ModelRequest(model=FakeListChatModel(responses=['ok']),
                           messages=[HumanMessage(content='hello')], runtime=SimpleNamespace())
    async def handler(req):
        pytest.fail('unscanned data must not reach the model')

    with pytest.raises(DLPDenied) as caught:
        await build_security_middleware().awrap_model_call(request, handler)
    payload = classify_run_exception(caught.value)
    assert payload['code'] == 'security_unavailable'
    assert payload['details']['degraded'] is True
    assert payload['details']['terminate_run'] is True
    assert payload['details']['policy_type'] == 'dlp'
    assert caught.value.__cause__ is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['mask','block'])
async def test_streamed_model_output_is_governed_before_publication(monkeypatch, action):
    from langchain.agents import create_agent
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import DLPDenied
    secret='13824681357'
    def scan(method, aid, text):
        start=text.find(secret)
        return {'findings':[] if start<0 else [dict(start=start,end=start+11,pii_type='phone',action=action)]}
    monkeypatch.setattr(security_runtime,'security_client_call',scan)
    graph=create_agent(model=FakeListChatModel(responses=[secret]),middleware=[build_security_middleware()])
    messages=[]
    try:
        async for message, _ in graph.astream({'messages':[HumanMessage(content='hello')]},stream_mode='messages'):
            messages.append(message.content)
    except DLPDenied:
        assert action=='block'
    else:
        assert action=='mask'
    joined=''.join(messages)
    assert secret not in joined
    assert ('PII' in joined) if action=='mask' else not joined


def test_dlp_denial_uses_security_error_card():
    from bianbu_agent.security.security_errors import classify_run_exception
    from bianbu_agent.security.security_dlp import DLPDenied
    result=classify_run_exception(DLPDenied('数据被拦截',stage='post_model_call'))
    assert result['code']=='security_denied'
    assert result['details']['stage']=='post_model_call'


@pytest.mark.asyncio
async def test_output_buffer_preserves_stateful_model_across_rounds(monkeypatch):
    from langchain.agents import create_agent
    from bianbu_agent.security import security_runtime
    monkeypatch.setattr(security_runtime,'security_client_call',lambda *args:{'findings':[]})
    model=FakeListChatModel(responses=['first','second','third'])
    graph=create_agent(model=model,middleware=[build_security_middleware()])
    results=[await graph.ainvoke({'messages':[HumanMessage(content='hello')]}) for _ in range(2)]
    assert [r['messages'][-1].content for r in results]==['first','second']
    assert model.i==2
