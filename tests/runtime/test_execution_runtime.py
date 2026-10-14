from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

import bianbu_agent.security.execution_runtime as execution
from bianbu_agent.security.execution_runtime import (
    BackendHandle,
    BwrapBackend,
    ExecutionCoordinator,
    ExecutionRecord,
    ExecutionRequest,
    HostProcessBackend,
    RuntimeExecutionError,
)
from bianbu_agent.security.security_runtime import RuntimeStartDecision


def test_attestation_registers_real_systemd_unit_and_network_properties():
    decision = _decision(domains=["example.com"])
    handle = BackendHandle("i1", decision.policy["agent_id"], "host-process", None, 123,
                           "/user.slice/cgroup", unit="bianbu-agent-x.service",
                           unit_scope="user",
                           network_properties=["IPAddressDeny=any", "IPAddressAllow=203.0.113.10"])
    attestation = execution._attestation(decision, handle)
    assert attestation["unit"] == "bianbu-agent-x.service"
    assert attestation["systemd_scope"] == "user"
    assert attestation["network_properties"] == ["IPAddressDeny=any", "IPAddressAllow=203.0.113.10"]


def _decision(*, mode: str = "host", backend: str = "host-process",
              domains: list[str] | None = None,
              entrypoint: list[str] | None = None, cwd: str | None = None,
              resources: dict[str, object] | None = None) -> RuntimeStartDecision:
    policy = {
        "version": 1,
        "agent_id": "com.example.agent",
        "requested_mode": mode,
        "effective_mode": mode,
        "backend": backend,
        "sandbox_required": mode == "sandbox",
        "entrypoint": entrypoint or [],
        "readonly_mounts": [],
        "writable_mounts": [],
        "deny_paths": ["/root", "/etc/shadow"],
        "network": {"mode": "whitelist", "domains": domains or [], "default": "drop"},
        "resources": resources or {},
        "environment_allowlist": ["BIANBU_AGENT_ID", "BIANBU_RUNTIME_ENDPOINT"],
        "host_constraints": {"working_directory": cwd or os.getcwd(), "system_dbus_allowed": False,
                              "port_proxy_required": True},
    }
    return RuntimeStartDecision("lease-1", json.dumps(policy),
                                mode, backend, mode == "sandbox", 2**31, "test")


class _Client:
    def __init__(self, decision: RuntimeStartDecision):
        self.decision = decision
        self.commits: list[tuple] = []
        self.aborts: list[tuple] = []
        self.revokes: list[tuple] = []
        self.stops: list[tuple] = []

    async def prepare_start(self, request):
        assert request.agent_id == "com.example.agent"
        return self.decision

    async def commit_start(self, *args):
        self.commits.append(args)
        return {"state": "committed"}

    async def abort_start(self, *args):
        self.aborts.append(args)
        return True

    async def revoke_agent(self, *args):
        self.revokes.append(args)
        return True

    async def stop_instance(self, *args):
        self.stops.append(args)
        return {"state": "stopped", "changed": True}


@pytest.mark.asyncio
async def test_registered_start_selects_policy_for_logical_worker_agent(tmp_path):
    requests = []

    class MissingDecision:
        async def prepare_start(self, request):
            requests.append(request)
            return None

    coordinator = ExecutionCoordinator(
        MissingDecision(), state_dir=tmp_path, backends={},
    )

    with pytest.raises(RuntimeExecutionError, match="未返回启动决策"):
        await coordinator.start_registered(
            agent_id="com.bianbu.runtime",
            worker_agent_id="agent-0fefac549b59",
        )

    assert requests[0].agent_id == "com.bianbu.runtime"
    assert requests[0].worker_agent_id == "agent-0fefac549b59"


@pytest.mark.asyncio
async def test_registered_start_delegates_missing_optional_security_to_supervisor(
    tmp_path: Path,
) -> None:
    class MissingSecurity(_Client):
        required = False

        async def prepare_start(self, request):
            return None

    class MustNotStart:
        name = "host-process"

        def start(self, *_args):
            raise AssertionError("coordinator must not start a synthetic fallback Worker")

    coordinator = ExecutionCoordinator(
        MissingSecurity(None),
        state_dir=tmp_path,
        backends={"host-process": MustNotStart()},
    )

    with pytest.raises(RuntimeExecutionError, match="未返回启动决策"):
        await coordinator.start_registered(
            agent_id="com.example.agent",
            fallback_entrypoint=("/bin/true",),
        )


def _request(tmp_path: Path, *, mode: str = "host", command: tuple[str, ...] = ("/bin/true",)):
    return ExecutionRequest("com.example.agent", command, mode,
                            {"BIANBU_RUNTIME_ENDPOINT": "test"}, tmp_path,
                            "instance-1")


def test_host_backend_never_installs_seccomp_filter(tmp_path: Path, monkeypatch):
    """host Worker 保留 sudo/setuid 语义：不得下发 seccomp 审计过滤器。"""
    calls = []

    class FakeProcess:
        pid = 4243

        def poll(self): return None
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/user.slice/agent")
    policy = dict(_decision(cwd=str(tmp_path)).policy)
    # 内核只允许 no_new_privs（不可撤销，会让 sudo/pkexec/su 永久失效）或
    # CAP_SYS_ADMIN 的进程装载 seccomp 过滤器，因此即便策略声明了 profile，
    # host 后端也不能把它交给 Worker 自装载。
    policy["seccomp_profile"] = {"path": str(tmp_path / "missing-seccomp.bpf")}
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy), "host", "host-process", False, 2**31, "test")
    request = ExecutionRequest("com.example.agent", ("/bin/true",), "host", {}, tmp_path)

    handle = HostProcessBackend(popen_factory=fake_popen).start(request, decision)

    assert "BIANBU_SECCOMP_AUDIT_FILTER" not in calls[0][1]["env"]
    assert execution._attestation(decision, handle)["kernel_seccomp_log"] is False


def test_sandbox_attestation_reports_kernel_seccomp_log(tmp_path: Path):
    """只有 bwrap 沙箱在内核里真正装载了过滤器，才能声明审计覆盖。"""
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["seccomp_profile"] = {"path": str(tmp_path / "seccomp.bpf")}
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy), "sandbox", "bwrap", True, 2**31, "test")
    handle = BackendHandle("i1", "com.example.agent", "bwrap", None, 123,
                           "/user.slice/cgroup", sandboxed=True)

    assert execution._attestation(decision, handle)["kernel_seccomp_log"] is True


def test_bwrap_argv_has_no_user_injection_and_hides_denied_paths(tmp_path: Path, monkeypatch):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    decision = _decision(
        mode="sandbox",
        backend="bwrap",
        cwd=str(tmp_path),
        entrypoint=["/bin/true"],
    )
    request = _request(tmp_path, mode="sandbox")
    argv = backend._argv(request, decision)
    assert argv[0] == "/usr/bin/bwrap"
    assert "--cap-drop" in argv and "ALL" in argv
    assert "--die-with-parent" in argv and "--unshare-net" in argv
    assert "/etc/shadow" in argv
    shadow = argv.index("/etc/shadow")
    assert argv[shadow - 1] == "/dev/null"
    assert "--" in argv
    assert "--extra-bwrap-option" not in argv


def test_learning_network_mode_does_not_add_blocking_systemd_properties():
    policy = _decision(domains=["example.com"]).policy
    policy["network"] = {"mode": "learning", "domains": ["example.com"]}
    assert execution._network_properties(policy) == []


def test_bwrap_learning_preload_is_explicitly_injected_into_worker(tmp_path):
    decision = _decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path))
    decision.policy["network"] = {"mode": "learning", "domains": []}
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"LD_PRELOAD": "/usr/lib/bianbu-agent-runtime/libbianbu-network-learning.so"},
        tmp_path, "instance-learning",
    )
    argv = BwrapBackend(bwrap_path="/usr/bin/bwrap")._argv(request, decision)
    command = " ".join(argv)
    assert "--setenv LD_PRELOAD /usr/lib/bianbu-agent-runtime/libbianbu-network-learning.so" in command
    assert "--setenv BIANBU_NETWORK_LEARNING_SOCKET /run/bianbu/network-learning/collector.sock" in command


def test_allow_all_network_mode_does_not_add_blocking_systemd_properties():
    policy = _decision(domains=["example.com"]).policy
    policy["network"] = {"mode": "allow_all", "domains": ["example.com"]}

    assert execution._network_properties(policy) == []


@pytest.mark.parametrize("mode,domains,isolated", [
    ("learning", [], False),
    ("learning", ["example.com"], False),
    ("allow_all", [], False),
    ("allow_all", ["example.com"], False),
    ("deny_all", ["example.com"], True),
])
def test_bwrap_network_namespace_obeys_policy_mode(tmp_path, mode, domains, isolated):
    decision = _decision(mode="sandbox", backend="bwrap", domains=domains)
    policy = decision.policy
    policy["network"]["mode"] = mode
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), "sandbox", "bwrap", True, 2**31, "test",
    )
    argv = BwrapBackend(bwrap_path="/usr/bin/bwrap")._argv(
        _request(tmp_path, mode="sandbox"), decision,
    )
    assert ("--unshare-net" in argv) is isolated


def test_empty_network_allowlist_denies_all_host_egress() -> None:
    policy = _decision(mode="host", backend="host-process").policy

    assert execution._network_properties(policy) == ["IPAddressDeny=any"]


def test_bwrap_network_allowlist_uses_systemd_filter(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(execution, "_resolved_resolv_conf", lambda: None)
    monkeypatch.setattr(
        execution.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (execution.socket.AF_INET, execution.socket.SOCK_STREAM, 6, "", ("203.0.113.9", 0))
        ],
    )
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    decision = _decision(mode="sandbox", backend="bwrap", domains=["api.example.com"])
    argv = backend._argv(_request(tmp_path, mode="sandbox"), decision)
    # With an allowlist, bwrap keeps the network namespace available and the
    # enclosing transient systemd unit applies the IP filter.
    assert "--unshare-net" not in argv
    assert execution._network_properties(decision.policy) == [
        "IPAddressDeny=any",
        "IPAddressAllow=203.0.113.9",
    ]


