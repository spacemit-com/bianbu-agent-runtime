from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys

from bianbu_agent.security.network_learning_collector import (
    NetworkLearningCollector,
    parse_strace_line,
)


def test_parses_ipv4_connect_success():
    line = (
        '1788800000.125000 connect(3, {sa_family=AF_INET, '
        'sin_port=htons(443), sin_addr=inet_addr("203.0.113.8")}, 16) = 0'
    )
    assert parse_strace_line(line) == [{
        "observed_at": 1788800000.125,
        "event_type": "connect",
        "protocol": "tcp",
        "remote_ip": "203.0.113.8",
        "remote_port": 443,
        "outcome": "success",
        "errno": "",
    }]


def test_parses_real_strace_xx_encoded_ipv4_address():
    line = (
        r'1788799632.489364 connect(3, {sa_family=AF_INET, sin_port=htons(9), '
        r'sin_addr=inet_addr("\x31\x32\x37\x2e\x30\x2e\x30\x2e\x31")}, 16) '
        r'= -1 ECONNREFUSED (Connection refused)'
    )
    assert parse_strace_line(line) == [{
        "observed_at": 1788799632.489364,
        "event_type": "connect",
        "protocol": "tcp",
        "remote_ip": "127.0.0.1",
        "remote_port": 9,
        "outcome": "failed",
        "errno": "ECONNREFUSED",
    }]


def test_parses_ipv6_failed_connect():
    line = (
        '1788800001.5 connect(4, {sa_family=AF_INET6, sin6_port=htons(8443), '
        'sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2001:0db8::1", '
        '&sin6_addr), sin6_scope_id=0}, 28) = -1 ECONNREFUSED (Connection refused)'
    )
    assert parse_strace_line(line) == [{
        "observed_at": 1788800001.5,
        "event_type": "connect",
        "protocol": "tcp",
        "remote_ip": "2001:db8::1",
        "remote_port": 8443,
        "outcome": "failed",
        "errno": "ECONNREFUSED",
    }]


def test_parses_udp_dns_query_as_separate_target_and_dns_facts():
    packet = (
        r'\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00'
        r'\x03\x61\x70\x69\x07\x65\x78\x61\x6d\x70\x6c\x65'
        r'\x03\x63\x6f\x6d\x00\x00\x1c\x00\x01'
    )
    line = (
        f'1788800002.25 sendto(5, "{packet}", 33, MSG_NOSIGNAL, '
        '{sa_family=AF_INET, sin_port=htons(53), '
        'sin_addr=inet_addr("192.0.2.53")}, 16) = 33'
    )
    assert parse_strace_line(line) == [
        {
            "observed_at": 1788800002.25,
            "event_type": "connect",
            "protocol": "udp",
            "remote_ip": "192.0.2.53",
            "remote_port": 53,
            "outcome": "success",
            "errno": "",
        },
        {
            "observed_at": 1788800002.25,
            "event_type": "dns_query",
            "protocol": "udp",
            "domain": "api.example.com",
            "remote_ip": "192.0.2.53",
            "remote_port": 53,
            "dns_qtype": "AAAA",
            "outcome": "success",
            "errno": "",
        },
    ]


def test_parses_sendmsg_udp_destination_without_dns_payload():
    line = (
        '1788800003.0 sendmsg(7, {msg_name={sa_family=AF_INET, '
        'sin_port=htons(123), sin_addr=inet_addr("198.51.100.4")}, '
        'msg_namelen=16, msg_iov=[{iov_base="ping", iov_len=4}], '
        'msg_iovlen=1}, 0) = 4'
    )
    assert parse_strace_line(line) == [{
        "observed_at": 1788800003.0,
        "event_type": "connect",
        "protocol": "udp",
        "remote_ip": "198.51.100.4",
        "remote_port": 123,
        "outcome": "success",
        "errno": "",
    }]


def test_ignores_malformed_truncated_and_destination_free_lines():
    assert parse_strace_line("connect(3, <unfinished ...>") == []
    assert parse_strace_line("sendto(3, NULL, 0, 0, NULL, 0) = 0") == []
    assert parse_strace_line("read(3, \"data\", 4) = 4") == []


