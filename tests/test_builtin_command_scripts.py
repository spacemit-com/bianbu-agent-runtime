"""Exercise built-in Hook programs through their public JSON boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1] / 'resources/plugins'


def invoke(plugin, event, tmp_path, **fields):
    script = ROOT / plugin / 'scripts' / f'{event}.py'
    assert script.is_file(), f'{plugin} must ship an external {event} program'
    payload = dict(schema_version=1, hook_event_name='BeforeRun', run_id='run', session_id='session', agent_id='agent', account_id='account', request='', prompt='', response='', status='', metadata={})
    payload.update(fields)
    result = subprocess.run([sys.executable, str(script)], input=json.dumps(payload), text=True, capture_output=True, env={**os.environ, 'PLUGIN_ROOT': str(ROOT / plugin), 'PLUGIN_DATA_ROOT': str(tmp_path), 'PLUGIN_WORKSPACE_ROOT': str(tmp_path), 'BIANBU_AGENT_MEMORY_ENABLED': 'false'}, timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_memory_scripts_record_then_recall(tmp_path):
    assert invoke('bianbu.memory', 'after_run', tmp_path, metadata={'memory_to_record': 'Prefer Chinese'}) == {}
    result = invoke('bianbu.memory', 'before_run', tmp_path, request='hello')
    assert 'Prefer Chinese' in '\n'.join(result['context_sections'])


def test_intent_terminal_context_stays_on_cloud(tmp_path):
    result = invoke('bianbu.bibit.intent', 'before_run', tmp_path, request='终端里')
    assert result['metadata']['route'] == 'cloud'
    assert result['context_sections']


def test_intent_before_tool_guards_ambiguous_adjustment(tmp_path):
    ambiguous = invoke(
        'bianbu.bibit.intent', 'before_tool', tmp_path,
        tool_name='bianbu.bibit.intent/bibit-intent/set_volume',
        request='调到1',
    )
    assert ambiguous['decision'] == 'deny'
    assert '音量' in ambiguous['reason']

    brightness = invoke(
        'bianbu.bibit.intent', 'before_tool', tmp_path,
        tool_name='bianbu.bibit.intent/bibit-intent/set_brightness',
        request='调到1',
    )
    assert brightness['decision'] == 'deny'

    allowed = invoke(
        'bianbu.bibit.intent', 'before_tool', tmp_path,
        tool_name='bianbu.bibit.intent/bibit-intent/set_volume',
        request='把音量调到1',
    )
    assert allowed == {}


def test_intent_before_tool_reads_the_user_prompt_not_the_tool_id(tmp_path):
    """K3 真实形态：request 是工具限定 ID（自带 "volume"），别拿它当用户请求。

    只看 request 时正则总会命中工具名里的 "volume"，守卫等于永远放行，
    「调到1」就这么被送进 set_volume。
    """

    tool = 'bianbu.bibit.intent/bibit-intent/set_volume'
    denied = invoke(
        'bianbu.bibit.intent', 'before_tool', tmp_path,
        tool_name=tool, request=tool, prompt='调到1',
    )
    assert denied['decision'] == 'deny'
    assert '音量' in denied['reason']

    allowed = invoke(
        'bianbu.bibit.intent', 'before_tool', tmp_path,
        tool_name=tool, request=tool, prompt='把音量调到1',
    )
    assert allowed == {}


def test_audit_script_persists_identity(tmp_path):
    invoke('bianbu.audit', 'after_run', tmp_path, status='completed')
    entry = json.loads((tmp_path / 'audit.jsonl').read_text())
    assert entry == {'kind': 'run.completed', 'run_id': 'run', 'session_id': 'session', 'agent_id': 'agent'}


def test_security_denial_is_structured_json(tmp_path):
    result = invoke('bianbu.security', 'before_tool', tmp_path, request='execute', metadata={'security_denied': True})
    assert result['decision'] == 'deny'
    assert result['error']['tool'] == 'execute'
    assert result['error']['capability'] == '<preset>'
    assert result['error']['terminate_run'] is True


def test_memory_watermark_completes_before_process_exit(tmp_path):
    sdk = tmp_path / 'bianbu_agent_sdk'
    sdk.mkdir()
    (sdk / '__init__.py').write_text('')
    (sdk / 'memory.py').write_text('''import asyncio, os
from pathlib import Path
class Bridge:
    async def record_run_started(self, **kwargs):
        await asyncio.sleep(0.05)
        Path(os.environ['PLUGIN_DATA_ROOT'], 'watermark').write_text(kwargs['run_id'])
async def get_memory_bridge(**kwargs):
    return Bridge()
''')
    script = ROOT / 'bianbu.memory/scripts/before_run.py'
    request = dict(schema_version=1, run_id='live-run', session_id='s', agent_id='a', account_id='account', request='历史会话', metadata={})
    result = subprocess.run([sys.executable, str(script)], input=json.dumps(request), text=True, capture_output=True, env={**os.environ, 'PYTHONPATH': str(tmp_path), 'PLUGIN_DATA_ROOT': str(tmp_path), 'BIANBU_AGENT_MEMORY_ENABLED': 'true'}, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['metadata']['session_history_request'] is True
    assert (tmp_path / 'watermark').read_text() == 'live-run'


def test_intent_sigterm_cancels_edge_run_and_hands_back_to_runtime(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import signal
    import threading

    started, stopped, release = threading.Event(), threading.Event(), threading.Event()
    submitted = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            submitted.append(json.loads(self.rfile.read(int(self.headers['Content-Length'])))['run_id'])
            started.set()
            release.wait(5)

        def do_DELETE(self):
            assert self.path == '/v1/runs/' + submitted[0]
            stopped.set()
            self.send_response(204)
            self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = ROOT / 'bianbu.bibit.intent/scripts/before_run.py'
    process = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, 'BIBIT_INTENT_BASE_URL': f'http://127.0.0.1:{server.server_port}'})
    try:
        process.stdin.write(json.dumps(dict(schema_version=1, request='启动设备操作', session_id='s', metadata={})))
        process.stdin.close()
        process.stdin = None
        assert started.wait(4)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert stopped.is_set()
        result = json.loads(stdout)
        assert 'direct_reply' not in result
        assert result['metadata']['route'] == 'cloud'
        assert result['metadata']['reason'] == 'edge_error'
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
            process.wait()
        server.shutdown()
        server.server_close()
        thread.join()


def test_security_private_state_survives_degraded_nonterminal_hook(tmp_path):
    state = {'pii_map': {'[PII-NAME-001]': 'private name'}}
    result = invoke('bianbu.security', 'on_error', tmp_path, status='running', state=state)
    assert result['state'] == state
    assert 'private name' not in json.dumps(result.get('metadata', {}))


def test_security_terminal_hook_clears_private_state(tmp_path):
    for event, status in [('after_run', 'completed'), ('on_error', 'failed')]:
        result = invoke('bianbu.security', event, tmp_path, status=status, state={'pii_map': {'placeholder': 'private'}})
        assert result['state'] == {}