def test_network_allowlist_includes_host_dns_resolver(tmp_path: Path, monkeypatch):
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    monkeypatch.setattr(execution, "_resolved_resolv_conf", lambda: resolv)
    monkeypatch.setattr(
        execution.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (execution.socket.AF_INET, execution.socket.SOCK_STREAM, 6, "", ("203.0.113.9", 0))
        ],
    )
    policy = _decision(
        mode="sandbox", backend="bwrap", domains=["api.example.com"],
    ).policy
    assert execution._network_properties(policy) == [
        "IPAddressDeny=any",
        "IPAddressAllow=127.0.0.53",
        "IPAddressAllow=203.0.113.9",
    ]


def test_network_allowlist_resolves_explicit_localhost_service(monkeypatch):
    monkeypatch.setattr(execution, "_resolved_resolv_conf", lambda: None)

    def resolve(domain, *args, **kwargs):
        assert domain == "localhost"
        return [
            (execution.socket.AF_INET6, execution.socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
            (execution.socket.AF_INET, execution.socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(execution.socket, "getaddrinfo", resolve)
    policy = _decision(
        mode="sandbox", backend="bwrap", domains=["localhost"],
    ).policy

    assert execution._network_properties(policy) == [
        "IPAddressDeny=any",
        "IPAddressAllow=127.0.0.1",
        "IPAddressAllow=::1",
    ]


def test_bwrap_network_allowlist_mounts_resolver_symlink_target(
    tmp_path: Path, monkeypatch,
):
    resolv = tmp_path / "run/systemd/resolve/stub-resolv.conf"
    resolv.parent.mkdir(parents=True)
    resolv.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    monkeypatch.setattr(execution, "_resolved_resolv_conf", lambda: resolv)
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    decision = _decision(
        mode="sandbox", backend="bwrap", domains=["api.example.com"],
    )
    argv = backend._argv(_request(tmp_path, mode="sandbox"), decision)
    target = str(resolv)
    index = argv.index(target)
    assert argv[index - 1] == "--ro-bind"
    assert argv[index + 1] == target


def test_bwrap_offline_sandbox_does_not_mount_host_resolver(
    tmp_path: Path, monkeypatch,
):
    resolv = tmp_path / "resolv.conf"
    resolv.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    monkeypatch.setattr(execution, "_resolved_resolv_conf", lambda: resolv)
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    argv = backend._argv(
        _request(tmp_path, mode="sandbox"),
        _decision(mode="sandbox", backend="bwrap"),
    )
    assert "--unshare-net" in argv
    assert str(resolv) not in argv


def test_bwrap_worker_socket_requires_policy_mount(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    decision = _decision(
        mode="sandbox",
        backend="bwrap",
        cwd=str(tmp_path),
        entrypoint=["/bin/true"],
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"BIANBU_WORKER_SOCKET": str(tmp_path / "instances" / "worker.sock")},
        tmp_path,
    )
    with pytest.raises(RuntimeExecutionError, match="socket writable mount"):
        backend._argv(request, decision)


def test_bwrap_worker_socket_accepts_covering_policy_mount(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    socket_root = tmp_path / "instances"
    socket_root.mkdir()
    decision = _decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path))
    policy = dict(decision.policy)
    policy["writable_mounts"] = [{"host": str(tmp_path), "target": str(tmp_path)}]
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy),
        decision.effective_mode, decision.backend, decision.sandbox_required,
        decision.expires_at, decision.reason,
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"BIANBU_WORKER_SOCKET": str(socket_root / "worker.sock")},
        tmp_path,
    )
    assert "--" in backend._argv(request, decision)


def test_bwrap_mounts_readonly_agent_root_before_writable_state(
    tmp_path: Path,
) -> None:
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = dict(_decision(mode="sandbox", backend="bwrap").policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(tmp_path), "target": "/home/agent"},
    ]
    policy["writable_mounts"] = [
        {"host": str(workspace), "target": "/home/agent/workspace"},
    ]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )

    argv = backend._argv(_request(tmp_path, mode="sandbox"), decision)
    root_bind = argv.index(str(tmp_path), argv.index("--ro-bind"))
    workspace_bind = argv.index(str(workspace), root_bind + 1)

    assert argv[root_bind - 1] == "--ro-bind"
    assert argv[workspace_bind - 1] == "--bind"
    assert root_bind < workspace_bind


def test_bwrap_resolves_worker_home_to_policy_mount(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    socket_root = tmp_path / "instances"
    socket_root.mkdir()
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["writable_mounts"] = [
        {"host": str(tmp_path), "target": "/home/agent"},
        {"host": str(socket_root), "target": "/run/bianbu-agent/instances"},
    ]
    policy["host_constraints"] = {
        **policy["host_constraints"], "sandbox_working_directory": "/home/agent",
    }
    policy["environment_allowlist"] = ["BIANBU_WORKER_SOCKET", "BIANBU_WORKER_SOCKET_TARGET"]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {
            "BIANBU_WORKER_SOCKET": str(socket_root / "worker.sock"),
            "BIANBU_WORKER_SOCKET_TARGET": "/run/bianbu-agent/instances",
        }, tmp_path,
    )
    argv = backend._argv(request, decision)
    home = argv.index("HOME")
    assert argv[home + 1] == "/home/agent"


def test_bwrap_maps_worker_paths_to_policy_targets(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    socket_root = tmp_path / "instances"
    socket_root.mkdir()
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["writable_mounts"] = [
        {"host": str(tmp_path), "target": "/home/agent"},
        {"host": str(socket_root), "target": "/run/bianbu-agent/instances"},
    ]
    policy["host_constraints"] = {
        **policy["host_constraints"],
        "sandbox_working_directory": "/home/agent",
    }
    policy["environment_allowlist"] = [
        *policy["environment_allowlist"],
        "BIANBU_WORKER_SOCKET",
        "BIANBU_WORKER_SOCKET_TARGET",
        "BIANBU_RUNTIME_DATA_DIR",
    ]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {
            "BIANBU_WORKER_SOCKET": str(socket_root / "worker.sock"),
            "BIANBU_WORKER_SOCKET_TARGET": "/run/bianbu-agent/instances",
            "BIANBU_RUNTIME_DATA_DIR": str(tmp_path),
        }, tmp_path,
    )
    argv = backend._argv(request, decision)
    socket_index = argv.index("BIANBU_WORKER_SOCKET")
    assert argv[socket_index + 1] == "/run/bianbu-agent/instances/worker.sock"
    assert "--chdir" in argv and "/home/agent" in argv


@pytest.mark.asyncio
async def test_registered_start_adds_memory_mounts_for_legacy_securityd_policy(
    tmp_path: Path,
) -> None:
    runtime_source = tmp_path / "runtime-source"
    (runtime_source / ".venv" / "bin").mkdir(parents=True)
    (runtime_source / ".venv" / "bin" / "python").touch()
    (runtime_source / "bianbu_agent").mkdir()
    (runtime_source / "resources").mkdir()
    memory_socket = tmp_path / "agent-memoryd.sock"
    memory_socket.touch()
    memory_registry = tmp_path / "memory-registry"
    memory_registry.mkdir()
    decision = _decision(
        mode="sandbox",
        backend="bwrap",
        cwd=str(tmp_path),
        entrypoint=[
            str(runtime_source / ".venv" / "bin" / "python"),
            "-m",
            "bianbu_agent.cli",
        ],
    )
    policy = dict(decision.policy)
    policy["readonly_bind_mounts"] = [{"host": str(tmp_path), "target": "/home/agent"}]
    policy["writable_mounts"] = [
        {"host": str(tmp_path), "target": "/run/bianbu-agent/instances"},
    ]
    decision = RuntimeStartDecision(
        decision.lease_id,
        json.dumps(policy),
        decision.effective_mode,
        decision.backend,
        decision.sandbox_required,
        decision.expires_at,
        decision.reason,
    )
    captured: dict[str, object] = {}

    class FakeBackend:
        name = "bwrap"

        def start(self, request, decision):
            captured["request_env"] = dict(request.env)
            captured["policy"] = decision.policy
            raise RuntimeExecutionError("stop after policy augmentation")

    coordinator = ExecutionCoordinator(
        _Client(decision),
        state_dir=tmp_path / "state",
        backends={"bwrap": FakeBackend()},
    )

    with pytest.raises(RuntimeExecutionError, match="stop after policy augmentation"):
        await coordinator.start_registered(
            agent_id="com.example.agent",
            worker_agent_id="research",
            requested_mode="sandbox",
            env={
                "BIANBU_AGENT_MEMORY_ENABLED": "true",
                "BIANBU_AGENT_MEMORY_SOCKET": str(memory_socket),
                "BIANBU_AGENT_MEMORY_REGISTRY_DIR": str(memory_registry),
                "BIANBU_AGENT_MEMORY_ACCOUNT_ID": "bianbu",
                "BIANBU_AGENT_MEMORY_AGENT_NAME": "bianbu-agent-default",
                "BIANBU_AGENT_MEMORY_REGISTRATION": "1",
            },
            worker_data_root=tmp_path,
            worker_socket_root=tmp_path,
            memory_uds=memory_socket,
            memory_registry_dir=memory_registry,
            bibit_intent_root=tmp_path,
        )

    policy = captured["policy"]
    assert {
        "host": str(memory_socket.resolve()),
        "target": "/run/bianbu/agent-memoryd.sock",
    } in policy["readonly_bind_mounts"]
    assert {
        "host": str(tmp_path.resolve()),
        "target": "/opt/bibit-intent",
    } in policy["readonly_bind_mounts"]
    assert {
        "host": str((runtime_source / ".venv").resolve()),
        "target": str((runtime_source / ".venv").resolve()),
    } in policy["readonly_bind_mounts"]
    assert {
        "host": str((runtime_source / "bianbu_agent").resolve()),
        "target": str((runtime_source / "bianbu_agent").resolve()),
    } in policy["readonly_bind_mounts"]
    assert {
        "host": str(memory_registry.resolve()),
        "target": "/run/bianbu/agent-runtime/memory-registry",
    } in policy["writable_mounts"]
    for key in (
        "BIANBU_AGENT_MEMORY_ENABLED",
        "BIANBU_AGENT_MEMORY_SOCKET",
        "BIANBU_AGENT_MEMORY_REGISTRY_DIR",
        "BIANBU_AGENT_MEMORY_ACCOUNT_ID",
        "BIANBU_AGENT_MEMORY_AGENT_NAME",
        "BIANBU_AGENT_MEMORY_REGISTRATION",
    ):
        assert key in policy["environment_allowlist"]
        assert key in captured["request_env"]
    expected_identity = {
        "BIANBU_WORKER_AGENT_ID": "research",
        "BIANBU_WORKER_SECURITY_AGENT_ID": "com.example.agent",
        "COM_BIANBU_AGENT_ID": "research",
        "BIANBU_SECURITY_SUBJECT": "com.bianbu.agent_id:research",
    }
    for key, value in expected_identity.items():
        assert key in policy["environment_allowlist"]
        assert captured["request_env"][key] == value


