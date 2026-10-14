import os
import socket
import subprocess
import sys
from pathlib import Path


def test_preload_source_intercepts_connect_and_emits_json():
    source=(Path(__file__).parents[2]/"scripts/network-learning-preload.c").read_text()
    assert 'int connect(' in source and '\\"op\\":\\"event\\"' in source
    assert 'remote_ip' in source and 'remote_port' in source


def test_missing_collector_socket_does_not_change_connect_errno(tmp_path):
    source = Path(__file__).parents[2] / "scripts/network-learning-preload.c"
    library = tmp_path / "libnetwork-learning.so"
    subprocess.run(
        ["gcc", "-shared", "-fPIC", "-O2", str(source), "-o", str(library), "-ldl"],
        check=True,
    )
    code = (
        "import socket; s=socket.socket(); s.setblocking(False); "
        "print(s.connect_ex(('198.51.100.1', 443)))"
    )
    baseline = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    env = os.environ.copy()
    env.update({
        "LD_PRELOAD": str(library),
        "BIANBU_NETWORK_LEARNING_SOCKET": str(tmp_path / "missing" / "collector.sock"),
        "BIANBU_NETWORK_LEARNING_INSTANCE_ID": "test-instance",
    })
    hooked = subprocess.check_output(
        [sys.executable, "-c", code], env=env, text=True,
    ).strip()

    assert hooked == baseline


def test_real_connections_and_dns_report_final_results(tmp_path):
    import json
    import threading
    import errno
    source = Path(__file__).parents[2] / 'scripts/network-learning-preload.c'
    library = tmp_path / 'learning.so'
    subprocess.run(['gcc', '-shared', '-fPIC', '-O2', str(source), '-o', str(library), '-ldl', '-pthread'], check=True)
    collector = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path = str(tmp_path / 'collector.sock'); collector.bind(path); collector.listen(32)
    collector.settimeout(.1)
    listener = socket.socket(); listener.bind(('127.0.0.1', 0)); listener.listen(4)
    closed = socket.socket(); closed.bind(('127.0.0.1', 0)); closed_port = closed.getsockname()[1]
    events = []; stopping = threading.Event()
    def receive():
        while not stopping.is_set():
            try: c, _ = collector.accept()
            except socket.timeout: continue
            with c, c.makefile('r') as stream:
                events.append(json.loads(stream.readline()))
    thread = threading.Thread(target=receive); thread.start()
    env = dict(os.environ, LD_PRELOAD=str(library), BIANBU_NETWORK_LEARNING_SOCKET=path,
               BIANBU_NETWORK_LEARNING_INSTANCE_ID='test-instance')
    code = '''
import socket,sys,os,select
for timeout in (None, 1):
 for port in map(int,sys.argv[1:]):
  with socket.socket() as s:
   s.settimeout(timeout)
   try: s.connect(('127.0.0.1',port))
   except OSError: pass
socket.getaddrinfo('localhost',80,type=socket.SOCK_STREAM)
# Replacing a pending fd must not attach the replacement's success to the old target.
with socket.socket() as pending, socket.socket() as replacement:
 pending.setblocking(False)
 pending.connect_ex(('127.0.0.1',int(sys.argv[2])))
 select.select([], [pending], [], 1)
 replacement.connect(('127.0.0.1',int(sys.argv[1])))
 os.dup2(replacement.fileno(), pending.fileno())
 assert pending.getsockopt(socket.SOL_SOCKET,socket.SO_ERROR) == 0

'''
    try:
        subprocess.run([sys.executable, '-c', code, str(listener.getsockname()[1]), str(closed_port)], env=env, check=True, timeout=5)
    finally:
        stopping.set(); thread.join(2); collector.close(); listener.close(); closed.close()
    failures = [e for e in events if e.get('remote_port') == closed_port]
    assert len(failures) == 2
    assert all(e['retval'] < 0 and e.get('errno') == errno.ECONNREFUSED for e in failures)
    successes = [e for e in events if e.get('remote_port') not in (closed_port, 0) and e.get('event_type', 'connect') == 'connect']
    assert len(successes) == 3 and all(e['retval'] == 0 for e in successes)
    assert any(e.get('event_type') == 'dns_query' and e.get('domain') == 'localhost' and e.get('dns_qtype') in ('A', 'AAAA') for e in events)