def test_collector_forwards_objects_without_identity_arguments_and_reports_exit():
    events = []
    states = []
    commands = []

    class Process:
        stdout = io.StringIO('{"event_type":"connect","remote_ip":"203.0.113.8"}\ninvalid\n[]\n')
        stderr = io.StringIO("trace reader stopped")

        def poll(self):
            return 2

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 2

    def popen(argv, **kwargs):
        commands.append((argv, kwargs))
        return Process()

    collector = NetworkLearningCollector(
        command="/usr/bin/python3 /opt/network-learning-collector.py",
        popen_factory=popen,
        event_callback=events.append,
        status_callback=lambda state, reason: states.append((state, reason)),
    )
    assert collector.start(trace_dir="/run/traces") is True
    collector._thread.join(timeout=1)

    assert events == [{"event_type": "connect", "remote_ip": "203.0.113.8"}]
    assert states[0] == ("active", "")
    assert states[-1][0] == "degraded"
    assert "trace reader stopped" in states[-1][1]
    assert commands[0][0] == [
        "/usr/bin/python3", "/opt/network-learning-collector.py",
        "--trace-dir", "/run/traces",
    ]
    assert "--agent-id" not in commands[0][0]


def test_collector_stop_reports_inactive_without_degraded_exit():
    states = []

    class Process:
        stdout = io.StringIO("")
        stderr = io.StringIO("")

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    collector = NetworkLearningCollector(
        command="collector",
        popen_factory=lambda *args, **kwargs: Process(),
        status_callback=lambda state, reason: states.append((state, reason)),
    )
    assert collector.start(trace_dir="/run/traces") is True
    collector.stop()
    assert states[-1] == ("inactive", "stopped")


def test_collector_can_attach_network_strace_to_running_worker(tmp_path):
    commands = []

    class Process:
        def __init__(self, *, output=False):
            self.stdout = io.StringIO("") if output else None
            self.stderr = io.StringIO("")

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    def popen(argv, **kwargs):
        commands.append((argv, kwargs))
        return Process(output="--trace-dir" in argv)

    collector = NetworkLearningCollector(
        command="collector",
        popen_factory=popen,
    )

    assert collector.start(trace_dir=str(tmp_path), attach_pid=4321) is True
    collector.stop()

    assert commands[0][0] == [
        "/usr/bin/strace", "-qq", "-ttt", "-xx", "-s", "512",
        "-e", "trace=connect,sendto,sendmsg", "-o",
        str(tmp_path / ".syscall-trace"), "-p", "4321",
    ]
    assert commands[1][0] == ["collector", "--trace-dir", str(tmp_path)]


def test_packaged_collector_script_emits_stable_jsonl_from_trace_directory(tmp_path):
    trace = tmp_path / ".syscall-trace.4321"
    trace.write_text(
        '1788800000.125 connect(3, {sa_family=AF_INET, sin_port=htons(443), '
        'sin_addr=inet_addr("203.0.113.8")}, 16) = 0\n',
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[2] / "scripts/network-learning-collector.py"
    command = [sys.executable, str(script), "--trace-dir", str(tmp_path), "--once"]

    first = subprocess.run(command, check=True, capture_output=True, text=True)
    second = subprocess.run(command, check=True, capture_output=True, text=True)
    event = json.loads(first.stdout)
    repeated = json.loads(second.stdout)

    assert event == {
        "event_id": repeated["event_id"],
        "observed_at": 1788800000.125,
        "event_type": "connect",
        "protocol": "tcp",
        "remote_ip": "203.0.113.8",
        "remote_port": 443,
        "outcome": "success",
        "errno": "",
        "backend": "strace",
    }


def test_parses_nonblocking_connect_in_progress_as_success():
    events = parse_strace_line(
        '1788800000.5 connect(5, {sa_family=AF_INET, sin_port=htons(443), '
        'sin_addr=inet_addr("8.8.8.8")}, 16) = -1 EINPROGRESS '
        '(Operation now in progress)'
    )

    assert events == [{
        "observed_at": 1788800000.5,
        "event_type": "connect",
        "protocol": "tcp",
        "remote_ip": "8.8.8.8",
        "remote_port": 443,
        "outcome": "success",
        "errno": "",
    }]