def test_bwrap_maps_security_client_path_into_security_home_mount(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    security_home = tmp_path / "securityd"
    client_dir = security_home / "client"
    client_dir.mkdir(parents=True)
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(tmp_path), "target": "/home/agent"},
        {"host": str(security_home), "target": "/opt/bianbu-securityd"},
    ]
    policy["writable_mounts"] = []
    policy["environment_allowlist"] = ["BIANBU_SECURITY_CLIENT_DIR"]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"BIANBU_SECURITY_CLIENT_DIR": str(client_dir)}, tmp_path,
    )
    argv = backend._argv(request, decision)
    index = argv.index("BIANBU_SECURITY_CLIENT_DIR")
    assert argv[index + 1] == "/opt/bianbu-securityd/client"


def test_bwrap_routes_security_client_state_to_writable_worker_mount(tmp_path: Path):
    """A read-only Worker HOME must not make securityd client bootstrap fail."""
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    worker_root = tmp_path / "worker"
    worker_root.mkdir(parents=True)
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["writable_mounts"] = [
        {"host": str(worker_root), "target": "/home/agent/runtime/worker"},
    ]
    policy["environment_allowlist"] = ["BIANBU_SEC_ROOT"]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox", {},
        tmp_path,
    )
    argv = backend._argv(request, decision)
    index = argv.index("BIANBU_SEC_ROOT")
    assert argv[index + 1] == "/home/agent/runtime/worker/securityd-local"


def test_bwrap_maps_fixed_entrypoint_into_readonly_mount(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    entrypoint = runtime_root / ".venv" / "bin" / "python"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("", encoding="utf-8")
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(runtime_root), "target": "/opt/bianbu-securityd"},
    ]
    policy["writable_mounts"] = []
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", (str(entrypoint), "-m", "bianbu_agent.cli"),
        "sandbox", {}, tmp_path,
    )
    argv = backend._argv(request, decision)
    assert argv[-3:] == ["/opt/bianbu-securityd/.venv/bin/python", "-m", "bianbu_agent.cli"]


def test_bwrap_does_not_map_relative_argv_from_runtime_cwd(
    tmp_path: Path, monkeypatch,
) -> None:
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    runtime_root = tmp_path / "runtime"
    entrypoint = runtime_root / ".venv/bin/python"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("", encoding="utf-8")
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(runtime_root)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(runtime_root), "target": "/opt/bianbu-securityd"},
    ]
    policy["writable_mounts"] = []
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent",
        (str(entrypoint), "-m", "bianbu_agent.cli", "--role", "agent-worker"),
        "sandbox", {}, runtime_root,
    )
    monkeypatch.chdir(runtime_root)

    argv = backend._argv(request, decision)

    assert argv[-5:] == [
        "/opt/bianbu-securityd/.venv/bin/python",
        "-m", "bianbu_agent.cli", "--role", "agent-worker",
    ]


def test_bwrap_stages_seccomp_for_user_systemd_worker(tmp_path: Path) -> None:
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    source = tmp_path / "securityd" / "seccomp.bpf"
    source.parent.mkdir()
    source.write_bytes(b"policy")
    socket_dir = tmp_path / "runtime" / "instances" / "instance-1"
    socket_dir.mkdir(parents=True)
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["seccomp_profile"] = {"path": str(source)}
    policy["writable_mounts"] = [{
        "host": str(tmp_path / "runtime" / "instances"),
        "target": "/run/bianbu-agent/instances",
    }]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"BIANBU_WORKER_SOCKET": str(socket_dir / "worker.sock"),
         "BIANBU_WORKER_SOCKET_TARGET": "/run/bianbu-agent/instances"},
        tmp_path, "instance-1",
    )

    argv = backend._argv(request, decision)

    staged = socket_dir / ".seccomp.bpf"
    assert staged.read_bytes() == b"policy"
    assert staged.stat().st_mode & 0o777 == 0o644
    assert argv[0] == "/usr/bin/bwrap"
    assert argv[argv.index("--seccomp") + 1] == "0"
    assert "/bin/sh" not in argv


def test_bwrap_systemd_service_opens_seccomp_without_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "securityd" / "seccomp.bpf"
    source.parent.mkdir()
    source.write_bytes(b"policy")
    socket_dir = tmp_path / "runtime" / "instances" / "instance-1"
    socket_dir.mkdir(parents=True)
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["seccomp_profile"] = {"path": str(source)}
    policy["writable_mounts"] = [{
        "host": str(tmp_path / "runtime" / "instances"),
        "target": "/run/bianbu-agent/instances",
    }]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"BIANBU_WORKER_SOCKET": str(socket_dir / "worker.sock"),
         "BIANBU_WORKER_SOCKET_TARGET": "/run/bianbu-agent/instances"},
        tmp_path, "instance-1",
    )
    captured: dict[str, object] = {}

    def fake_launch(command, *, unit, cwd, env, standard_input_path=None):
        captured["command"] = list(command)
        captured["standard_input_path"] = standard_input_path
        return SimpleNamespace(pid=4243, unit=unit)

    monkeypatch.setattr(execution, "_launch_systemd", fake_launch)
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/user.slice/agent")

    BwrapBackend(bwrap_path="/usr/bin/bwrap").start(request, decision)

    command = captured["command"]
    separator = command.index("--")
    assert command[separator + 1] == "/usr/bin/bwrap"
    assert f"StandardInput=file:{socket_dir / '.seccomp.bpf'}" in command
    assert "PrivateTmp=yes" not in command
    assert captured["standard_input_path"] == str(socket_dir / ".seccomp.bpf")


def test_bwrap_sets_pythonpath_for_runtime_mount(tmp_path: Path) -> None:
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(tmp_path), "target": "/opt/bianbu-securityd"},
    ]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )

    argv = backend._argv(_request(tmp_path, mode="sandbox"), decision)

    index = argv.index("PYTHONPATH")
    assert argv[index + 1] == "/opt/bianbu-securityd"
    lock_index = argv.index("BIANBU_CAPABILITY_LOCK_PATH")
    assert argv[lock_index + 1] == "/home/agent/runtime/worker/.capability.lock"
    model_lock_index = argv.index("BIANBU_MODEL_LOCK_PATH")
    assert argv[model_lock_index + 1] == "/home/agent/runtime/worker/model-locks"
    assert "BIANBU_AGENT_MIGRATION_LOCK_PATH" not in argv


def test_bwrap_adds_readonly_runtime_source_to_pythonpath(tmp_path: Path) -> None:
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    security_home = tmp_path / "securityd"
    security_home.mkdir()
    runtime_source = tmp_path / "runtime-source"
    (runtime_source / "bianbu_agent").mkdir(parents=True)
    (runtime_source / "bianbu_agent" / "__init__.py").write_text("", encoding="utf-8")
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(security_home), "target": "/opt/bianbu-securityd"},
        {"host": str(runtime_source), "target": str(runtime_source)},
    ]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )

    argv = backend._argv(_request(tmp_path, mode="sandbox"), decision)

    index = argv.index("PYTHONPATH")
    assert argv[index + 1] == f"/opt/bianbu-securityd:{runtime_source}"


def test_worker_policy_mounts_python_runtime_at_virtualenv_symlink_target(
    tmp_path: Path,
) -> None:
    actual_runtime = tmp_path / "cpython-3.14.4"
    actual_runtime.mkdir()
    alias_runtime = tmp_path / "cpython-3.14"

    result = execution._with_runtime_worker_policy(
        _decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)),
        memory_uds=None,
        memory_registry_dir=None,
        bibit_intent_root=None,
        python_runtime_root=actual_runtime,
        python_runtime_target=alias_runtime,
    )

    assert {
        "host": str(actual_runtime.resolve()),
        "target": str(alias_runtime),
    } in result.policy["readonly_bind_mounts"]


@pytest.mark.parametrize("escape", [False, True])
def test_worker_operational_logs_have_narrow_writable_mount(tmp_path, escape):
    data = tmp_path / "data"
    logs = data / "logs" / "operational"
    logs.parent.mkdir(parents=True)
    if escape:
        outside = tmp_path / "outside"
        outside.mkdir()
        logs.symlink_to(outside, target_is_directory=True)
    else:
        logs.mkdir()
    decision = RuntimeStartDecision(
        "lease", json.dumps({"readonly_bind_mounts": [{"host": str(data), "target": "/home/agent"}]}),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    def policy():
        return execution._with_runtime_worker_policy(
            decision, memory_uds=None, memory_registry_dir=None, bibit_intent_root=None,
        ).policy
    if escape:
        with pytest.raises(RuntimeExecutionError, match="operational log"):
            policy()
    else:
        assert policy()["writable_mounts"] == [
            {"host": str(logs), "target": "/home/agent/logs/operational"},
        ]


def test_bwrap_maps_symlinked_entrypoint_before_resolving_target(tmp_path: Path):
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    python_runtime = tmp_path / "managed" / "python-runtime"
    python_runtime.mkdir(parents=True)
    real_python = python_runtime / "python-real"
    real_python.write_text("", encoding="utf-8")
    entrypoint = runtime_root / ".venv" / "bin" / "python"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.symlink_to(real_python)
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(runtime_root), "target": "/opt/bianbu-securityd"},
        {"host": str(python_runtime), "target": str(python_runtime)},
    ]
    policy["writable_mounts"] = []
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", (str(entrypoint),),
        "sandbox", {}, tmp_path,
    )
    argv = backend._argv(request, decision)
    assert argv[-1] == "/opt/bianbu-securityd/.venv/bin/python"


