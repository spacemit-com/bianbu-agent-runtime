from types import SimpleNamespace
import re
import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, SystemMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from bianbu_agent.security.security_middleware import build_security_middleware


def _masked_value(model_text: str, secret: str) -> str:
    """Return the natural stand-in the model saw for ``secret``.

    Stand-ins look like ordinary values, so the test reads the private
    session map instead of matching a visible marker.
    """
    from bianbu_agent.security.security_dlp import _REFERENCES, current_session_id

    mapping = _REFERENCES.mapping(current_session_id())
    for stand_in, original in mapping.items():
        if original == secret:
            assert stand_in in model_text
            return stand_in
    raise AssertionError(f'no stand-in for {secret!r} in {model_text!r}')



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
    mode = 'safe'
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
    mode = 'safe'
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
    mode = 'safe'
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
    if action == 'mask':
        # The replacement must look like an ordinary phone number; a visible
        # marker would tell the model that substitution happened.
        assert 'PII' not in joined
        assert re.search(r'1[3-9]\d{9}', joined)


def test_dlp_denial_uses_security_error_card():
    from bianbu_agent.security.security_errors import classify_run_exception
    from bianbu_agent.security.security_dlp import DLPDenied
    result=classify_run_exception(DLPDenied('数据被拦截',stage='post_model_call'))
    assert result['code']=='security_denied'
    assert result['details']['stage']=='post_model_call'


def _content_scan_client(secret: str):
    """Scan the whole text, so every block field is covered by one verdict."""

    scans = []

    def call(method, *args):
        if method == 'get_agent_security_mode':
            return {'mode': 'normal'}
        assert method == 'dlp_scan'
        agent_id, text = args
        scans.append((agent_id, text))
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret),
            pii_type='phone', action='mask')]}

    return call, scans


@pytest.mark.asyncio
async def test_non_text_reasoning_blocks_are_scanned(monkeypatch):
    from bianbu_agent.security import security_runtime

    secret = '13824681357'
    call, scans = _content_scan_client(secret)
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', call)
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=[
            {'type': 'text', 'text': secret},
            {'type': 'reasoning', 'reasoning': f'about {secret}'},
        ])],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def handler(prepared):
        seen['messages'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await build_security_middleware().awrap_model_call(request, handler)

    assert secret not in seen['messages']
    assert 'PII' not in seen['messages']
    assert re.search(r'1[3-9]\d{9}', seen['messages'])
    assert any(secret in text for _aid, text in scans)


@pytest.mark.asyncio
async def test_media_blocks_are_refused_instead_of_forwarded_unscanned(monkeypatch):
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import DLPDenied

    call, _scans = _content_scan_client('unused')
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', call)
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=[{
            'type': 'image_url',
            'image_url': {'url': 'data:image/png;base64,AAAA'},
        }])],
        runtime=SimpleNamespace(),
    )

    async def handler(_prepared):
        pytest.fail('an unscannable image block must not reach the model')

    with pytest.raises(DLPDenied, match='媒体内容'):
        await build_security_middleware().awrap_model_call(request, handler)


@pytest.mark.asyncio
async def test_opaque_payload_in_an_unknown_block_fails_closed(monkeypatch):
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import DLPDenied

    call, _scans = _content_scan_client('unused')
    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', call)
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=[{
            'type': 'provider_payload',
            'blob': 'data:application/octet-stream;base64,AAAA',
        }])],
        runtime=SimpleNamespace(),
    )

    async def handler(_prepared):
        pytest.fail('opaque bytes must not reach the model')

    with pytest.raises(DLPDenied, match='二进制'):
        await build_security_middleware().awrap_model_call(request, handler)


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


