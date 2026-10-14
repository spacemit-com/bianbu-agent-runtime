import io

from bianbu_agent.security.syscall_collector import SyscallCollector


class DummyProc:
    def __init__(self, *args, **kwargs):
        self.stdout = io.StringIO('{"syscall":"finit_module"}\nnot-json\n')
        self.terminated = False

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def test_collector_scopes_command_and_reads_json_events():
    seen = []
    collector = SyscallCollector(
        command="collector --mode ebpf",
        popen_factory=DummyProc,
        event_callback=seen.append,
    )
    assert collector.start(
        cgroup_path="/user.slice/worker.scope",
        agent_id="agent-a",
        instance_id="instance-a",
        lease_id="lease-a",
        policy_version=3,
    )
    collector._thread.join(timeout=1)
    assert seen == [{"syscall": "finit_module"}]
    collector.stop()


def test_collector_disabled_by_default():
    assert not SyscallCollector(command="").enabled
    assert not SyscallCollector(command="").start(
        cgroup_path="/x", agent_id="a", instance_id="i", lease_id="l"
    )


def test_collector_accepts_kernel_relative_cgroup_path():
    seen = []
    collector = SyscallCollector(
        command="collector", popen_factory=DummyProc, event_callback=seen.append
    )
    assert collector.start(
        cgroup_path="user.slice/worker.scope",
        agent_id="a", instance_id="i", lease_id="l",
    )
    assert collector.process is not None
    collector.stop()


def test_collector_passes_trace_directory():
    captured = {}

    class Proc(DummyProc):
        def __init__(self, argv, *args, **kwargs):
            captured["argv"] = argv
            super().__init__()

    collector = SyscallCollector(command="collector", popen_factory=Proc)
    assert collector.start(cgroup_path="/x", agent_id="a", instance_id="i",
                           lease_id="l", trace_dir="/tmp/traces")
    assert captured["argv"][-2:] == ["--trace-dir", "/tmp/traces"]