def test_bwrap_keeps_resource_properties_with_absolute_systemd_run(tmp_path: Path, monkeypatch):
    decision = _decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path), resources={
        "memory_max": "256M",
        "cpu_quota": "50%",
        "cpu_weight": 250,
        "tasks_max": 64,
        "io_weight": 200,
    })
    captured: dict[str, object] = {}

    def fake_launch(command, *, unit, cwd, env, standard_input_path=None):
        captured["command"] = list(command)
        return SimpleNamespace(pid=4243, unit=unit)

    monkeypatch.setattr(execution, "_launch_systemd", fake_launch)
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/user.slice/agent")
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap")
    handle = backend.start(_request(tmp_path, mode="sandbox"), decision)

    command = captured["command"]
    assert isinstance(command, list)
    assert command[0] == "/usr/bin/systemd-run"
    assert "MemoryMax=256M" in command
    assert "CPUQuota=50%" in command
    assert "CPUWeight=250" in command
    assert "TasksMax=64" in command
    assert "IOWeight=200" in command
    assert handle.cgroup_path == "/user.slice/agent"


def test_worker_scope_gets_cpu_weight_floor_when_policy_omits_it():
    props = execution._resource_properties({"resources": {"memory_max": "2G"}})
    assert f"CPUWeight={execution._DEFAULT_WORKER_CPU_WEIGHT}" in props
    overridden = execution._resource_properties({"resources": {"cpu_weight": 250}})
    assert "CPUWeight=250" in overridden


def test_worker_memory_limit_can_be_disabled_or_overridden(monkeypatch):
    policy = {"resources": {"memory_max": "2G", "cpu_quota": "100%"}}
    # Unset keeps enforcing what the Security policy published.
    monkeypatch.delenv("BIANBU_WORKER_MEMORY_MAX", raising=False)
    assert "MemoryMax=2G" in execution._resource_properties(policy)
    for off in ("off", "OFF", "none", "0", "unlimited"):
        monkeypatch.setenv("BIANBU_WORKER_MEMORY_MAX", off)
        props = execution._resource_properties(policy)
        assert not any(prop.startswith("MemoryMax=") for prop in props)
        assert "CPUQuota=100%" in props
    monkeypatch.setenv("BIANBU_WORKER_MEMORY_MAX", "4G")
    assert "MemoryMax=4G" in execution._resource_properties(policy)


def test_worker_cpu_quota_can_be_disabled_or_overridden(monkeypatch):
    policy = {"resources": {"memory_max": "2G", "cpu_quota": "100%"}}
    # Unset keeps enforcing what the Security policy published.
    monkeypatch.delenv("BIANBU_WORKER_CPU_QUOTA", raising=False)
    assert "CPUQuota=100%" in execution._resource_properties(policy)
    for off in ("off", "OFF", "no", "none", "0", "unlimited"):
        monkeypatch.setenv("BIANBU_WORKER_CPU_QUOTA", off)
        props = execution._resource_properties(policy)
        assert not any(prop.startswith("CPUQuota=") for prop in props)
        assert "MemoryMax=2G" in props
        # 配额关掉后仍然保留 CPUWeight 兜底，避免 Worker 在争抢时被饿死。
        assert f"CPUWeight={execution._DEFAULT_WORKER_CPU_WEIGHT}" in props
    monkeypatch.setenv("BIANBU_WORKER_CPU_QUOTA", "200%")
    assert "CPUQuota=200%" in execution._resource_properties(policy)


def test_worker_memory_and_cpu_caps_can_both_be_disabled(monkeypatch):
    policy = {"resources": {"memory_max": "2G", "cpu_quota": "100%"}}
    monkeypatch.setenv("BIANBU_WORKER_MEMORY_MAX", "off")
    monkeypatch.setenv("BIANBU_WORKER_CPU_QUOTA", "off")
    props = execution._resource_properties(policy)
    assert not any(
        prop.startswith(("MemoryMax=", "CPUQuota=")) for prop in props
    )


def test_systemd_launch_error_preserves_stderr(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise execution.subprocess.CalledProcessError(
            1, args[0], stderr="unit rejected"
        )

    monkeypatch.setattr(execution.subprocess, "run", fail)

    with pytest.raises(RuntimeExecutionError, match="exit=1.*unit rejected"):
        execution._launch_systemd(
            ["systemd-run", "--user", "--", "/bin/true"],
            unit="test.service", cwd=tmp_path, env={},
        )


def test_systemd_clients_use_persistent_user_bus_environment(
    tmp_path: Path, monkeypatch,
) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if "show" in argv:
            return SimpleNamespace(stdout="4242\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(execution.subprocess, "run", run)
    monkeypatch.setattr(execution.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(execution.os, "environ", {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/test",
        "USER": "test",
        "LOGNAME": "test",
        "LANG": "C.UTF-8",
        "LC_TIME": "C",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "INVOCATION_ID": "outer-unit",
        "MANAGERPID": "1088",
        "SYSTEMD_EXEC_PID": "1234",
        "XDG_SESSION_PATH": "/org/freedesktop/login1/session/_1",
        "DISPLAY": ":0",
    })

    process = execution._launch_systemd(
        ["systemd-run", "--user", "--", "/bin/true"],
        unit="test.service", cwd=tmp_path, env={"WORKER_VALUE": "ok"},
    )
    execution._stop_systemd_unit("test.service", timeout=1)

    assert process.pid == 4242
    assert len(calls) == 3
    for _argv, kwargs in calls:
        assert kwargs["env"] == {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/test",
            "USER": "test",
            "LOGNAME": "test",
            "LANG": "C.UTF-8",
            "LC_TIME": "C",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        }
    assert "WORKER_VALUE=ok" in calls[0][0]


def test_host_backend_uses_fixed_command_and_cleans_environment(tmp_path: Path, monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4242
        def poll(self): return None
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/user.slice/agent")
    backend = HostProcessBackend(popen_factory=fake_popen)
    decision = _decision(cwd=str(tmp_path))
    request = ExecutionRequest("com.example.agent", ("/bin/true",), "host",
                               {"BIANBU_RUNTIME_ENDPOINT": "ok", "LD_PRELOAD": "bad",
                                "HTTP_PROXY": "bad", "BIANBU_SANDBOX_ACTIVE": "1"}, tmp_path)
    handle = backend.start(request, decision)
    assert handle.pid == 4242 and handle.cgroup_path == "/user.slice/agent"
    env = calls[0][1]["env"]
    assert env["BIANBU_AGENT_ID"] == "com.example.agent"
    assert env["BIANBU_RUNTIME_ENDPOINT"] == "ok"
    assert env["BIANBU_SANDBOX_ACTIVE"] == "0"
    assert env["BIANBU_CAPABILITY_LOCK_PATH"].endswith(
        "/runtime/worker/.capability.lock")


def test_host_resource_command_applies_filesystem_boundary(tmp_path: Path, monkeypatch):
    decision = _decision(cwd=str(tmp_path))
    policy = dict(decision.policy)
    policy["writable_mounts"] = [{"host": str(tmp_path), "target": "/home/agent"}]
    policy["deny_paths"] = ["/root", "/etc/shadow"]
    policy["host_constraints"] = {
        **policy["host_constraints"], "file_boundary_required": True,
    }
    command, unit = execution._resource_command(policy, ["/bin/true"], "unit-test")
    assert Path(command[0]).name == "systemd-run"
    assert "ProtectSystem=strict" in command
    assert "ProtectHome=read-only" in command
    assert f"ReadWritePaths={tmp_path.resolve()}" in command
    assert "InaccessiblePaths=/root" in command
    assert "InaccessiblePaths=/etc/shadow" in command


def test_system_service_mode_uses_system_transient_unit_for_network_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    policy = dict(_decision(cwd=str(tmp_path)).policy)
    policy["writable_mounts"] = [{"host": str(tmp_path), "target": "/home/agent"}]
    command, unit = execution._resource_command(policy, ["/bin/true"], "unit-test")
    assert command[0].endswith("systemd-run")
    assert "--system" in command
    assert "IPAddressDeny=any" in command
    assert unit == "unit-test"


def test_system_service_host_transient_scope_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    policy = dict(_decision(cwd=str(tmp_path)).policy)
    command, _unit = execution._resource_command(policy, ["/bin/true"], "unit-test")
    text = " ".join(command)
    assert "--system" in command
    assert "--scope" in command
    assert "User=bianbu-runtime" not in text
    assert "Group=bianbu-runtime" not in text
    assert "SupplementaryGroups=bianbu-security" not in text
    assert "BindsTo=bianbu-agent-runtime.service" in text


def test_system_service_scope_binds_to_current_user_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    monkeypatch.setenv(
        "BIANBU_RUNTIME_SYSTEM_UNIT",
        "bianbu-agent-runtime@alice.service",
    )
    policy = dict(_decision(cwd=str(tmp_path)).policy)

    command, _unit = execution._resource_command(
        policy, ["/bin/true"], "unit-test",
    )

    text = " ".join(command)
    assert "BindsTo=bianbu-agent-runtime@alice.service" in text
    assert "After=bianbu-agent-runtime@alice.service" in text


@pytest.mark.parametrize(
    "unit",
    ["../../evil.service", "ssh.service", "bianbu-agent-runtime@alice.scope"],
)
def test_system_service_scope_rejects_unsafe_parent_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit: str,
):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_UNIT", unit)
    policy = dict(_decision(cwd=str(tmp_path)).policy)

    with pytest.raises(execution.RuntimeExecutionError, match="system unit"):
        execution._resource_command(policy, ["/bin/true"], "unit-test")


def test_system_service_mode_keeps_offline_bwrap_in_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path), resources={
        "memory_max": "256M", "tasks_max": 64,
    }).policy)
    command, unit = execution._resource_command(policy, ["/usr/bin/bwrap"], "unit-test")
    assert command[0].endswith("systemd-run")
    assert "--scope" in command
    assert "IPAddressDeny=any" in command
    assert unit == "unit-test"


def test_system_service_scope_inherits_seccomp_stdin_without_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    policy = dict(_decision(
        mode="sandbox", backend="bwrap", cwd=str(tmp_path),
        resources={"memory_max": "256M", "tasks_max": 64},
    ).policy)

    command, _unit = execution._resource_command(
        policy,
        ["/usr/bin/bwrap", "--seccomp", "0", "--", "/bin/true"],
        "unit-test.scope",
        standard_input_path=tmp_path / "seccomp.bpf",
    )

    assert "--scope" in command
    assert "--pipe" not in command
    assert not any(item.startswith("StandardInput=") for item in command)