@pytest.mark.asyncio
async def test_tool_arguments_are_restored_after_model_saw_only_handles(monkeypatch):
    """The model sees a handle; the local tool receives the original value."""
    import re
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import clear_references

    secret = '13824681357'
    clear_references()

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        assert method == 'dlp_scan'
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    middleware = build_security_middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=secret)],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def model_handler(prepared):
        seen['model'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await middleware.awrap_model_call(request, model_handler)
    assert secret not in seen['model']
    ref = _masked_value(seen['model'], secret)
    assert 'PII-' not in seen['model']

    tool_request = ToolCallRequest(
        tool_call={'name': 'echo', 'id': 'call-1', 'args': {'value': ref}},
        tool=None, state={}, runtime=None,
    )

    async def tool_handler(prepared):
        seen['tool_arg'] = prepared.tool_call['args']['value']
        return ToolMessage(content=seen['tool_arg'], tool_call_id='call-1', name='echo')

    try:
        await middleware.awrap_tool_call(tool_request, tool_handler)
        assert seen['tool_arg'] == secret
    finally:
        clear_references()


def test_unknown_dlp_reference_fails_closed():
    from bianbu_agent.security.security_dlp import DLPDenied, clear_references, resolve_references

    clear_references()
    with pytest.raises(DLPDenied):
        resolve_references('[PII-phone-R0123456789ab]', session_id='missing')


def test_natural_stand_in_is_format_valid_and_marker_free():
    import re
    from bianbu_agent.security.security_dlp import natural_stand_in

    phone = natural_stand_in('phone', '13824681357')
    assert re.fullmatch(r'1[3-9]\d{9}', phone)
    assert 'PII' not in phone

    email = natural_stand_in('email', 'alice@example.com')
    assert re.fullmatch(r'[^@\s]+@[^@\s]+\.[A-Za-z]{2,}', email)

    card = natural_stand_in('bank_card', '4539578763621486')
    assert len(card) == 16 and card.isdigit()


def test_same_value_gets_one_stand_in_and_round_trips(monkeypatch):
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import (
        clear_references,
        resolve_references,
        scan_text,
    )
    from bianbu_agent.security.security_dlp import current_session_id

    secret = '13824681357'

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    clear_references()
    masked = scan_text(f'联系 {secret}', agent_id='com.test')
    assert secret not in masked
    assert 'PII' not in masked
    assert re.search(r'1[3-9]\d{9}', masked)
    restored, count = resolve_references(masked, session_id=current_session_id())
    assert count == 1
    assert restored == f'联系 {secret}'


@pytest.mark.asyncio
async def test_invalid_restored_arguments_never_echo_plaintext(monkeypatch):
    """A validation failure must not hand the restored value back to the model."""
    import re
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import clear_references

    secret = 'secret-plaintext-42'
    clear_references()

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        assert method == 'dlp_scan'
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    middleware = build_security_middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=secret)],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def model_handler(prepared):
        seen['model'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await middleware.awrap_model_call(request, model_handler)
    ref = _masked_value(seen['model'], secret)
    assert 'PII-' not in seen['model']

    async def lookup(count: int) -> str:
        return str(count)

    tool = StructuredTool.from_function(
        coroutine=lookup, name='lookup', description='lookup',
    )
    tool_request = ToolCallRequest(
        tool_call={'name': 'lookup', 'id': 'call-1', 'args': {'count': ref}},
        tool=tool, state={}, runtime=None,
    )

    node = ToolNode([tool])

    async def tool_handler(prepared):
        return await node._execute_tool_async(prepared, 'tool_calls', {})

    try:
        result = await middleware.awrap_tool_call(tool_request, tool_handler)
        assert result.status == 'error'
        assert secret not in str(result.content)
        assert result.tool_call_id == 'call-1'
    finally:
        clear_references()


@pytest.mark.asyncio
async def test_invalid_call_without_dlp_handles_keeps_langgraph_error(monkeypatch):
    """Calls that never touched DLP keep LangGraph's own error reporting."""
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    from bianbu_agent.security import security_runtime

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(
        security_runtime, 'security_client_call',
        lambda method, *args: {'mode': 'safe'} if method == 'get_agent_security_mode' else {'findings': []},
    )
    middleware = build_security_middleware()

    async def lookup(count: int) -> str:
        return str(count)

    tool = StructuredTool.from_function(
        coroutine=lookup, name='lookup', description='lookup',
    )
    tool_request = ToolCallRequest(
        tool_call={'name': 'lookup', 'id': 'call-2', 'args': {'count': 'not-a-number'}},
        tool=tool, state={}, runtime=None,
    )

    node = ToolNode([tool])

    async def tool_handler(prepared):
        return await node._execute_tool_async(prepared, 'tool_calls', {})

    result = await middleware.awrap_tool_call(tool_request, tool_handler)
    assert result.status == 'error'
    assert 'not-a-number' in str(result.content)


@pytest.mark.asyncio
async def test_embedded_handle_value_is_scrubbed_from_errors(monkeypatch):
    """A handle inside a longer argument must not leak through its prefix."""
    import re
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import clear_references

    secret = 'secret-plaintext-42'
    clear_references()

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        assert method == 'dlp_scan'
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    middleware = build_security_middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=secret)],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def model_handler(prepared):
        seen['model'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await middleware.awrap_model_call(request, model_handler)
    ref = _masked_value(seen['model'], secret)
    assert 'PII-' not in seen['model']

    async def lookup(count: int) -> str:
        return str(count)

    tool = StructuredTool.from_function(
        coroutine=lookup, name='lookup', description='lookup',
    )
    tool_request = ToolCallRequest(
        tool_call={
            'name': 'lookup', 'id': 'call-3',
            'args': {'count': 'x' + ref + 'y'},
        },
        tool=tool, state={}, runtime=None,
    )
    node = ToolNode([tool])

    async def tool_handler(prepared):
        return await node._execute_tool_async(prepared, 'tool_calls', {})

    try:
        result = await middleware.awrap_tool_call(tool_request, tool_handler)
        assert result.status == 'error'
        assert secret not in str(result.content)
        assert 'PII-' not in str(result.content)
    finally:
        clear_references()


@pytest.mark.asyncio
async def test_escaped_restored_value_is_not_echoed(monkeypatch):
    """repr() escapes newlines and quotes, so matching text is not enough."""
    import re
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import clear_references

    secret = 'home address\n12 "back" \\ lane'
    clear_references()

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        assert method == 'dlp_scan'
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='address', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    middleware = build_security_middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=secret)],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def model_handler(prepared):
        seen['model'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await middleware.awrap_model_call(request, model_handler)
    ref = _masked_value(seen['model'], secret)
    assert 'PII-' not in seen['model']

    async def lookup(count: int) -> str:
        return str(count)

    tool = StructuredTool.from_function(
        coroutine=lookup, name='lookup', description='lookup',
    )
    tool_request = ToolCallRequest(
        tool_call={'name': 'lookup', 'id': 'call-4', 'args': {'count': ref}},
        tool=tool, state={}, runtime=None,
    )
    node = ToolNode([tool])

    async def tool_handler(prepared):
        return await node._execute_tool_async(prepared, 'tool_calls', {})

    try:
        result = await middleware.awrap_tool_call(tool_request, tool_handler)
        text = str(result.content)
        assert result.status == 'error'
        assert 'home address' not in text
        assert 'back' not in text
        assert 'with kwargs' not in text
        assert 'count' in text
    finally:
        clear_references()


@pytest.mark.asyncio
async def test_repr_wrapped_error_is_also_scrubbed(monkeypatch):
    """handle_tool_errors=True wraps the whole exception with repr()."""
    import re
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import clear_references

    secret = 'secret-plaintext-42'
    clear_references()

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        assert method == 'dlp_scan'
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    middleware = build_security_middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=secret)],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def model_handler(prepared):
        seen['model'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await middleware.awrap_model_call(request, model_handler)
    ref = _masked_value(seen['model'], secret)
    assert 'PII-' not in seen['model']

    async def lookup(count: int) -> str:
        return str(count)

    tool = StructuredTool.from_function(
        coroutine=lookup, name='lookup', description='lookup',
    )
    tool_request = ToolCallRequest(
        tool_call={'name': 'lookup', 'id': 'call-5', 'args': {'count': ref}},
        tool=tool, state={}, runtime=None,
    )
    node = ToolNode([tool], handle_tool_errors=True)

    async def tool_handler(prepared):
        return await node._execute_tool_async(prepared, 'tool_calls', {})

    try:
        result = await middleware.awrap_tool_call(tool_request, tool_handler)
        text = str(result.content)
        assert result.status == 'error'
        assert secret not in text
        assert 'kwargs' not in text
        assert 'count' in text
    finally:
        clear_references()


@pytest.mark.asyncio
async def test_unwrapped_tool_invocation_error_is_answered_without_values(monkeypatch):
    """ToolNode raises ToolInvocationError when no handler wraps it."""
    import re
    from langchain_core.tools import StructuredTool
    from langgraph.prebuilt.tool_node import ToolCallRequest, ToolNode
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import clear_references

    secret = 'secret-plaintext-42'
    clear_references()

    def scan(method, _agent_id, text):
        if method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        assert method == 'dlp_scan'
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    middleware = build_security_middleware()
    request = ModelRequest(
        model=FakeListChatModel(responses=['ok']),
        messages=[HumanMessage(content=secret)],
        runtime=SimpleNamespace(),
    )
    seen = {}

    async def model_handler(prepared):
        seen['model'] = str(prepared.messages)
        return ModelResponse(result=[AIMessage(content='ok')])

    await middleware.awrap_model_call(request, model_handler)
    ref = _masked_value(seen['model'], secret)
    assert 'PII-' not in seen['model']

    async def lookup(count: int) -> str:
        return str(count)

    tool = StructuredTool.from_function(
        coroutine=lookup, name='lookup', description='lookup',
    )
    tool_request = ToolCallRequest(
        tool_call={'name': 'lookup', 'id': 'call-6', 'args': {'count': ref}},
        tool=tool, state={}, runtime=None,
    )
    node = ToolNode([tool])

    async def tool_handler(prepared):
        return await node._execute_tool_async(prepared, 'tool_calls', {})

    try:
        result = await middleware.awrap_tool_call(tool_request, tool_handler)
        text = str(result.content)
        assert result.status == 'error'
        assert secret not in text
        assert 'kwargs' not in text
        assert 'count' in text
    finally:
        clear_references()
def test_dlp_is_bypassed_without_security_plugin_agent(monkeypatch):
    monkeypatch.setenv('BIANBU_SECURITY_AGENT_ENABLED', '0')
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import dlp_checks_disabled

    def forbidden(*_args, **_kwargs):
        raise AssertionError("non-security Agent must not call securityd")

    monkeypatch.setattr(security_runtime, 'security_client_call', forbidden)

    assert dlp_checks_disabled(stage='pre_model_call') is True


def test_already_masked_request_is_not_masked_again(monkeypatch):
    """A prompt can cross the model boundary after securityd already masked it.

    Replacing the stand-in a second time would hand the model a different value
    from the one the security Hook reported, so the Session could no longer
    restore the answer.
    """
    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import (
        clear_references,
        scan_text,
    )

    secret = '13824681357'

    def scan(_method, _agent_id, text):
        if _method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        start = text.find(secret)
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + len(secret),
            pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', scan)
    clear_references()
    try:
        once = scan_text(f'联系 {secret}', agent_id='com.test')

        def mask_stand_in(_method, _agent_id, text):
            if _method == 'get_agent_security_mode':
                return {'mode': 'safe'}
            start = text.find(once.rsplit(' ', 1)[-1])
            return {'findings': [] if start < 0 else [dict(
                start=start, end=len(text),
                pii_type='phone', action='mask')]}

        monkeypatch.setattr(
            security_runtime, 'security_client_call', mask_stand_in)
        twice = scan_text(once, agent_id='com.test')
    finally:
        clear_references()

    assert twice == once


def test_middleware_adopts_the_hook_session_map(monkeypatch):
    """The model filter must know the stand-ins the Hook already assigned."""
    from types import SimpleNamespace

    import langgraph.config

    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import (
        _adopt_hook_stand_ins,
        clear_references,
        mask_text_with_references,
    )

    monkeypatch.setattr(
        langgraph.config,
        "get_config",
        lambda: {"configurable": {
            "session_id": "hook-session",
            "run_control": SimpleNamespace(
                dlp_stand_ins={"13879455773": "13812345678"},
            ),
        }},
    )

    def mask_stand_in(_method, _agent_id, text):
        if _method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        return {'findings': [dict(
            start=0, end=len(text), pii_type='phone', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(security_runtime, 'security_client_call', mask_stand_in)
    clear_references()
    try:
        _adopt_hook_stand_ins("hook-session")
        masked = mask_text_with_references(
            "13879455773",
            [dict(start=0, end=11, pii_type="phone")],
            session_id="hook-session",
        )
    finally:
        clear_references()

    assert masked == "13879455773"


def test_real_value_reuses_the_adopted_hook_stand_in(monkeypatch):
    """A later turn mentioning the real value must keep the model's stand-in.

    ``merge`` only carries ``ref -> value``; without a value lookup the next
    scan falls back to the deterministic generator, sees that stand-in as
    taken, and collision-retries into a *third* value.
    """
    from types import SimpleNamespace

    import langgraph.config

    from bianbu_agent.security import security_runtime
    from bianbu_agent.security.security_dlp import (
        _adopt_hook_stand_ins,
        clear_references,
        scan_text,
    )

    control = SimpleNamespace(
        session_id="product-session",
        dlp_stand_ins={"丁敏秀": "林嘉禾"},
    )
    monkeypatch.setattr(
        langgraph.config,
        "get_config",
        lambda: {"configurable": {
            "session_id": "thread-1",
            "thread_id": "thread-1",
            "run_control": control,
        }},
    )

    def scan_real_value(_method, _agent_id, text):
        if _method == 'get_agent_security_mode':
            return {'mode': 'safe'}
        start = text.find("林嘉禾")
        return {'findings': [] if start < 0 else [dict(
            start=start, end=start + 3, pii_type='name', action='mask')]}

    monkeypatch.setenv('BIANBU_SECURITY', '1')
    monkeypatch.setattr(
        security_runtime, 'security_client_call', scan_real_value)
    clear_references()
    try:
        # The Hook already reported this Session's stand-in for 林嘉禾.
        _adopt_hook_stand_ins("thread-1")
        masked = scan_text("我的名字是林嘉禾", agent_id='com.test')
    finally:
        clear_references()

    assert masked == "我的名字是丁敏秀"


def test_session_scope_is_the_product_session_not_the_rotated_thread(monkeypatch):
    """Clearing context rotates the thread; the DLP scope must not rotate."""
    import langgraph.config

    from bianbu_agent.security.security_dlp import current_session_id

    monkeypatch.setattr(
        langgraph.config,
        "get_config",
        lambda: {"configurable": {
            "thread_id": "thread-after-clear",
            "product_session_id": "product-session",
            "session_id": "product-session",
            "run_control": SimpleNamespace(session_id="product-session"),
        }},
    )

    assert current_session_id() == "product-session"