def test_system_service_scope_properties_are_scope_safe(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path), resources={
        "memory_max": "256M", "tasks_max": 64,
    }).policy)
    command, _unit = execution._resource_command(policy, ["/usr/bin/bwrap"], "unit-test.scope")
    text = " ".join(command)
    assert "--scope" in command
    assert "User=" not in text and "Group=" not in text
    assert "ProtectSystem=" not in text and "WorkingDirectory=" not in text
    assert "MemoryMax=" in text and "TasksMax=" in text


def test_system_scope_launch_returns_worker_pid_and_stops_by_scope(tmp_path: Path, monkeypatch):
    if not execution.shutil.which("systemd-run"):
        pytest.skip("systemd-run unavailable")
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    # Use the production-shaped command but avoid requiring root/system polkit
    # in this user test; the same code path is selected by --scope.
    unit = "bianbu-agent-test-scope.scope"
    command = ["/usr/bin/systemd-run", "--user", "--quiet", "--collect",
               "--unit", unit, "--scope", "--property", "MemoryMax=128M",
               "--", "/bin/sleep", "30"]
    handle = execution._launch_systemd(command, unit=unit, cwd=tmp_path, env={})
    try:
        assert handle.pid > 0
        assert handle.unit == unit
        assert handle.scope == "user"
        assert execution._unit_control_group(unit, scope="user")
    finally:
        execution._stop_systemd_unit(unit, timeout=1, scope="user")


def test_scope_orphan_attestation_uses_control_group_not_main_pid(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(stdout="/system.slice/bianbu-agent-x.scope\n")

    monkeypatch.setattr(execution.subprocess, "run", run)
    monkeypatch.setattr(execution, "current_cgroup_path",
                        lambda pid: "/system.slice/bianbu-agent-x.scope")
    assert execution._unit_owns_pid(
        "bianbu-agent-x.scope", 4242, scope="system") is True
    assert any(any("ControlGroup" in item for item in argv) for argv in calls)
    assert all(all("MainPID" not in item for item in argv) for argv in calls)


def test_host_backend_system_service_starts_direct_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    calls = []

    class FakeProcess:
        pid = 4244
        def poll(self): return None
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/system.slice/bianbu-agent-runtime.service")
    backend = HostProcessBackend(popen_factory=fake_popen)
    handle = backend.start(
        ExecutionRequest("com.example.agent", ("/bin/true",), "host", {}, tmp_path),
        _decision(cwd=str(tmp_path)),
    )
    # Injected Popen is a unit-test seam; production uses systemd-run above.
    assert calls[0][0][0].endswith("systemd-run")
    assert handle.unit_scope == "system"


def test_bwrap_backend_system_service_routes_output_to_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BIANBU_RUNTIME_SYSTEM_SERVICE", "1")
    calls = []

    class FakeProcess:
        pid = 4245
        def poll(self): return None
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/system.slice/bianbu-agent-runtime.service")
    backend = BwrapBackend(bwrap_path="/usr/bin/bwrap", popen_factory=fake_popen)
    policy = dict(_decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path)).policy)
    policy["writable_mounts"] = [{"host": str(tmp_path), "target": "/home/agent"}]
    decision = RuntimeStartDecision(
        "lease-1", json.dumps(policy),
        "sandbox", "bwrap", True, 2**31, "test",
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {
            "BIANBU_WORKER_SOCKET": str(tmp_path / "worker.sock"),
            "BIANBU_WORKER_SOCKET_TARGET": "/home/agent",
        }, tmp_path,
    )
    backend.start(request, decision)
    assert calls[0][1]["stdout"] is None
    assert calls[0][1]["stderr"] is None


def test_host_filesystem_boundary_keeps_agent_package_readonly(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = dict(_decision(cwd=str(tmp_path)).policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(tmp_path), "target": "/home/agent"},
    ]
    policy["writable_mounts"] = [
        {"host": str(workspace), "target": "/home/agent/workspace"},
    ]

    command, _unit = execution._resource_command(
        policy, ["/bin/true"], "unit-test",
    )

    assert "ProtectSystem=strict" in command
    assert "ProtectHome=read-only" in command
    assert f"ReadWritePaths={workspace.resolve()}" in command
    assert f"ReadWritePaths={tmp_path.resolve()}" not in command
    assert f"ReadOnlyPaths={tmp_path.resolve()}" in command


def test_worker_supervisor_uses_configured_security_boundary(tmp_path: Path, monkeypatch):
    from bianbu_agent.config import (
        RuntimeConfig,
        RuntimeSecurityConfig,
    )
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config = RuntimeConfig(
        data_dir=tmp_path,
        security=RuntimeSecurityConfig(
            securityd_uds="/run/test/security.sock",
            client_dir="/opt/test-security/client",
        ),
    )
    supervisor = WorkerSupervisor(config, agent_id="default")
    assert supervisor.agent_id == "default"
    assert supervisor.security_agent_id == "com.bianbu.runtime"
    assert supervisor.coordinator.security_client.required is True


@pytest.mark.asyncio
async def test_worker_health_timeout_is_deferred_while_run_is_active(
    tmp_path: Path,
) -> None:
    from bianbu_agent.config import (
        RuntimeConfig,
        RuntimeControlConfig,
        RuntimeProcessConfig,
    )
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config = RuntimeConfig(
        data_dir=tmp_path,
        runtime=RuntimeProcessConfig(control=RuntimeControlConfig(
            heartbeat_interval_seconds=0.001,
            heartbeat_timeout_seconds=0.001,
        )),
    )
    supervisor = WorkerSupervisor(config, agent_id="default")

    class Client:
        async def health(self):
            await asyncio.sleep(1)

    class Runs:
        def has_active_for(self, selected):
            return selected is supervisor

    supervisor.client = Client()
    supervisor.identity = SimpleNamespace()
    supervisor.run_manager = Runs()
    supervisor._ready.set()

    task = asyncio.create_task(supervisor._health_loop())
    await asyncio.sleep(0.01)
    supervisor._closed = True
    await task

    assert supervisor._failure is None


@pytest.mark.asyncio
async def test_worker_health_timeout_reports_nonempty_failure_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import (
        RuntimeConfig,
        RuntimeControlConfig,
        RuntimeProcessConfig,
    )
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config = RuntimeConfig(
        data_dir=tmp_path,
        runtime=RuntimeProcessConfig(control=RuntimeControlConfig(
            heartbeat_interval_seconds=0.001,
            heartbeat_timeout_seconds=0.001,
        )),
    )
    supervisor = WorkerSupervisor(config, agent_id="default")

    class Client:
        async def health(self):
            await asyncio.sleep(1)

    reasons = []

    async def handle_failure(error):
        reasons.append(str(error))
        supervisor._closed = True

    supervisor.client = Client()
    supervisor.identity = SimpleNamespace()
    supervisor._ready.set()
    monkeypatch.setattr(supervisor, "_handle_failure", handle_failure)

    await supervisor._health_loop()

    assert reasons == ["TimeoutError"]


@pytest.mark.asyncio
async def test_logical_agent_worker_uses_runtime_process_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent import AgentHome, AgentPackageManager, AgentSpec
    from bianbu_agent.config import RuntimeConfig, RuntimeSecurityConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    packages = AgentPackageManager(home)
    packages.create(AgentSpec(id="default", name="Default"))
    packages.create(AgentSpec(id="research", name="Research"))
    config = RuntimeConfig(
        data_dir=home.root,
        security=RuntimeSecurityConfig(),
    )
    supervisor = WorkerSupervisor(config, agent_id="research")
    captured: dict[str, object] = {}

    async def fake_start_registered(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after policy request")

    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_RUNTIME_PACKAGE_DIGEST", "sha256:" + "a" * 64)
    monkeypatch.setattr(
        supervisor.coordinator, "start_registered", fake_start_registered,
    )

    with pytest.raises(RuntimeError, match="stop after policy request"):
        await supervisor._start_impl()

    default_package = packages.inspect("default")
    research_package = packages.inspect("research")
    assert default_package is not None
    assert research_package is not None
    assert default_package.content_digest != research_package.content_digest
    assert supervisor.agent_id == "research"
    assert captured["agent_id"] == "com.bianbu.runtime"
    assert captured["env"]["BIANBU_WORKER_AGENT_ID"] == "research"
    assert captured["env"]["COM_BIANBU_AGENT_ID"] == "research"
    assert captured["env"]["BIANBU_SECURITY_SUBJECT"] == (
        "com.bianbu.agent_id:research"
    )


@pytest.mark.asyncio
async def test_worker_supervisor_memory_identity_uses_explicit_agent_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config = RuntimeConfig(data_dir=tmp_path)
    logical_agent_id = "com.bianbu.123456789abc"
    supervisor = WorkerSupervisor(config, agent_id=logical_agent_id)
    captured: dict[str, object] = {}

    async def fake_start_registered(**kwargs):
        captured["env"] = dict(kwargs["env"])
        raise RuntimeError("force development fallback")

    async def fake_start_development_process(env):
        captured["fallback_env"] = dict(env)
        return ExecutionRecord(
            instance_id=supervisor.instance_id,
            agent_id=supervisor.agent_id,
            backend="host-process",
            pid=1234,
            cgroup_path="",
            unit="",
            state="RUNNING",
            lease_id="dev",
            started_at=0.0,
            sandboxed=False,
            container_id="",
        )

    class FakeClient:
        last_peer_pid = 1234

        async def hello(self):
            return {
                "agent_id": supervisor.agent_id,
                "instance_id": supervisor.instance_id,
                "lease_id": "dev",
                "effective_mode": "host",
                "backend": "host-process",
                "sandboxed": False,
                "container_id": "",
                "pid": 1234,
            }

        async def activate(self):
            return {"status": "active", "generation_id": 1}

    async def fake_audit_event(*args, **kwargs):
        return None

    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    monkeypatch.setenv("BIANBU_RUNTIME_PACKAGE_DIGEST", "sha256:" + "a" * 64)
    monkeypatch.setenv("BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER", "1")
    monkeypatch.setattr(supervisor.coordinator, "start_registered", fake_start_registered)
    monkeypatch.setattr(supervisor, "_start_development_process", fake_start_development_process)
    monkeypatch.setattr(supervisor, "_audit_event", fake_audit_event)
    monkeypatch.setattr(supervisor, "_health_loop", lambda: asyncio.sleep(0))
    supervisor.client = FakeClient()

    await supervisor._start_impl()

    assert supervisor.agent_id == logical_agent_id
    assert supervisor.security_agent_id == "com.bianbu.runtime"
    assert captured["env"]["BIANBU_AGENT_MEMORY_AGENT_NAME"] == logical_agent_id
    assert captured["fallback_env"]["BIANBU_AGENT_MEMORY_AGENT_NAME"] == logical_agent_id


@pytest.mark.asyncio
async def test_development_worker_fallback_passes_synthetic_lease_to_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config = RuntimeConfig(data_dir=tmp_path)
    supervisor = WorkerSupervisor(config, agent_id="default")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 9876
        returncode = None

    async def fake_exec(*command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(
        "bianbu_agent.security.runtime_process.supervisor.asyncio.create_subprocess_exec",
        fake_exec,
    )
    supervisor.instance_id = "com.bianbu.agent-test"
    record = await supervisor._start_development_process({
        "BIANBU_WORKER_INSTANCE_ID": supervisor.instance_id,
        "BIANBU_AGENT_MEMORY_AGENT_NAME": supervisor.agent_id,
    })

    assert record.lease_id == "dev"
    assert captured["env"]["BIANBU_WORKER_LEASE_ID"] == "dev"
    assert captured["env"]["BIANBU_WORKER_BACKEND"] == "host-process"
    assert captured["env"]["BIANBU_WORKER_UNMANAGED_FALLBACK"] == "1"


@pytest.mark.asyncio
async def test_development_worker_stop_confirms_process_termination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    supervisor = WorkerSupervisor(
        RuntimeConfig(data_dir=tmp_path), agent_id="default",
    )

    class FakeProcess:
        pid = 9879

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            return 0 if self.returncode is None else self.returncode

    process = FakeProcess()

    async def fake_exec(*_command, **_kwargs):
        return process

    monkeypatch.setattr(
        "bianbu_agent.security.runtime_process.supervisor.asyncio.create_subprocess_exec",
        fake_exec,
    )

    record = await supervisor._start_development_process({})
    supervisor.record = record
    await supervisor.stop(reason="Agent deleted")

    assert process.terminated is True
    assert process.returncode == 0
    assert supervisor.record is None


@pytest.mark.asyncio
async def test_strict_worker_stop_retains_record_when_backend_stop_fails(
    tmp_path: Path,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    supervisor = WorkerSupervisor(
        RuntimeConfig(data_dir=tmp_path), agent_id="default",
    )
    record = SimpleNamespace(instance_id="worker-research", lease_id="managed")

    class Coordinator:
        stop_timeout = 0.1

        async def stop(self, *_args, **_kwargs):
            raise RuntimeError("backend still running")

    supervisor.record = record
    supervisor.coordinator = Coordinator()

    with pytest.raises(RuntimeError, match="still running"):
        await supervisor.stop(reason="Agent deleted", strict=True)

    assert supervisor.record is record


@pytest.mark.asyncio
async def test_agent_sandbox_worker_start_is_strict_security_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config = RuntimeConfig(data_dir=tmp_path)
    supervisor = WorkerSupervisor(config, agent_id="default")
    captured: dict[str, object] = {}

    async def fake_start_registered(**kwargs):
        captured["requested_mode"] = kwargs["requested_mode"]
        raise RuntimeError("securityd missing")

    async def fake_start_development_process(env):
        captured["fallback_env"] = dict(env)
        raise AssertionError("sandbox security startup must not use unmanaged fallback")

    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    monkeypatch.setenv("BIANBU_RUNTIME_PACKAGE_DIGEST", "sha256:" + "a" * 64)
    # ``BIANBU_SANDBOX=agent`` 只表示"走受管 Worker、不许降级"，
    # 具体 host/sandbox 由 security.runtime 推导。
    monkeypatch.setenv("BIANBU_SANDBOX", "agent")
    monkeypatch.setenv("BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER", "1")
    monkeypatch.setattr(supervisor.coordinator, "start_registered", fake_start_registered)
    monkeypatch.setattr(supervisor, "_start_development_process", fake_start_development_process)

    with pytest.raises(RuntimeError, match="securityd missing"):
        await supervisor._start_impl()

    # 沙箱与否交给 security.runtime 判定，Runtime 只提交 auto。
    assert captured["requested_mode"] == "auto"
    assert "fallback_env" not in captured


def test_synthetic_development_lease_is_not_treated_as_managed() -> None:
    from bianbu_agent.security.runtime_process.worker import _is_submitted_lease

    assert _is_submitted_lease("dev") is False
    assert _is_submitted_lease("") is False
    assert _is_submitted_lease("lease-from-securityd") is True


def test_unmanaged_worker_fallback_requires_development_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.server.runtime import _is_unmanaged_worker_fallback

    monkeypatch.setenv("BIANBU_WORKER_UNMANAGED_FALLBACK", "1")
    monkeypatch.setenv("BIANBU_WORKER_MANAGED", "0")
    monkeypatch.setenv("BIANBU_WORKER_LEASE_ID", "dev")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    monkeypatch.setenv("BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER", "1")
    assert _is_unmanaged_worker_fallback() is True

    for key, value in (
        ("BIANBU_RUNTIME_MODE", "production"),
        ("BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER", "0"),
        ("BIANBU_WORKER_LEASE_ID", "submitted-lease"),
        ("BIANBU_WORKER_MANAGED", "1"),
    ):
        monkeypatch.setenv(key, value)
        assert _is_unmanaged_worker_fallback() is False
        monkeypatch.setenv(key, {
            "BIANBU_RUNTIME_MODE": "development",
            "BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER": "1",
            "BIANBU_WORKER_LEASE_ID": "dev",
            "BIANBU_WORKER_MANAGED": "0",
        }[key])


@pytest.mark.asyncio
async def test_development_worker_fallback_maps_canonical_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import (
        RuntimeConfig, RuntimeProcessConfig, RuntimeWorkerConfig,
    )
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    config_path = tmp_path / "config.yaml"
    config_path.write_text("data_dir: .\n", encoding="utf-8")
    config = RuntimeConfig(
        data_dir=tmp_path,
        config_path=config_path,
        runtime=RuntimeProcessConfig(
            worker=RuntimeWorkerConfig(
                entrypoint=("/bin/echo", "--config", "/home/agent/config.yaml", "worker"),
            ),
        ),
    )
    supervisor = WorkerSupervisor(config, agent_id="default")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 9877
        returncode = None

    async def fake_exec(*command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr(
        "bianbu_agent.security.runtime_process.supervisor.asyncio.create_subprocess_exec",
        fake_exec,
    )
    await supervisor._start_development_process({})
    assert captured["command"] == ("/bin/echo", "--config", str(config_path), "worker")


@pytest.mark.asyncio
async def test_worker_start_uses_bounded_hello_retry_after_fallback_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.security.runtime_process.supervisor import WorkerSupervisor

    supervisor = WorkerSupervisor(RuntimeConfig(data_dir=tmp_path), agent_id="default")
    calls: list[str] = []

    class FakeClient:
        async def hello(self):
            calls.append("hello")
            return {"type": "hello.result"}

    supervisor.client = FakeClient()
    result = await supervisor._wait_hello()
    assert result["type"] == "hello.result"
    assert calls == ["hello"]


@pytest.mark.asyncio
async def test_coordinator_commits_and_stops(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/user.slice/agent")
    client = _Client(_decision(cwd=str(tmp_path)))
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    coordinator = ExecutionCoordinator(client, state_dir=tmp_path,
                                       backends={"host-process": backend})
    record = await coordinator.start(_request(tmp_path))
    assert record.state == "RUNNING" and client.commits
    assert await coordinator.stop(record.instance_id) is True
    assert coordinator.list()[0]["state"] == "STOPPED"
    assert client.revokes == []
    # D-9：正常停止必须收敛 securityd 侧本实例的 lease/instance，且不撤销整个 Agent
    assert client.stops == [(record.lease_id, record.instance_id, "requested")]


@pytest.mark.asyncio
async def test_coordinator_stop_survives_old_daemon_without_stop_instance(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/user.slice/agent")
    client = _Client(_decision(cwd=str(tmp_path)))

    async def failing_stop(*args):
        raise RuntimeError("UnknownMethod StopInstance")

    client.stop_instance = failing_stop
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    coordinator = ExecutionCoordinator(client, state_dir=tmp_path,
                                       backends={"host-process": backend})
    record = await coordinator.start(_request(tmp_path))
    assert await coordinator.stop(record.instance_id) is True
    assert coordinator.list()[0]["state"] == "STOPPED"
    assert client.revokes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("network", [
    {"mode": "learning", "domains": [], "version": 7},
    {"mode": "allow_all", "domains": [], "version": 7, "observe": True},
])
async def test_learning_coordinator_starts_after_commit_and_overrides_event_identity(
    tmp_path: Path, monkeypatch, network,
):
    order = []
    collectors = []
    decision = _decision(cwd=str(tmp_path))
    policy = decision.policy
    policy["network"] = network
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), decision.effective_mode,
        decision.backend, decision.sandbox_required, decision.expires_at,
        decision.reason,
    )

    class Client(_Client):
        def __init__(self):
            super().__init__(decision)
            self.learning_events = []
            self.learning_states = []

        async def commit_start(self, *args):
            order.append("commit")
            return await super().commit_start(*args)

        async def record_network_learning_event(self, *args):
            self.learning_events.append(args)
            order.append("event")
            return True

        async def report_network_learning_collector(self, *args):
            self.learning_states.append(args)
            order.append(f"status:{args[4]}")
            return True

    class Collector:
        enabled = True

        def __init__(self, *, event_callback, status_callback):
            self.event_callback = event_callback
            self.status_callback = status_callback
            self.stopped = False
            collectors.append(self)

        def start(self, *, trace_dir, attach_pid=None):
            order.append("collector-start")
            assert Path(trace_dir).name == "instance-1"
            assert attach_pid == 1111
            self.status_callback("active", "")
            self.event_callback({
                "agent_id": "forged", "instance_id": "forged",
                "lease_id": "forged", "pid": 999999,
                "event_id": "evt-1", "observed_at": 1788800000.0,
                "event_type": "connect", "protocol": "tcp",
                "remote_ip": "203.0.113.8", "remote_port": 443,
                "outcome": "success", "errno": "",
                "backend": "forged", "policy_version": 999,
            })
            return True

        def stop(self, timeout=1.0):
            self.stopped = True
            self.status_callback("inactive", "stopped")
            order.append("collector-stop")

    class Backend(HostProcessBackend):
        def stop(self, handle, *, timeout):
            order.append("backend-stop")

    monkeypatch.setattr(execution, "NetworkLearningCollector", Collector)
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    backend = Backend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    client = Client()
    coordinator = ExecutionCoordinator(
        client, state_dir=tmp_path, backends={"host-process": backend})
    socket_path = tmp_path / "instances" / "worker.sock"
    record = await coordinator.start(ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host",
        {"BIANBU_WORKER_SOCKET": str(socket_path)}, tmp_path, "instance-1"))
    for _ in range(4):
        await asyncio.sleep(0)

    assert order.index("commit") < order.index("collector-start")
    assert client.learning_events == [(
        "com.example.agent", "instance-1", "lease-1", 1111,
        {
            "event_id": "evt-1", "observed_at": 1788800000.0,
            "event_type": "connect", "protocol": "tcp",
            "domain": "", "remote_ip": "203.0.113.8", "remote_port": 443,
            "dns_qtype": "", "outcome": "success", "errno": "",
            "backend": "strace", "policy_version": 7,
        },
    )]
    assert record.state == "RUNNING"
    await coordinator.stop(record.instance_id)
    for _ in range(2):
        await asyncio.sleep(0)
    assert collectors[0].stopped is True
    assert order.index("status:inactive") < order.index("backend-stop")


@pytest.mark.asyncio
async def test_learning_collector_unavailable_reports_degraded_without_blocking_worker(
    tmp_path: Path, monkeypatch,
):
    decision = _decision(cwd=str(tmp_path))
    policy = decision.policy
    policy["network"] = {"mode": "learning", "domains": [], "version": 8}
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), decision.effective_mode,
        decision.backend, decision.sandbox_required, decision.expires_at,
        decision.reason,
    )

    class Client(_Client):
        def __init__(self):
            super().__init__(decision)
            self.learning_states = []

        async def report_network_learning_collector(self, *args):
            self.learning_states.append(args)
            return True

    class DisabledCollector:
        enabled = False

        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(execution, "NetworkLearningCollector", DisabledCollector)
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    client = Client()
    coordinator = ExecutionCoordinator(
        client, state_dir=tmp_path, backends={"host-process": backend})

    record = await coordinator.start(_request(tmp_path))
    assert record.state == "RUNNING"
    assert client.learning_states[-1][4] == "degraded"
    assert "unavailable" in client.learning_states[-1][5]


@pytest.mark.asyncio
async def test_learning_status_transport_failure_does_not_stop_worker(
    tmp_path: Path, monkeypatch,
):
    decision = _decision(cwd=str(tmp_path))
    policy = decision.policy
    policy["network"] = {"mode": "learning", "domains": [], "version": 8}
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), decision.effective_mode,
        decision.backend, decision.sandbox_required, decision.expires_at,
        decision.reason,
    )

    class Client(_Client):
        async def report_network_learning_collector(self, *args):
            raise RuntimeError("security transport unavailable")

    class DisabledCollector:
        enabled = False

        def __init__(self, **kwargs):
            pass

    monkeypatch.setattr(execution, "NetworkLearningCollector", DisabledCollector)
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    coordinator = ExecutionCoordinator(
        Client(decision), state_dir=tmp_path,
        backends={"host-process": backend})

    record = await coordinator.start(_request(tmp_path))
    assert record.state == "RUNNING"


@pytest.mark.asyncio
async def test_non_learning_coordinator_does_not_start_network_collector(
    tmp_path: Path, monkeypatch,
):
    class MustNotConstruct:
        def __init__(self, **kwargs):
            raise AssertionError("non-learning mode must not construct collector")

    monkeypatch.setattr(execution, "NetworkLearningCollector", MustNotConstruct)
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    client = _Client(_decision(cwd=str(tmp_path)))
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    coordinator = ExecutionCoordinator(
        client, state_dir=tmp_path, backends={"host-process": backend})
    assert (await coordinator.start(_request(tmp_path))).state == "RUNNING"


def test_non_learning_host_handle_retains_trace_directory_for_hot_attach(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(pid=1111))
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host",
        {"BIANBU_WORKER_SOCKET": str(tmp_path / "instances" / "worker.sock")},
        tmp_path, "instance-1",
    )

    handle = backend.start(request, _decision(cwd=str(tmp_path)))

    assert handle.trace_dir == str(
        Path(request.env["BIANBU_WORKER_SOCKET"]).resolve().parent
        / ".syscall-traces" / request.instance_id
    )


def test_production_uses_external_syscall_collector_instead_of_default_strace(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.delenv("BIANBU_SYSCALL_STRACE", raising=False)
    monkeypatch.setattr(
        execution, "_external_syscall_collector_available", lambda: True,
        raising=False,
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host",
        {"BIANBU_WORKER_SOCKET": str(tmp_path / "worker.sock")},
        tmp_path, "instance-1",
    )

    command, trace_dir = execution._strace_command(
        request.command, request, _decision(cwd=str(tmp_path)),
        optional_syscall_audit=True,
    )

    assert command == ["/bin/true"]
    assert trace_dir.endswith("/.syscall-traces/instance-1")


def test_production_does_not_wrap_worker_when_external_collector_unavailable(
    tmp_path: Path, monkeypatch,
):
    """An unavailable optional collector must not risk freezing the Worker in ptrace."""
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.delenv("BIANBU_SYSCALL_STRACE", raising=False)
    monkeypatch.setattr(
        execution, "_external_syscall_collector_available", lambda: False,
        raising=False,
    )
    monkeypatch.setattr(execution.shutil, "which", lambda name: f"/usr/bin/{name}")
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host",
        {"BIANBU_WORKER_SOCKET": str(tmp_path / "worker.sock")},
        tmp_path, "instance-1",
    )

    command, _ = execution._strace_command(
        request.command, request, _decision(cwd=str(tmp_path)),
        optional_syscall_audit=True,
    )

    assert command == ["/bin/true"]


def test_explicit_syscall_strace_overrides_external_collector(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.setenv("BIANBU_SYSCALL_STRACE", "1")
    monkeypatch.setattr(
        execution, "_external_syscall_collector_available", lambda: True,
        raising=False,
    )
    monkeypatch.setattr(execution.shutil, "which", lambda name: f"/usr/bin/{name}")
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host",
        {"BIANBU_WORKER_SOCKET": str(tmp_path / "worker.sock")},
        tmp_path, "instance-1",
    )

    command, _ = execution._strace_command(
        request.command, request, _decision(cwd=str(tmp_path)),
        optional_syscall_audit=True,
    )

    assert command[0] == "/usr/bin/strace"


@pytest.mark.asyncio
async def test_host_worker_network_policy_hot_update_starts_and_stops_learning_collector(
    tmp_path: Path, monkeypatch,
):
    starts = []
    stops = []
    states = []

    class Collector:
        enabled = True

        def __init__(self, *, event_callback, status_callback):
            self.status_callback = status_callback

        def start(self, *, trace_dir, attach_pid=None):
            starts.append((trace_dir, attach_pid))
            self.status_callback("active", "")
            return True

        def stop(self, timeout=1.0):
            stops.append(timeout)

    class Client:
        async def report_network_learning_collector(self, *args):
            states.append(args)
            return True

        async def record_network_learning_event(self, *args):
            return True

    monkeypatch.setattr(execution, "NetworkLearningCollector", Collector)
    coordinator = ExecutionCoordinator(Client(), state_dir=tmp_path)
    record = ExecutionRecord(
        "instance-1", "com.bianbu.runtime", "host-process", 4321,
        "/cg/worker", "worker.scope", execution.ExecutionState.RUNNING,
        "lease-1", 1.0, False,
    )
    handle = execution.BackendHandle(
        "instance-1", "com.bianbu.runtime", "host-process", object(),
        4321, "/cg/worker", unit="worker.scope",
        trace_dir="/run/traces/instance-1",
    )
    coordinator._records[record.instance_id] = record
    coordinator._handles[record.instance_id] = handle

    entered = await coordinator.apply_network_learning_policy(
        "com.bianbu.runtime", "learning", 9,
    )
    await asyncio.sleep(0)
    left = await coordinator.apply_network_learning_policy(
        "com.bianbu.runtime", "deny_all", 10,
    )

    assert entered == {"active_instances": 1, "collectors_started": 1,
                       "collectors_stopped": 0, "degraded_instances": 0}
    assert starts == [("/run/traces/instance-1", 4321)]
    assert left == {"active_instances": 1, "collectors_started": 0,
                    "collectors_stopped": 1, "degraded_instances": 0}
    assert stops == [coordinator.stop_timeout]
    assert states[-1][4:] == ("inactive", "network policy changed to deny_all")


def test_learning_host_launch_traces_network_syscalls(tmp_path: Path, monkeypatch):
    captured = []
    decision = _decision(cwd=str(tmp_path))
    policy = decision.policy
    policy["network"] = {"mode": "learning", "domains": []}
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), decision.effective_mode,
        decision.backend, decision.sandbox_required, decision.expires_at,
        decision.reason,
    )
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    monkeypatch.setenv("BIANBU_NETWORK_LEARNING_STRACE", "1")
    monkeypatch.setattr(execution.shutil, "which", lambda name: f"/usr/bin/{name}")
    backend = HostProcessBackend(popen_factory=lambda argv, **kwargs: (
        captured.append(argv) or SimpleNamespace(pid=1111)))
    socket_path = tmp_path / "instances" / "worker.sock"
    backend.start(ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host",
        {"BIANBU_WORKER_SOCKET": str(socket_path)}, tmp_path, "instance-1"),
        decision)
    command = " ".join(str(value) for value in captured[0])
    assert "strace" in command
    assert "connect" in command
    assert "sendto" in command
    assert "sendmsg" in command
    assert ".syscall-traces/instance-1/.syscall-trace" in command


def test_learning_bwrap_launch_traces_network_syscalls(tmp_path: Path, monkeypatch):
    captured = []
    decision = _decision(
        mode="sandbox", backend="bwrap", cwd=str(tmp_path),
        entrypoint=["/bin/true"],
    )
    policy = decision.policy
    policy["network"] = {"mode": "learning", "domains": []}
    socket_root = tmp_path / "instances"
    socket_root.mkdir()
    policy["writable_mounts"] = [{
        "host": str(socket_root), "target": str(socket_root),
    }]
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), decision.effective_mode,
        decision.backend, decision.sandbox_required, decision.expires_at,
        decision.reason,
    )
    monkeypatch.setattr(execution, "current_cgroup_path", lambda pid: "/worker.scope")
    monkeypatch.setenv("BIANBU_NETWORK_LEARNING_STRACE", "1")
    monkeypatch.setattr(execution.shutil, "which", lambda name: f"/usr/bin/{name}")
    backend = BwrapBackend(
        bwrap_path="/usr/bin/bwrap",
        popen_factory=lambda argv, **kwargs: (
            captured.append(argv) or SimpleNamespace(pid=1111)),
    )
    socket_path = socket_root / "worker.sock"
    backend.start(ExecutionRequest(
        "com.example.agent", ("/bin/true",), "sandbox",
        {"BIANBU_WORKER_SOCKET": str(socket_path)}, tmp_path, "instance-1"),
        decision)
    command = " ".join(str(value) for value in captured[0])
    assert "strace" in command
    assert "connect" in command
    assert "sendto" in command
    assert "sendmsg" in command
    assert ".syscall-traces/instance-1/.syscall-trace" in command


@pytest.mark.asyncio
async def test_coordinator_aborts_and_records_failure(tmp_path: Path):
    class BrokenBackend:
        name = "host-process"
        def start(self, *args): raise RuntimeExecutionError("backend unavailable")
        def stop(self, *args, **kwargs): pass

    client = _Client(_decision())
    coordinator = ExecutionCoordinator(client, state_dir=tmp_path,
                                       backends={"host-process": BrokenBackend()})
    with pytest.raises(RuntimeExecutionError, match="backend unavailable"):
        await coordinator.start(_request(tmp_path))
    assert client.aborts
    assert coordinator.list()[0]["state"] == "FAILED"


def test_production_requires_manager(monkeypatch):
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.delenv("BIANBU_RUNTIME_MANAGED", raising=False)
    assert execution.production_launch_allowed() is False


@pytest.mark.asyncio
async def test_proxy_opens_only_after_commit_and_closes_on_stop(tmp_path: Path):
    class _ListeningBackend:
        name = "host-process"

        def start(self, request, decision):
            return execution.BackendHandle(
                request.instance, request.agent_id, self.name,
                SimpleNamespace(pid=2222, poll=lambda: None), 2222,
                "/user.slice/agent", unit="", sandboxed=False,
            )

        def stop(self, handle, *, timeout):
            return None

    committed_proxy_ports: list[int | None] = []

    class _CommitClient(_Client):
        async def commit_start(self, *args):
            record = coordinator.list()[0]
            committed_proxy_ports.append(record.get("proxy_port"))
            return await super().commit_start(*args)

    client = _CommitClient(_decision(cwd=str(tmp_path)))
    coordinator = ExecutionCoordinator(
        client,
        state_dir=tmp_path,
        backends={"host-process": _ListeningBackend()},
    )
    request = ExecutionRequest(
        "com.example.agent", ("/bin/true",), "host", {}, tmp_path,
        "proxy-instance", target_port=9, listen_port=0,
    )
    # The proxy is deliberately created after CommitStart, so the ledger has
    # no externally reachable port while the lease is still pending.
    record = await coordinator.start(request)
    assert committed_proxy_ports == [None]
    assert record.proxy_port and record.proxy_port > 0
    with socket.create_connection(("127.0.0.1", record.proxy_port), timeout=1.0):
        pass
    await coordinator.stop(record.instance_id)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", record.proxy_port), timeout=0.2)


@pytest.mark.asyncio
async def test_reap_orphans_does_not_signal_zero_pid(tmp_path: Path):
    client = _Client(_decision())
    coordinator = ExecutionCoordinator(client, state_dir=tmp_path, backends={})
    coordinator._records["pending"] = execution.ExecutionRecord(
        "pending", "com.example.agent", "host-process", 0, "", "", "STARTING",
        "lease-1", "sha256:" + "a" * 64, 0.0, False,
    )
    assert await coordinator.reap_orphans() == 0
    assert coordinator.list()[0]["state"] == "FAILED"


@pytest.mark.asyncio
async def test_privileged_network_forwarding_preserves_source_and_dns(tmp_path):
    received = asyncio.Event()
    values = []
    class Client:
        async def record_network_learning_event(self, *args):
            values.append(args[-1]); received.set()
    coordinator = ExecutionCoordinator(Client(), state_dir=tmp_path)
    record = ExecutionRecord('i', 'a', 'bwrap', 42, '/cg', 'test.scope',
                             execution.ExecutionState.RUNNING, 'lease', 1.0, True)
    client = coordinator._network_learning_client(record, 3)
    client.event_callback({'backend': 'preload', 'event_type': 'dns_query', 'domain': 'localhost',
                           'dns_qtype': 'A', 'protocol': 'udp', 'observed_at': 1.0})
    await asyncio.wait_for(received.wait(), 1)
    assert values[0]['backend'] == 'preload'
    assert values[0]['domain'] == 'localhost'


def test_learning_mount_tracks_socket_recreation_without_exposing_other_runtime_sockets(monkeypatch):
    directory = '/run/bianbu/network-learning'
    original = Path.is_dir
    monkeypatch.setattr(Path, 'is_dir', lambda p: str(p) == directory or original(p))
    decision = execution._with_runtime_worker_policy(
        _decision(), memory_uds=None, memory_registry_dir=None, bibit_intent_root=None)
    mounts = decision.policy['readonly_bind_mounts']
    assert {'host': directory, 'target': directory} in mounts
    assert not any(m['target'] == '/run/bianbu' for m in mounts)
    assert not any(m['target'].endswith('agent-network-learning.sock') for m in mounts)


def test_worker_network_diagnostics_come_from_startup_policy_not_request_env():
    policy = _decision(domains=["example.com"]).policy
    request = ExecutionRequest(agent_id=policy["agent_id"], command=("/usr/bin/python3",),
        env={"BIANBU_WORKER_NETWORK_POLICY": '{"mode":"allow_all"}'})
    policy["environment_allowlist"].append("BIANBU_WORKER_NETWORK_POLICY")
    result = execution._safe_env(policy, request)
    snapshot = json.loads(result["BIANBU_WORKER_NETWORK_POLICY"])
    assert snapshot == {"agent_id": policy["agent_id"], "mode": "whitelist", "domains": ["example.com"]}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_stage', ['construct', 'register'])
async def test_yolo_observation_exception_keeps_worker_running(tmp_path, monkeypatch, failure_stage):
    decision = _decision(cwd=str(tmp_path))
    policy = decision.policy
    policy['network'] = {'mode': 'allow_all', 'domains': [], 'observe': True}
    decision = RuntimeStartDecision(
        decision.lease_id, json.dumps(policy), decision.effective_mode,
        decision.backend, decision.sandbox_required, decision.expires_at, decision.reason)
    states = []
    class Client(_Client):
        async def report_network_learning_collector(self, *args):
            states.append(args)
            return True
    class BrokenCollector(execution.PrivilegedNetworkLearningClient):
        def __init__(self, *args, **kwargs):
            if failure_stage == 'construct':
                raise RuntimeError('collector construction failed')
            super().__init__(*args, **kwargs)
        def register(self, **kwargs):
            raise RuntimeError('collector registration failed')
    monkeypatch.setattr(execution, 'PrivilegedNetworkLearningClient', BrokenCollector)
    monkeypatch.setattr(execution, 'current_cgroup_path', lambda pid: '/worker.scope')
    backend = HostProcessBackend(popen_factory=lambda *a, **k: SimpleNamespace(
        pid=1111, poll=lambda: None, terminate=lambda: None, kill=lambda: None,
        wait=lambda timeout=None: 0))
    coordinator = ExecutionCoordinator(Client(decision), state_dir=tmp_path,
                                       backends={'host-process': backend})
    record = await coordinator.start(_request(tmp_path))
    assert record.state == 'RUNNING'
    assert record.instance_id in coordinator._handles
    assert states[-1][4] == 'degraded'


@pytest.mark.asyncio
async def test_worker_policy_drops_the_retired_control_owned_session_mounts(
    tmp_path: Path,
) -> None:
    """The Session log belongs to the control plane, not to a Worker.

    Older securityd releases still declare ``sessions/checkpoints`` (and other
    ``sessions/`` children) as writable Worker mounts for the LangGraph
    checkpointer this Runtime no longer has.  Keeping them demands that the
    deleted directory exist, which is exactly why spawning a Worker used to
    fail with ``策略挂载路径不存在``; the Worker reads the log through the
    read-only data root instead.
    """

    worker_home = tmp_path / "agent-data"
    (worker_home / "workspace").mkdir(parents=True)
    workspace_socket = tmp_path / "instances"
    workspace_socket.mkdir()
    decision = _decision(mode="sandbox", backend="bwrap", cwd=str(tmp_path))
    policy = dict(decision.policy)
    policy["readonly_bind_mounts"] = [
        {"host": str(worker_home), "target": "/home/agent"},
    ]
    policy["writable_mounts"] = [
        {"host": str(worker_home / "workspace"),
         "target": "/home/agent/workspace"},
        {"host": str(worker_home / "sessions"), "target": "/home/agent/sessions"},
        # The retired checkpointer directory from an un-upgraded securityd.
        {"host": str(worker_home / "sessions" / "checkpoints"),
         "target": "/home/agent/sessions/checkpoints"},
        {"host": str(workspace_socket), "target": "/run/bianbu-agent/instances"},
    ]
    decision = RuntimeStartDecision(
        decision.lease_id,
        json.dumps(policy),
        decision.effective_mode,
        decision.backend,
        decision.sandbox_required,
        decision.expires_at,
        decision.reason,
    )

    augmented = execution._with_runtime_worker_policy(
        decision,
        memory_uds=None,
        memory_registry_dir=None,
        bibit_intent_root=None,
    )

    session_targets = {
        str(item.get("target"))
        for name in ("readonly_bind_mounts", "writable_mounts")
        for item in augmented.policy[name]
    }
    assert not any(target.startswith("/home/agent/sessions") for target in session_targets)
    # The immutable Agent root still carries the transcript read-only.
    assert {"host": str(worker_home.resolve()), "target": "/home/agent"} in (
        augmented.policy["readonly_bind_mounts"]
    )
    assert {"host": str((worker_home / "workspace").resolve()),
            "target": "/home/agent/workspace"} in augmented.policy["writable_mounts"]
