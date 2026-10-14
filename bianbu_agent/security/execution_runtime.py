"""Runtime 的受策略约束 Agent 执行后端。

本模块是 ``bianbu-agent-runtime`` 的进程控制面。securityd 只返回不可变的
启动策略和一次性 lease；本模块负责按策略创建 host 或 bubblewrap 进程、
提交实际 PID/cgroup/namespace 证明，并在失败、撤销或 Runtime 重启时回收。

这里故意不接受任意 shell 字符串或用户追加的 bwrap 参数。生产调用方应为
每个 Agent 注册固定 entrypoint；开发态可以通过 ``allow_unregistered`` 明确
开启测试入口，但仍然使用同一套策略校验和 lease 流程。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

from .security_runtime import (
    RuntimeStartDecision,
    RuntimeStartRequest,
    SecurityRuntimeClient,
    current_cgroup_path,
)
from .network_learning_collector import NetworkLearningCollector
from .network_learning_privileged import PrivilegedNetworkLearningClient
_DEFAULT_NETWORK_LEARNING_COLLECTOR = NetworkLearningCollector
from .syscall_collector import SyscallCollector

logger = logging.getLogger(__name__)

DENY_SYSCALL_NAMES = (
    "reboot", "swapon", "swapoff", "kexec_load", "kexec_file_load",
    "init_module", "finit_module", "delete_module", "mount", "umount2",
    "pivot_root", "perf_event_open", "ptrace", "bpf", "userfaultfd",
)
NETWORK_OBSERVATION_SYSCALLS = ("connect", "sendto", "sendmsg")


def _external_syscall_collector_available() -> bool:
    return SyscallCollector().enabled


def _trace_directory(request: "ExecutionRequest") -> str:
    socket_path = str(request.env.get("BIANBU_WORKER_SOCKET", "")).strip()
    if not socket_path:
        return ""
    return str(
        Path(socket_path).expanduser().resolve().parent
        / ".syscall-traces" / request.instance
    )


def _strace_command(
    command: Sequence[str], request: "ExecutionRequest",
    decision: RuntimeStartDecision, *, optional_syscall_audit: bool = False,
) -> tuple[list[str], str]:
    network = decision.policy.get("network") or {}
    learning = str(network.get("mode", "")) == "learning" or network.get("observe") is True
    trace_dir = _trace_directory(request)
    strace_setting = os.environ.get("BIANBU_SYSCALL_STRACE", "")
    normalized_setting = str(strace_setting).strip().lower()
    # Wrapping the whole Worker in ptrace is an unsafe fallback: if strace is
    # stopped (for example by a security hook or blocked trace-file I/O), the
    # Worker remains in ``tracing stop`` and its control RPCs—including
    # cancellation—cannot make progress.  Production uses the independent
    # audit collector when available; when it is unavailable, degrade without
    # tracing.  Keep strace available only as an explicit opt-in for
    # diagnostics (BIANBU_SYSCALL_STRACE=1).
    syscall_audit = optional_syscall_audit and normalized_setting == "1"
    # Network learning has a dedicated attach-only collector.  Do not wrap the
    # Worker at launch (the ptrace parent can freeze the control channel).
    # Launch-time tracing remains available as an explicit diagnostic opt-in.
    launch_learning_trace = learning and os.environ.get(
        "BIANBU_NETWORK_LEARNING_STRACE", ""
    ).strip().lower() in {"1", "true", "yes"}
    if not launch_learning_trace and not syscall_audit:
        return list(command), trace_dir
    if str(normalized_setting or "1") in {"0", "false", "no"} and not launch_learning_trace:
        return list(command), trace_dir
    executable = shutil.which("strace")
    if not executable or not trace_dir:
        return list(command), trace_dir
    Path(trace_dir).mkdir(parents=True, exist_ok=True)
    names = set(DENY_SYSCALL_NAMES)
    if learning:
        names.update(NETWORK_OBSERVATION_SYSCALLS)
    return ([
        executable, "-ff", "-qq", "-ttt", "-xx", "-s", "512",
        "-e", "trace=" + ",".join(sorted(names)),
        "-o", str(Path(trace_dir) / ".syscall-trace"), *command,
    ], trace_dir)


def _system_service_mode() -> bool:
    """Whether the control plane is the packaged systemd service.

    A system service has no per-user systemd manager/bus. In that mode Worker
    processes are launched in per-Worker *system* transient scopes. A scope
    inherits the already-dropped ``bianbu-runtime`` identity and therefore
    cannot be turned into a root service with a ``User=`` property. It still
    provides the per-Worker cgroup/IP filter and is bound to the Runtime
    service. Development/user launches retain transient services below.
    """
    return os.environ.get("BIANBU_RUNTIME_SYSTEM_SERVICE", "") == "1"


class RuntimeExecutionError(RuntimeError):
    """执行后端无法满足 security.runtime 策略。"""


class ExecutionState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"
    REVOKED = "REVOKED"


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """一次受控启动请求。

    ``command`` 是固定 entrypoint + 参数，不是 shell 命令。生产环境由
    Runtime 注册表提供，调用者不能从 HTTP 请求直接替换。
    """

    agent_id: str
    command: tuple[str, ...]
    requested_mode: str = "host"
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | Path | None = None
    instance_id: str = ""
    target_port: int | None = None
    listen_port: int | None = None

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise RuntimeExecutionError("agent_id 不能为空")
        if not self.command or any(not str(x).strip() for x in self.command):
            raise RuntimeExecutionError("command 不能为空")
        if any("\x00" in str(x) for x in self.command):
            raise RuntimeExecutionError("command 不能包含 NUL")
        if self.cwd is not None and not str(self.cwd).strip():
            raise RuntimeExecutionError("cwd 不能为空字符串")
        for name, value in (("target_port", self.target_port), ("listen_port", self.listen_port)):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise RuntimeExecutionError(f"{name} 必须是整数端口")
            if not 0 <= value <= 65535:
                raise RuntimeExecutionError(f"{name} 超出端口范围")
        if self.target_port is not None and self.target_port == 0:
            raise RuntimeExecutionError("target_port 不能为 0")
        if self.listen_port is not None and self.target_port is None:
            raise RuntimeExecutionError("指定 listen_port 时必须同时指定 target_port")

    @property
    def instance(self) -> str:
        return self.instance_id or f"{self.agent_id}-{uuid.uuid4().hex}"


@dataclass(slots=True)
class BackendHandle:
    instance_id: str
    agent_id: str
    backend: str
    process: Any
    pid: int
    cgroup_path: str
    unit: str = ""
    started_at: float = field(default_factory=time.time)
    sandboxed: bool = False
    container_id: str = ""
    unit_scope: str = "user"
    network_properties: list[str] = field(default_factory=list)
    trace_dir: str = ""


@dataclass(slots=True)
class ExecutionRecord:
    instance_id: str
    agent_id: str
    backend: str
    pid: int
    cgroup_path: str
    unit: str
    state: str
    lease_id: str
    started_at: float
    sandboxed: bool
    container_id: str = ""
    reason: str = ""
    proxy_port: int | None = None
    unit_scope: str = "user"

    def as_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "agent_id": self.agent_id,
            "backend": self.backend,
            "pid": self.pid,
            "cgroup_path": self.cgroup_path,
            "unit": self.unit,
            "state": self.state,
            "lease_id": self.lease_id,
            "started_at": self.started_at,
            "sandboxed": self.sandboxed,
            "container_id": self.container_id,
            "reason": self.reason,
            "proxy_port": self.proxy_port,
            "unit_scope": self.unit_scope,
        }


class ExecutionBackend(Protocol):
    name: str

    def start(self, request: ExecutionRequest, decision: RuntimeStartDecision) -> BackendHandle:
        ...

    def stop(self, handle: BackendHandle, *, timeout: float) -> None:
        ...


def _safe_env(policy: Mapping[str, Any], request: ExecutionRequest) -> dict[str, str]:
    allowed = {str(x) for x in policy.get("environment_allowlist", ())}
    result = {key: str(value) for key, value in request.env.items() if key in allowed}
    # These identity values are injected by Runtime and cannot be overridden by
    # a caller-provided environment mapping.
    result["BIANBU_AGENT_ID"] = request.agent_id
    # Diagnostic context must describe this process's immutable start policy,
    # not a subsequently saved configuration awaiting an Agent restart.
    # Always overwrite caller input; this value never grants network access.
    network = policy.get("network") or {}
    result["BIANBU_WORKER_NETWORK_POLICY"] = json.dumps({
        "agent_id": str(request.env.get("BIANBU_WORKER_AGENT_ID") or request.agent_id),
        "mode": str(network.get("mode", "deny_all")),
        "domains": list(network.get("domains") or []),
    }, separators=(",", ":"))
    # The supervisor creates this per-instance credential. Keep it out of
    # user-provided HTTP environment handling while still allowing the signed
    # Security Runtime policy to explicitly permit its delivery to the Worker.
    if "BIANBU_WORKER_RPC_TOKEN" in allowed:
        token = str(request.env.get("BIANBU_WORKER_RPC_TOKEN", ""))
        if token:
            result["BIANBU_WORKER_RPC_TOKEN"] = token
    # The security client imports the securityd config module for its local
    # DLP/cache fallback. In a Worker sandbox HOME is deliberately a
    # read-only bind, so the default ``~/.local`` path would make client
    # bootstrap fail before the first UDS request. Derive this path from the
    # policy's writable Worker mount instead of accepting caller input.
    if "BIANBU_SEC_ROOT" in allowed:
        mount = _policy_mount(policy, "writable_mounts",
                              target="/home/agent/runtime/worker")
        if mount is not None:
            result["BIANBU_SEC_ROOT"] = str(mount[0] / "securityd-local")
    user_skills = _policy_mount(policy, "readonly_bind_mounts",
                                target="/run/bianbu-agent/user-skills")
    if user_skills is not None:
        result["BIANBU_USER_SKILLS_DIR"] = str(user_skills[0])
    return result


_MEMORY_ENV_KEYS = (
    "BIANBU_AGENT_MEMORY_ENABLED",
    "BIANBU_AGENT_MEMORY_SOCKET",
    "BIANBU_AGENT_MEMORY_REGISTRY_DIR",
    "BIANBU_AGENT_MEMORY_ACCOUNT_ID",
    "BIANBU_AGENT_MEMORY_AGENT_NAME",
    "BIANBU_AGENT_MEMORY_REGISTRATION",
)
# CPUQuota is only a ceiling: a Worker scope that asks for no CPUWeight
# inherits the default 100 and loses the scheduler to any saturating task on
# the same board.  That starvation made Hook children run 8-20x slow and
# tripped gate Hook timeouts (RUNTIME-HOOK-001), so Runtime applies a floor
# whenever security policy does not carry its own ``cpu_weight``.  Weight
# only redistributes contended CPU; an idle board is unaffected.
_DEFAULT_WORKER_CPU_WEIGHT = 1000
_WORKER_IDENTITY_ENV_KEYS = (
    "BIANBU_WORKER_AGENT_ID",
    "BIANBU_WORKER_SECURITY_AGENT_ID",
    "COM_BIANBU_AGENT_ID",
    "BIANBU_SECURITY_SUBJECT",
)


def _append_mount(
    mounts: list[Any],
    *,
    host: str | Path | None,
    target: str,
) -> None:
    if not host:
        return
    resolved = str(Path(host).expanduser().resolve())
    candidate = {"host": resolved, "target": target}
    if candidate not in mounts:
        mounts.append(candidate)


def _runtime_source_root(entrypoint: tuple[str, ...]) -> Path | None:
    """Recognize a signed Worker entrypoint from a source checkout venv."""
    if not entrypoint:
        return None
    executable = Path(entrypoint[0]).expanduser()
    if not executable.is_absolute():
        return None
    for parent in executable.parents:
        if parent.name != ".venv":
            continue
        source_root = parent.parent
        if parent.is_dir() and (source_root / "bianbu_agent").is_dir():
            return source_root.resolve()
        return None
    return None


def _with_runtime_worker_policy(
    decision: RuntimeStartDecision,
    *,
    memory_uds: str | Path | None,
    memory_registry_dir: str | Path | None,
    bibit_intent_root: str | Path | None,
    runtime_source_root: str | Path | None = None,
    python_runtime_root: str | Path | None = None,
    python_runtime_target: str | Path | None = None,
    user_skills_root: str | Path | None = None,
) -> RuntimeStartDecision:
    """Add Runtime-managed Worker identity and Memory policy fields.

    Older securityd releases accept ``memory_uds`` and ``memory_registry_dir`` in
    PrepareStart but do not echo them into the sandbox policy. The Runtime is
    still the trusted path owner and validates every added mount before spawn.
    """

    policy = dict(decision.policy)
    readonly = list(policy.get("readonly_bind_mounts") or [])
    writable = list(policy.get("writable_mounts") or [])
    _append_mount(
        readonly, host=user_skills_root,
        target="/run/bianbu-agent/user-skills",
    )
    # Derive the system profile solely from the already-approved Worker home,
    # never from a caller-provided path or environment variable. The Worker
    # resolves the same profile under its remapped /home/agent data root.
    from bianbu_agent.agents.system_paths import system_agent_directory

    def _session_mount(item: object, session_root: Path) -> bool:
        # The control plane owns the Session log, and a Worker only ever reads
        # it through the read-only root bind.  Older securityd releases still
        # declare ``sessions/checkpoints`` as a writable mount for the
        # LangGraph checkpointer this Runtime no longer has; keeping it would
        # both resurrect a deleted directory and demand write access to
        # control-plane state.
        if not isinstance(item, Mapping):
            return False
        host = Path(str(item.get("host", ""))).expanduser().resolve()
        return host == session_root or host.is_relative_to(session_root)

    for mount in tuple(readonly):
        if not isinstance(mount, Mapping) or mount.get("target") != "/home/agent":
            continue
        worker_home = Path(str(mount["host"])).resolve()
        session_root = worker_home / "sessions"
        for mounts in (readonly, writable):
            mounts[:] = [
                item for item in mounts if not _session_mount(item, session_root)
            ]
        operational_logs = worker_home / "logs" / "operational"
        if operational_logs.resolve() != operational_logs:
            raise RuntimeExecutionError("operational log directory must not traverse symlinks")
        if operational_logs.is_dir():
            _append_mount(
                writable, host=operational_logs,
                target="/home/agent/logs/operational",
            )
        system_agents = system_agent_directory(worker_home)
        if system_agents.is_dir():
            _append_mount(
                readonly, host=system_agents,
                target=str(system_agent_directory(Path("/home/agent"))),
            )
    _append_mount(
        readonly,
        host=memory_uds,
        target="/run/bianbu/agent-memoryd.sock",
    )
    # Bind the dedicated directory, not the socket inode: collector restart
    # recreates the socket and must remain visible to existing Workers.
    learning_directory = Path("/run/bianbu/network-learning")
    if learning_directory.is_dir():
        _append_mount(
            readonly,
            host=learning_directory,
            target=str(learning_directory),
        )
    _append_mount(
        readonly,
        host=bibit_intent_root,
        target="/opt/bibit-intent",
    )
    if runtime_source_root:
        source_root = Path(runtime_source_root).expanduser().resolve()
        for relative in (".venv", "bianbu_agent", "resources"):
            source_path = source_root / relative
            _append_mount(readonly, host=source_path, target=str(source_path))
    if python_runtime_root:
        _append_mount(
            readonly, host=python_runtime_root,
            target=str(python_runtime_target or Path(python_runtime_root).resolve()),
        )
    _append_mount(
        writable,
        host=memory_registry_dir,
        target="/run/bianbu/agent-runtime/memory-registry",
    )
    allowlist = list(policy.get("environment_allowlist") or [])
    for key in (*_WORKER_IDENTITY_ENV_KEYS, *_MEMORY_ENV_KEYS, "BIANBU_SYSTEM_RESOURCE_ROOT"):
        if key not in allowlist:
            allowlist.append(key)
    policy["readonly_bind_mounts"] = readonly
    policy["writable_mounts"] = writable
    policy["environment_allowlist"] = allowlist
    return RuntimeStartDecision(
        decision.lease_id,
        json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        decision.effective_mode,
        decision.backend,
        decision.sandbox_required,
        decision.expires_at,
        decision.reason,
    )


def _policy_mount(policy: Mapping[str, Any], name: str, *, target: str) -> tuple[Path, str] | None:
    """Return one immutable policy mount, rejecting ambiguous duplicates."""
    found: tuple[Path, str] | None = None
    for item in policy.get(name) or ():
        if not isinstance(item, Mapping):
            continue
        selected_target = str(item.get("target", "")).strip()
        if selected_target != target:
            continue
        host = Path(str(item.get("host", ""))).expanduser().resolve()
        if found is not None and found[0] != host:
            raise RuntimeExecutionError(f"策略重复声明挂载目标：{target}")
        found = (host, selected_target)
    return found


def _policy_mount_covering(policy: Mapping[str, Any], name: str, path: Path) -> tuple[Path, str] | None:
    candidates: list[tuple[int, Path, str]] = []
    selected = path.resolve()
    for item in policy.get(name) or ():
        if not isinstance(item, Mapping):
            continue
        target = str(item.get("target", "")).strip()
        if not target.startswith("/"):
            continue
        target_path = Path(target).resolve()
        try:
            selected.relative_to(target_path)
        except ValueError:
            continue
        host = Path(str(item.get("host", ""))).expanduser().resolve()
        candidates.append((len(target_path.parts), host, target))
    if not candidates:
        return None
    _, host, target = max(candidates, key=lambda item: item[0])
    return host, target


def _host_to_target(policy: Mapping[str, Any], value: str) -> str:
    """Map a host path into the immutable sandbox mount namespace."""
    # Keep the lexical path first.  Virtualenv entrypoints are commonly
    # symlinks (for example ``.venv/bin/python``); resolving before matching
    # would turn a path inside a mounted bundle into ``/usr/bin/python`` and
    # make the namespace-visible entrypoint disappear.
    selected = Path(value).expanduser()
    # argv flags, module names and other relative values are not filesystem
    # paths.  Resolving them against a cwd inside the Runtime checkout would
    # incorrectly rewrite ``-m`` as ``/opt/bianbu-securityd/-m``.
    if not selected.is_absolute():
        return value
    resolved = selected.resolve()
    candidates: list[tuple[bool, int, Path, str]] = []
    for item in [*(policy.get("writable_mounts") or ()),
                 *(policy.get("readonly_bind_mounts") or ())]:
        if not isinstance(item, Mapping):
            continue
        host = Path(str(item.get("host", ""))).expanduser().resolve()
        target = str(item.get("target", "")).strip()
        if not target.startswith("/"):
            continue
        try:
            relative = selected.relative_to(host)
            lexical_match = True
        except ValueError:
            try:
                relative = resolved.relative_to(host)
                lexical_match = False
            except ValueError:
                continue
        candidates.append((lexical_match, len(host.parts), relative, target))
    if not candidates:
        return value
    _, _, relative, target = max(candidates, key=lambda item: (item[0], item[1]))
    return str(Path(target) / relative)


def _safe_cwd(policy: Mapping[str, Any], request: ExecutionRequest) -> Path:
    declared = (policy.get("host_constraints") or {}).get("working_directory")
    selected = Path(request.cwd or declared or os.getcwd()).expanduser().resolve()
    if not selected.is_dir():
        raise RuntimeExecutionError(f"工作目录不存在：{selected}")
    if declared:
        allowed = Path(str(declared)).expanduser().resolve()
        try:
            selected.relative_to(allowed)
        except ValueError as exc:
            raise RuntimeExecutionError("cwd 超出策略声明的 working_directory") from exc
    return selected


def _normalise_cgroup(value: str) -> str:
    selected = str(value).strip()
    if selected.startswith("0::"):
        selected = selected[3:]
    if selected.startswith("/sys/fs/cgroup"):
        selected = selected[len("/sys/fs/cgroup"):]
    if not selected.startswith("/"):
        selected = "/" + selected
    return selected.rstrip("/") or "/"


def _unit_name(agent_id: str, instance_id: str, *, unit_type: str | None = None) -> str:
    """Return the private unit name used for one Worker.

    System-service Runtime Workers are deliberately placed in transient
    *scopes*.  A scope contains the caller's already-dropped credentials and
    cannot carry a ``User=``/``Group=`` property, so a compromised Worker
    cannot ask systemd to create a root service.  User-manager development
    launches keep the historical transient service form.
    """
    safe = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in agent_id)[:40]
    selected = unit_type or ("scope" if _system_service_mode() else "service")
    if selected not in {"service", "scope"}:
        raise RuntimeExecutionError(f"非法 Worker unit 类型：{selected}")
    return f"bianbu-agent-{safe}-{instance_id[-12:]}.{selected}"


# ``securityd`` publishes the Worker sandbox policy with a 2 GiB ``MemoryMax``
# and a one-core ``CPUQuota`` by default (FR-SBX-02).  A single-board
# deployment has no other tenant to protect, and both caps only reflect those
# policy defaults: the memory cap OOM kills long Sessions, and the one-core
# quota makes the Worker's Hook children, engine build and tool calls take
# turns on a single core.  The Runtime therefore accepts an explicit override
# per limit instead of editing the Security side.  Unset means "keep enforcing
# the policy value"; the development launchers and the packaged system unit
# both default to ``off``, so only an explicitly configured value
# (``BIANBU_WORKER_MEMORY_MAX=2G``, ``BIANBU_WORKER_CPU_QUOTA=200%``) brings a
# cap back.
_NO_LIMIT = object()
_LIMIT_OFF = frozenset({"0", "off", "no", "none", "unlimited", "false"})

#: policy key -> systemd property -> override environment variable (or None)
_WORKER_LIMIT_PROPERTIES = (
    ("memory_max", "MemoryMax", "BIANBU_WORKER_MEMORY_MAX"),
    ("cpu_quota", "CPUQuota", "BIANBU_WORKER_CPU_QUOTA"),
    ("cpu_weight", "CPUWeight", None),
    ("tasks_max", "TasksMax", None),
    ("io_weight", "IOWeight", None),
)


def _limit_override(env_name: str) -> str | object | None:
    """Return a resource-limit override: the value, off sentinel, or None."""

    raw = str(os.environ.get(env_name, "")).strip()
    if not raw:
        return None
    if raw.casefold() in _LIMIT_OFF:
        return _NO_LIMIT
    return raw


def _resource_properties(policy: Mapping[str, Any]) -> list[str]:
    resources = policy.get("resources") or {}
    props: list[str] = []
    for key, systemd_key, env_name in _WORKER_LIMIT_PROPERTIES:
        value = resources.get(key)
        if env_name is not None:
            override = _limit_override(env_name)
            if override is _NO_LIMIT:
                continue
            value = override or value
        if value not in (None, ""):
            props.append(f"{systemd_key}={value}")
    if not any(prop.startswith("CPUWeight=") for prop in props):
        props.append(f"CPUWeight={_DEFAULT_WORKER_CPU_WEIGHT}")
    return props


def _network_properties(policy: Mapping[str, Any]) -> list[str]:
    network = policy.get("network") or {}
    mode = str(network.get("mode", "deny_all"))
    if mode not in {"whitelist", "deny_all", "allow_all", "learning"}:
        raise RuntimeExecutionError(f"不支持的网络策略模式：{mode}")
    if mode in {"allow_all", "learning"}:
        return []
    domains = [str(value).strip() for value in network.get("domains") or () if str(value).strip()]
    if mode == "deny_all" or not domains:
        return ["IPAddressDeny=any"]
    addresses: set[str] = set()
    resolv_conf = _resolved_resolv_conf()
    if resolv_conf is not None:
        try:
            for line in resolv_conf.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if len(fields) < 2 or fields[0] != "nameserver":
                    continue
                address = fields[1].split("%", 1)[0]
                try:
                    addresses.add(str(ipaddress.ip_address(address)))
                except ValueError:
                    continue
        except OSError as exc:
            raise RuntimeExecutionError("无法读取宿主 DNS resolver 配置") from exc
    for domain in domains:
        try:
            for item in socket.getaddrinfo(domain, None, type=socket.SOCK_STREAM):
                addresses.add(str(item[4][0]))
        except (OSError, UnicodeError) as exc:
            raise RuntimeExecutionError(f"网络白名单域名解析失败：{domain}") from exc
    if not addresses:
        raise RuntimeExecutionError("网络白名单解析为空，拒绝启动")
    return ["IPAddressDeny=any", *[f"IPAddressAllow={address}" for address in sorted(addresses)]]


def _resolved_resolv_conf() -> Path | None:
    """返回联网 sandbox 需要映射的实际 resolver 文件。

    systemd-resolved 主机通常让 ``/etc/resolv.conf`` 指向 ``/run``。bwrap
    只读绑定 ``/etc`` 后，该链接在 sandbox 中会悬空，因此必须按相同绝对路径
    额外绑定链接目标。普通文件布局仍返回 ``/etc/resolv.conf``，供 cgroup 网络
    白名单提取 DNS server，但不需要重复挂载。
    """
    path = Path("/etc/resolv.conf")
    try:
        target = path.resolve(strict=True)
    except OSError:
        return None
    return target if target.is_file() else None


def _resource_command(
    policy: Mapping[str, Any], command: Sequence[str], unit: str, *,
    standard_input_path: str | Path | None = None,
) -> tuple[list[str], str]:
    """构造 transient unit 命令；资源强制能力不可用时拒绝启动。"""
    props = [*_resource_properties(policy), *_network_properties(policy)]
    constraints = policy.get("host_constraints") or {}
    network = policy.get("network") or {}
    domains = [str(value).strip() for value in network.get("domains") or () if str(value).strip()]
    # Host mode still needs a filesystem boundary.  ``systemd-run`` creates a
    # private mount namespace for ProtectSystem/ProtectHome and applies the
    # explicit ReadWritePaths/InaccessiblePaths allowlist.  This is the
    # supported equivalent on K3 when Landlock is unavailable; a plain Popen
    # must never be presented as a commercial host backend.
    file_boundary_required = bool(constraints.get("file_boundary_required", True))
    if file_boundary_required:
        props += [
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            "PrivateDevices=yes",
            "RestrictSUIDSGID=yes",
            "LockPersonality=yes",
            "MemoryDenyWriteExecute=yes",
        ]
        for item in policy.get("writable_mounts") or ():
            if isinstance(item, Mapping) and str(item.get("host", "")).strip():
                props.append(f"ReadWritePaths={Path(str(item['host'])).expanduser().resolve()}")
        for item in policy.get("readonly_bind_mounts") or ():
            if isinstance(item, Mapping) and str(item.get("host", "")).strip():
                props.append(f"ReadOnlyPaths={Path(str(item['host'])).expanduser().resolve()}")
        for item in policy.get("deny_paths") or ():
            value = str(item).strip()
            if value.startswith("/"):
                props.append(f"InaccessiblePaths={value}")
    requires_unit = bool(props) or bool(constraints.get("resource_cgroup_required"))
    if not requires_unit:
        return list(command), ""
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        raise RuntimeExecutionError("策略要求 cgroup 资源限制，但未找到 systemd-run")
    system_scope = _system_service_mode()
    # Every Worker in the packaged system service uses a transient *scope*.
    # A scope inherits the already-dropped Runtime identity and mount namespace
    # and cannot carry User=/Group= properties.  Using a service for Host
    # Workers would let a compromised Worker request the same unit name with
    # User=root through the polkit rule, so Host and Sandbox deliberately share
    # this scope-only boundary.
    scope_unit = system_scope
    argv = [systemd_run, "--system" if system_scope else "--user",
            "--quiet", "--collect", "--unit", unit]
    if scope_unit:
        parent_unit = os.environ.get(
            "BIANBU_RUNTIME_SYSTEM_UNIT",
            "bianbu-agent-runtime.service",
        ).strip()
        if not re.fullmatch(
            r"bianbu-agent-runtime(?:@[A-Za-z_][A-Za-z0-9_.-]*)?\.service",
            parent_unit,
        ):
            raise RuntimeExecutionError("无效的 Runtime system unit")
        # A transient scope inherits the per-user Runtime identity and the
        # parent service's mount namespace.  Unlike a transient service it
        # has no User=/Group= properties, eliminating the privilege-escalation
        # footgun where an untrusted caller could request User=root.  The
        # parent service supplies the static filesystem boundary; this scope
        # adds only per-Worker cgroup/network properties.
        argv += [
            "--scope",
            "--property", f"BindsTo={parent_unit}",
            "--property", f"After={parent_unit}",
        ]
        # A scope runs the command as a direct child of systemd-run and
        # inherits its stdin.  _launch_systemd supplies the seccomp file as
        # fd 0; --pipe is both unnecessary and rejected together with
        # --scope by systemd.
        # Service-only filesystem properties cannot be attached to a scope.
        # They are enforced by the packaged parent unit and inherited by the
        # scope process (see debian/bianbu-agent-runtime.service).
        props = [prop for prop in props if not prop.startswith((
            "ProtectSystem=", "ProtectHome=", "PrivateDevices=",
            "RestrictSUIDSGID=", "LockPersonality=", "MemoryDenyWriteExecute=",
            "ReadWritePaths=", "ReadOnlyPaths=", "InaccessiblePaths=",
        ))]
    for prop in props:
        argv += ["--property", prop]
    if not scope_unit:
        argv += ["--property", "NoNewPrivileges=yes"]
        if bool(constraints.get("private_tmp_required", True)):
            argv += ["--property", "PrivateTmp=yes"]
        if standard_input_path is not None:
            # Let the service manager open the immutable BPF file as fd 0 so
            # ExecStart remains /usr/bin/bwrap.  Avoiding a /bin/sh wrapper is
            # required for executable-path AppArmor profiles to take effect.
            argv += ["--property", f"StandardInput=file:{Path(standard_input_path)}"]
    argv += ["--", *command]
    return argv, unit


def _network_properties_from_launch(launch: Sequence[str]) -> list[str]:
    return [str(launch[i + 1]) for i, value in enumerate(launch[:-1])
            if value == "--property" and str(launch[i + 1]).startswith(("IPAddressAllow=", "IPAddressDeny="))]


def _terminate_process(process: subprocess.Popen[Any], *, timeout: float) -> None:
    if hasattr(process, "unit") and getattr(process, "unit"):
        _stop_systemd_unit(str(process.unit), timeout=timeout,
                           scope=str(getattr(process, "scope", "user")))
        try:
            process.wait(timeout=max(0.1, timeout))
        except (AttributeError, subprocess.TimeoutExpired, OSError):
            pass
        return
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except (ProcessLookupError, OSError):
            return
    try:
        process.wait(timeout=max(0.1, timeout))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            process.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            pass


def _network_properties_from_launch(launch: Sequence[str]) -> list[str]:
    return [str(launch[i + 1]) for i, value in enumerate(launch[:-1])
            if value == "--property" and str(launch[i + 1]).startswith(("IPAddressAllow=", "IPAddressDeny="))]


@dataclass(slots=True)
class _UnitProcess:
    """最小 systemd unit 句柄，避免把已退出的 systemd-run CLI 当作 Agent PID。"""

    pid: int
    unit: str
    scope: str = "user"
    launcher: subprocess.Popen[Any] | None = None

    def poll(self) -> int | None:
        # For a transient scope this PID is the Worker itself: systemd-run
        # enters the scope and execs the command in-place.
        try:
            os.kill(self.pid, 0)
        except OSError:
            return 1
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self.launcher is not None:
            # The scope launcher is the actual Worker process. Waiting here is
            # only used during orderly shutdown after systemd has stopped the
            # scope, so tolerate an already-reaped launcher.
            try:
                return self.launcher.wait(timeout=timeout)
            except ChildProcessError:
                return 0
        return 0


def _systemctl(scope: str, *args: str) -> list[str]:
    return ["systemctl", "--user", *args] if scope == "user" else ["systemctl", *args]


def _systemd_client_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the minimal environment needed to contact the user manager.

    The control plane may itself run in a transient systemd unit.  Inheriting
    that unit's MANAGERPID/SYSTEMD_EXEC_PID/session metadata makes newer
    systemd clients select a local transport which is not mounted inside the
    outer bwrap namespace.  The persistent user bus is the only transport the
    Runtime needs for creating and managing Worker units.
    """
    current = os.environ if source is None else source
    exact = {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE",
        "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    }
    return {
        str(key): str(value)
        for key, value in current.items()
        if key in exact or key.startswith("LC_")
    }


def _unit_main_pid(unit: str, *, scope: str = "user") -> int:
    try:
        output = subprocess.run(
            _systemctl(scope, "show", unit, "--property=MainPID", "--value"),
            check=True, capture_output=True, text=True, timeout=5,
            env=_systemd_client_env() if scope == "user" else None,
        ).stdout.strip()
        return int(output or 0)
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0


def _unit_control_group(unit: str, *, scope: str = "user") -> str:
    try:
        return subprocess.run(
            _systemctl(scope, "show", unit, "--property=ControlGroup", "--value"),
            check=True, capture_output=True, text=True, timeout=5,
            env=_systemd_client_env() if scope == "user" else None,
        ).stdout.strip()
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _unit_owns_pid(unit: str, pid: int, *, scope: str = "user") -> bool:
    """Verify that ``pid`` is still inside the recorded transient unit.

    Services expose ``MainPID``.  Scopes intentionally do not have one (all
    processes are equivalent), so compare the kernel-reported cgroup path
    with the unit's ``ControlGroup`` instead.  This prevents PID-reuse cleanup
    from signalling an unrelated process after a Runtime restart.
    """
    try:
        if scope != "system":
            return _unit_main_pid(unit, scope=scope) == int(pid)
        output = subprocess.run(
            _systemctl(scope, "show", unit, "--property=ControlGroup", "--value"),
            check=True, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if not output:
            return False
        return _normalise_cgroup(output) == _normalise_cgroup(current_cgroup_path(int(pid)))
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return False


def _stop_systemd_unit(unit: str, *, timeout: float, scope: str = "user") -> None:
    try:
        subprocess.run(_systemctl(scope, "stop", unit), check=False,
                       capture_output=True, timeout=max(1.0, timeout),
                       env=_systemd_client_env() if scope == "user" else None)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _terminate_pid(pid: int, *, unit: str = "", scope: str = "user",
                   timeout: float = 3.0) -> None:
    """Stop a ledger-owned process after a coordinator restart.

    A restarted coordinator no longer has the original ``Popen`` handle.  A
    transient systemd unit remains the preferred kill boundary; otherwise the
    recorded PID is signalled directly and verified before escalating.
    """
    if unit:
        _stop_systemd_unit(unit, timeout=timeout, scope=scope)
        return
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.05)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class _LoopbackProxy:
    """A deliberately small TCP proxy bound only to the local loopback."""

    def __init__(self, target_port: int, listen_port: int | None = None):
        if not 1 <= int(target_port) <= 65535:
            raise RuntimeExecutionError("target_port 超出端口范围")
        selected = 0 if listen_port is None else int(listen_port)
        if not 0 <= selected <= 65535:
            raise RuntimeExecutionError("listen_port 超出端口范围")
        target = ("127.0.0.1", int(target_port))

        class _Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = False
            daemon_threads = True

        proxy = self

        class _Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    upstream = socket.create_connection(target, timeout=3.0)
                except OSError:
                    return
                with upstream:
                    self.request.settimeout(30.0)
                    upstream.settimeout(30.0)
                    sockets = [self.request, upstream]
                    while True:
                        try:
                            readable, _, exceptional = select.select(sockets, [], sockets, 30.0)
                        except (OSError, ValueError):
                            return
                        if exceptional or not readable:
                            return
                        for source in readable:
                            try:
                                data = source.recv(64 * 1024)
                            except OSError:
                                return
                            if not data:
                                return
                            destination = upstream if source is self.request else self.request
                            try:
                                destination.sendall(data)
                            except OSError:
                                return

        self._server = _Server(("127.0.0.1", selected), _Handler)
        self._server.daemon_threads = True
        self.target = target
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"bianbu-runtime-proxy-{self.port}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1.0)


def _launch_systemd(command: Sequence[str], *, unit: str, cwd: Path,
                    env: Mapping[str, str],
                    standard_input_path: str | Path | None = None) -> _UnitProcess:
    setenv: list[str] = []
    for key, value in env.items():
        if "\x00" in str(key) or "\x00" in str(value) or "=" in str(key):
            raise RuntimeExecutionError("环境变量名称或值包含非法 NUL")
        setenv += ["--setenv", f"{key}={value}"]
    if command and Path(str(command[0])).name == "systemd-run":
        # _resource_command already compiled the transient-unit properties.
        # Add only the immutable cwd/environment before the command separator.
        argv = list(command)
        try:
            separator = argv.index("--")
        except ValueError as exc:
            raise RuntimeExecutionError("systemd-run 命令缺少 -- 分隔符") from exc
        if "--scope" in argv:
            # WorkingDirectory= is a service property and is rejected for
            # scopes. Use systemd-run's native option instead.
            argv[separator:separator] = ["--working-directory", str(cwd), *setenv]
        else:
            argv[separator:separator] = ["--property", "WorkingDirectory=" + str(cwd), *setenv]
    else:
        argv = ["systemd-run", "--user", "--quiet", "--collect", "--unit", unit,
                "--property", "WorkingDirectory=" + str(cwd), *setenv, "--", *command]
    scope = "system" if "--system" in argv else "user"
    if "--scope" in argv:
        # systemd-run --scope keeps the command attached to the caller and
        # therefore blocks until the Worker exits. Run it asynchronously and
        # return the actual Worker PID; cgroup membership is attested from
        # /proc rather than trusting a detached service MainPID.
        try:
            stdin_file = (
                open(standard_input_path, "rb")  # noqa: SIM115 - closed after spawn
                if standard_input_path is not None else None
            )
            child = subprocess.Popen(
                argv, stdin=stdin_file if stdin_file is not None else subprocess.DEVNULL,
                # A scope process is the long-lived Worker itself. Do not
                # attach pipes that could fill and deadlock a chat Worker.
                stdout=None,
                stderr=None,
                start_new_session=True, env=_systemd_client_env(),
            )
            if stdin_file is not None:
                stdin_file.close()
        except (OSError, ValueError) as exc:
            raise RuntimeExecutionError(f"systemd scope 启动失败：{exc}") from exc
        for _ in range(40):
            if child.poll() is not None:
                detail = ""
                if child.stderr is not None:
                    try:
                        detail = child.stderr.read() or ""
                    except Exception:
                        pass
                raise RuntimeExecutionError(
                    f"systemd scope 提前退出（exit={child.returncode}）{detail.strip()}"
                )
            try:
                cgroup = current_cgroup_path(child.pid)
            except (OSError, RuntimeError):
                cgroup = ""
            expected_cgroup = _unit_control_group(unit, scope=scope)
            if cgroup and expected_cgroup and _normalise_cgroup(cgroup) == _normalise_cgroup(expected_cgroup):
                return _UnitProcess(child.pid, unit, scope, child)
            time.sleep(0.05)
        _stop_systemd_unit(unit, timeout=1, scope=scope)
        try:
            child.kill()
        except OSError:
            pass
        raise RuntimeExecutionError(f"systemd scope 未返回有效 cgroup：{unit}")
    try:
        subprocess.run(
            argv, check=True, capture_output=True, text=True, timeout=10,
            env=_systemd_client_env(),
        )
    except subprocess.CalledProcessError as exc:
        detail = str(exc.stderr or exc.stdout or "").strip()
        suffix = f"：{detail}" if detail else ""
        raise RuntimeExecutionError(
            f"systemd unit 启动失败（exit={exc.returncode}）{suffix}"
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeExecutionError(f"systemd unit 启动失败：{exc}") from exc
    for _ in range(40):
        pid = _unit_main_pid(unit, scope=scope)
        if pid > 0:
            return _UnitProcess(pid, unit, scope)
        time.sleep(0.05)
    _stop_systemd_unit(unit, timeout=1, scope=scope)
    raise RuntimeExecutionError(f"systemd unit 未返回 MainPID：{unit}")


class HostProcessBackend:
    """宿主机模式后端。

    即使不创建 namespace，也通过 transient systemd unit 应用 cgroup、
    ``NoNewPrivileges`` 和 ``PrivateTmp``。没有 systemd-run 时按策略拒绝，
    不把“普通 Popen”伪装成商用 host 模式。
    """

    name = "host-process"

    def __init__(self, *, popen_factory=subprocess.Popen, require_resources: bool = True):
        self._popen = popen_factory
        self.require_resources = require_resources

    def start(self, request: ExecutionRequest, decision: RuntimeStartDecision) -> BackendHandle:
        if decision.effective_mode != "host" or decision.backend != self.name:
            raise RuntimeExecutionError("HostProcessBackend 与策略 effective_mode/backend 不一致")
        policy = decision.policy
        constraints = policy.get("host_constraints") or {}
        if constraints.get("system_dbus_allowed", False):
            raise RuntimeExecutionError("host 模式不得开放完整 system D-Bus")
        cwd = _safe_cwd(policy, request)
        command = list(request.command)
        command, _trace_dir = _strace_command(command, request, decision)
        unit = _unit_name(
            request.agent_id, request.instance,
            unit_type=("scope" if _system_service_mode() else "service"),
        )
        launch, unit = _resource_command(policy, command, unit)
        if not launch:
            raise RuntimeExecutionError("host entrypoint 为空")
        env = _safe_env(policy, request)
        # Derive the desktop-launch marker from the validated backend decision.
        env["BIANBU_SANDBOX_ACTIVE"] = "0"
        # 不继承宿主凭据、代理和 PYTHONPATH；运行时变量由调用方显式声明。
        clean_env = {key: value for key, value in env.items() if key not in {
            "LD_LIBRARY_PATH", "PYTHONPATH", "HTTP_PROXY", "HTTPS_PROXY",
            "ALL_PROXY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        }}
        preload = str(request.env.get("LD_PRELOAD", ""))
        if preload.endswith("/libbianbu-network-learning.so") and Path(preload).is_file():
            clean_env.update({"LD_PRELOAD": preload,
                              "BIANBU_NETWORK_LEARNING_SOCKET": "/run/bianbu/network-learning/collector.sock",
                              "BIANBU_NETWORK_LEARNING_INSTANCE_ID": request.instance})
        if _system_service_mode():
            # Direct-child mode cannot rely on systemd-run to supply the
            # service manager's baseline environment. Preserve only harmless
            # process basics (never arbitrary credentials) and fill safe
            # defaults for an unusually minimal systemd environment.
            baseline = {
                key: str(value) for key, value in os.environ.items()
                if key in {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LANGUAGE"}
                or key.startswith("LC_")
            }
            baseline.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
            baseline.setdefault("HOME", str(cwd.parent))
            baseline.setdefault("USER", "bianbu-runtime")
            baseline.setdefault("LOGNAME", baseline["USER"])
            baseline.update(clean_env)
            clean_env = baseline
        # Host Workers use the Runtime HOME, but keep their mutable lock
        # files in the same worker-owned data root as sandbox Workers.  This
        # avoids touching the immutable/default profile root and makes both
        # backends obey the same filesystem contract.
        lock_root = cwd / "runtime" / "worker"
        lock_root.mkdir(parents=True, exist_ok=True)
        clean_env.update({
            "BIANBU_CAPABILITY_LOCK_PATH": str(lock_root / ".capability.lock"),
            "BIANBU_MODEL_LOCK_PATH": str(lock_root / "model-locks"),
        })
        # host-process Worker 不能装载 seccomp：内核只允许在 no_new_privs 置位
        # 或持有 CAP_SYS_ADMIN 的进程装载过滤器，而 no_new_privs 不可撤销，一旦
        # 置位 host Worker 就永久失去 sudo/pkexec/su 等 setuid 提权能力（用户可见
        # 回归）。主机进程以登录用户身份运行并要求保留 PAM/sudoers 语义，因此
        # 这里不再向 host Worker 下发 BIANBU_SECCOMP_AUDIT_FILTER；策略 seccomp
        # denylist 只在 bwrap 沙箱内生效（bwrap --seccomp 装载后 Worker 叠加
        # LOG）。host 实例的内核审计覆盖在 attestation 里如实报告为 false，
        # host 侧的强制点由 securityd 的逐次调用判权与 polkit 授权承担。
        # systemd-run 创建真实 transient unit；其 CLI 进程很快退出，不能把
        # CLI PID 当成 Agent PID，因此使用 systemctl MainPID 做 attestation。
        if self._popen is subprocess.Popen:
            process = _launch_systemd(launch, unit=unit, cwd=cwd, env=clean_env)
        else:
            try:
                # In system-service mode the Runtime unit owns the journal.
                # Never leave an unread PIPE attached to a long-lived Worker:
                # a verbose Worker could fill the pipe and deadlock while the
                # control plane is waiting for its RPC socket.
                stdout = None if _system_service_mode() else subprocess.PIPE
                stderr = None if _system_service_mode() else subprocess.PIPE
                process = self._popen(
                    launch, cwd=str(cwd), env=clean_env,
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr,
                )
            except (OSError, ValueError) as exc:
                raise RuntimeExecutionError(f"host 进程启动失败：{exc}") from exc
        pid = int(process.pid)
        try:
            cgroup = current_cgroup_path(pid)
        except (OSError, RuntimeError) as exc:
            _terminate_process(process, timeout=1)
            raise RuntimeExecutionError("host 进程没有可证明的 cgroup") from exc
        return BackendHandle(request.instance, request.agent_id, self.name, process, pid,
                             cgroup, unit=unit, sandboxed=False,
                             unit_scope=("system" if unit and _system_service_mode() else "user"),
                             network_properties=_network_properties_from_launch(launch),
                             trace_dir=_trace_dir)

    def stop(self, handle: BackendHandle, *, timeout: float) -> None:
        _terminate_process(handle.process, timeout=timeout)


class BwrapBackend:
    """基于不可变 security.runtime policy 的 bubblewrap 后端。"""

    name = "bwrap"

    def __init__(self, *, bwrap_path: str | None = None, popen_factory=subprocess.Popen):
        self.bwrap_path = bwrap_path or shutil.which("bwrap")
        self._popen = popen_factory

    @staticmethod
    def _mount_args(policy: Mapping[str, Any]) -> list[str]:
        args: list[str] = []
        for item in policy.get("readonly_mounts") or ():
            path = str(item)
            if Path(path).exists():
                args += ["--ro-bind", path, path]
        # The immutable Agent root must be bound before writable state
        # overlays. Otherwise a later read-only bind could hide the state
        # directories or a writable root could make signed code mutable.
        for item in policy.get("readonly_bind_mounts") or ():
            if not isinstance(item, Mapping):
                raise RuntimeExecutionError("readonly_bind_mounts 项必须是对象")
            host = Path(str(item.get("host", ""))).expanduser().resolve()
            target = str(item.get("target", "")).strip()
            if not host.exists() or not target.startswith("/") or ".." in Path(target).parts:
                raise RuntimeExecutionError("readonly_bind_mounts 路径非法或不存在")
            args += ["--dir", str(Path(target).parent)] if Path(target).parent != Path("/") else []
            args += ["--ro-bind", str(host), target]
        for item in policy.get("writable_mounts") or ():
            if not isinstance(item, Mapping):
                raise RuntimeExecutionError("writable_mounts 项必须是对象")
            host = Path(str(item.get("host", ""))).expanduser().resolve()
            target = str(item.get("target", "")).strip()
            if not host.exists() or not target.startswith("/") or ".." in Path(target).parts:
                raise RuntimeExecutionError("writable_mounts 路径非法或不存在")
            args += ["--dir", target]
            args += ["--bind", str(host), target]
        return args

    def _argv(self, request: ExecutionRequest, decision: RuntimeStartDecision) -> list[str]:
        if not self.bwrap_path:
            raise RuntimeExecutionError("sandbox 策略要求 bwrap，但系统未安装")
        policy = decision.policy
        network = policy.get("network") or {}
        domains = list(network.get("domains") or ())
        network_mode = str(network.get("mode", "deny_all"))
        if network_mode not in {"whitelist", "deny_all", "allow_all", "learning"}:
            raise RuntimeExecutionError(f"不支持的网络策略模式：{network_mode}")
        network_enabled = network_mode in {"allow_all", "learning"} or (
            network_mode == "whitelist" and bool(domains)
        )
        args = [
            self.bwrap_path, "--unshare-user", "--unshare-pid", "--unshare-ipc",
            "--unshare-uts", "--cap-drop", "ALL", "--die-with-parent",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        # bubblewrap does not create arbitrary bind targets.  Every target
        # parent must exist inside the namespace before the bind is applied.
        # The policy compiler therefore supplies canonical sandbox paths and
        # this backend creates only the fixed parent directories needed for
        # those paths; it never creates a caller-selected command path.
        targets: set[str] = set()
        for item in [*(policy.get("writable_mounts") or ()),
                     *(policy.get("readonly_bind_mounts") or ())]:
            if isinstance(item, Mapping):
                target = str(item.get("target", "")).strip()
                if target.startswith("/"):
                    targets.add(target)
        for target in sorted(targets):
            parent = Path(target).parent
            parents: list[Path] = []
            while str(parent) not in {"", "/", "."}:
                parents.append(parent)
                parent = parent.parent
            for directory in reversed(parents):
                args += ["--dir", str(directory)]
        # ``/etc/resolv.conf`` may be a symlink into /run/systemd/resolve.
        # Keep that exact target path available only for a network-enabled
        # sandbox; the fully offline path remains an empty network namespace
        # with no host resolver mounted.
        resolv_conf = _resolved_resolv_conf() if network_enabled else None
        if resolv_conf is not None and resolv_conf != Path("/etc/resolv.conf"):
            parents: list[Path] = []
            parent = resolv_conf.parent
            while str(parent) not in {"", "/", "."}:
                parents.append(parent)
                parent = parent.parent
            for directory in reversed(parents):
                args += ["--dir", str(directory)]
            args += ["--ro-bind", str(resolv_conf), str(resolv_conf)]
        # Blocking policies use cgroup IP filters or an offline namespace;
        # learning keeps the network available, including with no domains.
        if not network_enabled:
            args.insert(args.index("--cap-drop"), "--unshare-net")
        for top in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
            path = Path("/") / top
            if path.is_symlink():
                args += ["--symlink", os.readlink(path), f"/{top}"]
            elif path.exists():
                args += ["--ro-bind", str(path), f"/{top}"]
        args += self._mount_args(policy)
        # The control-plane/Worker RPC socket is a security boundary, not a
        # generic data directory.  The policy must bind the host directory to
        # the same absolute target visible to the Worker; otherwise a sandbox
        # Worker can start successfully but can never complete Hello/RPC.
        socket_path = str(request.env.get("BIANBU_WORKER_SOCKET", "")).strip()
        if socket_path:
            socket_dir = Path(socket_path).expanduser().resolve().parent
            socket_target = str(request.env.get("BIANBU_WORKER_SOCKET_TARGET", "")).strip()
            target_dir = Path(socket_target) if socket_target else socket_dir
            mount = _policy_mount_covering(policy, "writable_mounts", target_dir)
            if mount is None:
                raise RuntimeExecutionError(
                    "sandbox policy 未声明 Worker socket writable mount")
            if not socket_dir.is_relative_to(mount[0]):
                raise RuntimeExecutionError(
                    "Worker socket host 目录与 sandbox policy 不一致")
        seccomp = policy.get("seccomp_profile") or {}
        seccomp_path = str(seccomp.get("path", "")).strip() if isinstance(seccomp, Mapping) else ""
        if seccomp_path:
            path = Path(seccomp_path)
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeExecutionError("策略 seccomp 文件不存在或为空")
            # The user systemd manager starts the Worker with the user's
            # primary group, not the securityd service group that owns
            # /run/bianbu/sandbox.  Stage the non-secret BPF bytes beside the
            # already-authorized Worker socket so the transient unit can open
            # the FD without widening the securityd runtime directory.
            if socket_path:
                staged = Path(socket_path).expanduser().resolve().parent / ".seccomp.bpf"
                try:
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    temporary = staged.with_name(f".{staged.name}.{os.getpid()}.tmp")
                    temporary.write_bytes(path.read_bytes())
                    temporary.chmod(0o600)
                    os.replace(temporary, staged)
                    staged.chmod(0o644)
                    seccomp_path = str(staged)
                except OSError as exc:
                    raise RuntimeExecutionError(
                        f"无法为托管 Worker 准备 seccomp 文件：{exc}"
                    ) from exc
            args += ["--seccomp", "0"]
            args += ["--setenv", "BIANBU_SECCOMP_AUDIT_FILTER",
                     _host_to_target(policy, seccomp_path)]
        # Mandatory deny paths are hidden, never merely documented.
        for denied in policy.get("deny_paths") or ():
            path = Path(str(denied))
            if path.is_dir():
                args += ["--tmpfs", str(path)]
            elif path.is_file():
                args += ["--ro-bind", "/dev/null", str(path)]
        for key, value in _safe_env(policy, request).items():
            if key in {"BIANBU_WORKER_SOCKET", "BIANBU_WORKER_CONFIG_PATH",
                       "BIANBU_WORKER_CWD_TARGET", "BIANBU_RUNTIME_DATA_DIR",
                       "BIANBU_SECURITY_CLIENT_DIR", "BIANBU_SEC_ROOT",
                       "BIANBU_USER_SKILLS_DIR", "BIANBU_SYSTEM_RESOURCE_ROOT",
                       "BIANBU_AGENT_MEMORY_SOCKET",
                       "BIANBU_AGENT_MEMORY_REGISTRY_DIR"}:
                value = _host_to_target(policy, value)
            elif key == "BIANBU_SEC_UDS":
                value = "/run/bianbu/securityd.sock"
            elif key == "BIANBU_SECURITYD_HOME":
                value = "/opt/bianbu-securityd"
            args += ["--setenv", key, value]
        preload = str(request.env.get("LD_PRELOAD", ""))
        if preload.endswith("/libbianbu-network-learning.so"):
            args += [
                "--setenv", "LD_PRELOAD", preload,
                "--setenv", "BIANBU_NETWORK_LEARNING_SOCKET",
                "/run/bianbu/network-learning/collector.sock",
                "--setenv", "BIANBU_NETWORK_LEARNING_INSTANCE_ID",
                request.instance,
            ]
        sandbox_cwd = str((policy.get("host_constraints") or {}).get(
            "sandbox_working_directory", "/home/agent"))
        # The worker config uses the conventional ``~/.bianbu-agent`` path.
        # Make that path resolve to the policy-mounted worker home instead of
        # the host user's home (which is intentionally absent from bwrap).
        args += ["--setenv", "HOME", sandbox_cwd,
                 "--setenv", "BIANBU_SANDBOX_ACTIVE", "1",
                 "--chdir", sandbox_cwd, "--",
                 *tuple(_host_to_target(policy, str(item)) for item in request.command)]
        # The fixed Worker entrypoint is a Python module from the immutable
        # Runtime mount.  Its cwd is the isolated Agent data root, so expose
        # the signed mount root explicitly instead of relying on host cwd or
        # an inherited PYTHONPATH.
        # Worker HOME is intentionally read-only.  All mutable cross-process
        # locks must live below the writable worker-data mount, regardless of
        # whether the securityd client bundle is mounted at /opt.
        marker = args.index("--chdir")
        lock_env = [
            "--setenv", "BIANBU_CAPABILITY_LOCK_PATH",
            "/home/agent/runtime/worker/.capability.lock",
            "--setenv", "BIANBU_MODEL_LOCK_PATH",
            "/home/agent/runtime/worker/model-locks",
        ]
        python_paths: list[str] = []
        if _policy_mount(policy, "readonly_bind_mounts", target="/opt/bianbu-securityd"):
            python_paths.append("/opt/bianbu-securityd")
        # Source-checkout Workers execute an immutable Python runtime but the
        # package itself is not installed into that interpreter. Only derive
        # module roots from policy-approved read-only mounts containing the
        # actual bianbu_agent package; never trust an inherited PYTHONPATH.
        for item in policy.get("readonly_bind_mounts") or ():
            if not isinstance(item, Mapping):
                continue
            host = Path(str(item.get("host", ""))).expanduser().resolve()
            target = str(item.get("target", "")).strip()
            if (host / "bianbu_agent" / "__init__.py").is_file() and target.startswith("/"):
                if target not in python_paths:
                    python_paths.append(target)
        if python_paths:
            lock_env = ["--setenv", "PYTHONPATH", ":".join(python_paths), *lock_env]
        args[marker:marker] = lock_env
        return args

    @staticmethod
    def _seccomp_input_path(
        request: ExecutionRequest, decision: RuntimeStartDecision,
    ) -> str | None:
        seccomp = decision.policy.get("seccomp_profile") or {}
        path = str(seccomp.get("path", "")).strip() if isinstance(seccomp, Mapping) else ""
        if not path:
            return None
        socket_path = str(request.env.get("BIANBU_WORKER_SOCKET", "")).strip()
        if socket_path:
            return str(Path(socket_path).expanduser().resolve().parent / ".seccomp.bpf")
        return path

    def start(self, request: ExecutionRequest, decision: RuntimeStartDecision) -> BackendHandle:
        if decision.effective_mode != "sandbox" or decision.backend != self.name:
            raise RuntimeExecutionError("BwrapBackend 与策略 effective_mode/backend 不一致")
        cwd = _safe_cwd(decision.policy, request)
        unit = _unit_name(
            request.agent_id, request.instance,
            unit_type=("scope" if _system_service_mode() else "service"),
        )
        # bwrap 也纳入同一 transient service/scope，保证资源和生命周期约束覆盖整个容器。
        bwrap_command = self._argv(request, decision)
        bwrap_command, _trace_dir = _strace_command(
            bwrap_command, request, decision,
            optional_syscall_audit=self._popen is subprocess.Popen,
        )
        seccomp_input_path = self._seccomp_input_path(request, decision)
        unit_policy = dict(decision.policy)
        unit_policy["host_constraints"] = {
            **dict(unit_policy.get("host_constraints") or {}),
            # bwrap already enforces the mount/deny-path boundary. Repeating
            # ProtectHome/ReadOnlyPaths in a user transient service can fail
            # before bwrap execs with systemd status 218/CAPABILITIES.
            "file_boundary_required": False,
            # bwrap already creates an isolated tmpfs at /tmp. User systemd's
            # PrivateTmp would create a mount namespace in systemd-executor
            # before /usr/bin/bwrap is exec'd, bypassing the bwrap AppArmor
            # profile on hosts that restrict unprivileged user namespaces.
            "private_tmp_required": False,
        }
        launch, unit = _resource_command(
            unit_policy, bwrap_command, unit,
            standard_input_path=seccomp_input_path,
        )
        env = _safe_env(decision.policy, request)
        preload = str(request.env.get("LD_PRELOAD", ""))
        if preload.endswith("/libbianbu-network-learning.so") and Path(preload).is_file():
            env.update({"LD_PRELOAD": preload,
                        "BIANBU_NETWORK_LEARNING_SOCKET": "/run/bianbu/network-learning/collector.sock",
                        "BIANBU_NETWORK_LEARNING_INSTANCE_ID": request.instance})
        if self._popen is subprocess.Popen:
            # ``_resource_command`` uses the absolute systemd-run path when
            # available.  Check the executable basename so the policy-built
            # command (including Memory/CPU/Tasks/IO properties) is retained.
            if not launch or Path(str(launch[0])).name != "systemd-run":
                launch, unit = _resource_command(
                    {**unit_policy, "resources": {"tasks_max": 256}},
                    bwrap_command, unit,
                    standard_input_path=seccomp_input_path,
                )
            process = _launch_systemd(
                launch, unit=unit, cwd=cwd, env=env,
                standard_input_path=seccomp_input_path,
            )
        else:
            try:
                # This branch is used by injected test popen factories only;
                # the real system-service path always uses _launch_systemd so
                # the per-Worker service/scope applies the network filter.
                stdout = None if _system_service_mode() else subprocess.PIPE
                stderr = None if _system_service_mode() else subprocess.PIPE
                process = self._popen(
                    launch, cwd=str(cwd), env=env,
                    start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr,
                )
            except (OSError, ValueError) as exc:
                raise RuntimeExecutionError(f"sandbox 进程启动失败：{exc}") from exc
        pid = int(process.pid)
        try:
            cgroup = current_cgroup_path(pid)
        except (OSError, RuntimeError) as exc:
            _terminate_process(process, timeout=1)
            raise RuntimeExecutionError("sandbox 进程没有可证明的 cgroup") from exc
        return BackendHandle(request.instance, request.agent_id, self.name, process, pid,
                             cgroup, unit=unit, sandboxed=True,
                             unit_scope=("system" if unit and _system_service_mode() else "user"),
                             container_id=f"bwrap:{pid}",
                             network_properties=_network_properties_from_launch(launch),
                             trace_dir=_trace_dir)

    def stop(self, handle: BackendHandle, *, timeout: float) -> None:
        _terminate_process(handle.process, timeout=timeout)


def _attestation(decision: RuntimeStartDecision, handle: BackendHandle) -> dict[str, Any]:
    seccomp = decision.policy.get("seccomp_profile") or {}
    kernel_seccomp_log = bool(
        handle.sandboxed and isinstance(seccomp, Mapping) and seccomp.get("path"))
    return {
        "backend": decision.backend,
        "pid": handle.pid,
        "sandboxed": handle.sandboxed,
        "container_id": handle.container_id,
        "unit": handle.unit,
        "systemd_scope": handle.unit_scope,
        "network_properties": list(handle.network_properties),
        # 只有 bwrap 沙箱真正在内核里装载了 securityd 版本化过滤器并叠加
        # SECCOMP_FILTER_FLAG_LOG；host Worker 为保留 sudo/setuid 语义不装载过滤器
        # （见 HostProcessBackend），因此必须如实报告没有内核 seccomp 审计来源，
        # 不能按策略里是否存在 profile 推断（验收缺陷 D-3）。
        "kernel_seccomp_log": kernel_seccomp_log,
    }


class ExecutionCoordinator:
    """Prepare → backend start → Commit 的串行协调器。"""

    def __init__(self, security_client: SecurityRuntimeClient | None = None,
                 *, state_dir: str | Path | None = None,
                 backends: Mapping[str, ExecutionBackend] | None = None,
                 stop_timeout: float = 3.0,
                 production: bool | None = None):
        self.security_client = security_client or SecurityRuntimeClient()
        self.stop_timeout = max(0.1, float(stop_timeout))
        self.production = (
            str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production"
            if production is None else bool(production)
        )
        self.state_dir = Path(state_dir or (Path.home() / ".local/share/bianbu-agent/runtime"))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.state_dir / "executions.json"
        self._lock = threading.RLock()
        self._handles: dict[str, BackendHandle] = {}
        self._proxies: dict[str, _LoopbackProxy] = {}
        # Optional observer; seccomp remains the enforcement mechanism. It is
        # disabled unless BIANBU_SYSCALL_COLLECTOR_CMD is explicitly set.
        self._collectors: dict[str, SyscallCollector] = {}
        self._network_collectors: dict[str, Any] = {}
        self._records: dict[str, ExecutionRecord] = self._load()
        self.backends: dict[str, ExecutionBackend] = dict(backends or {
            "host-process": HostProcessBackend(),
            "bwrap": BwrapBackend(),
        })

    def _network_learning_client(self, record: ExecutionRecord, policy_version: int) -> PrivilegedNetworkLearningClient:
        loop = asyncio.get_running_loop()
        # Preserve injectable legacy doubles used by development diagnostics;
        # production always uses the privileged socket client below.
        if NetworkLearningCollector is not _DEFAULT_NETWORK_LEARNING_COLLECTOR:
            return NetworkLearningCollector(
                event_callback=lambda event: loop.call_soon_threadsafe(self._forward_network_event, record, policy_version, event),
                status_callback=lambda state, reason: loop.call_soon_threadsafe(self._forward_network_status, record, state, reason),
            )
        loop = asyncio.get_running_loop()
        def status(state: str, reason: str) -> None:
            async def forward() -> None:
                try:
                    await self.security_client.report_network_learning_collector(
                        record.agent_id, record.instance_id, record.lease_id,
                        record.pid, state, reason)
                except Exception:
                    logger.warning("network learning status forwarding failed", exc_info=True)
            loop.call_soon_threadsafe(lambda: asyncio.create_task(forward()))
        def event(value: Mapping[str, Any]) -> None:
            try:
                clean = {"event_id": str(value.get("event_id", "")).strip()
                         or f"preload-{record.instance_id}-{uuid.uuid4().hex}",
                         "observed_at": float(value.get("observed_at", 0)),
                         "event_type": str(value.get("event_type", "connect")),
                         "protocol": str(value.get("protocol", "tcp")),
                         "domain": str(value.get("domain", "")),
                         "remote_ip": str(value.get("remote_ip", "")),
                         "remote_port": int(value.get("remote_port", 0) or 0),
                         "dns_qtype": str(value.get("dns_qtype", "")),
                         "outcome": str(value.get("outcome", "success")),
                         "errno": str(value.get("errno", "")),
                         "backend": str(value.get("backend", "preload")), "policy_version": int(policy_version)}
            except (TypeError, ValueError):
                return
            async def forward() -> None:
                try:
                    await self.security_client.record_network_learning_event(
                        record.agent_id, record.instance_id, record.lease_id,
                        record.pid, clean)
                except Exception:
                    logger.warning("network learning event forwarding failed", exc_info=True)
            loop.call_soon_threadsafe(lambda: asyncio.create_task(forward()))
        return PrivilegedNetworkLearningClient(event_callback=event, status_callback=status)

    def _forward_network_status(self, record: ExecutionRecord, state: str, reason: str) -> None:
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self.security_client.report_network_learning_collector(
                record.agent_id, record.instance_id, record.lease_id, record.pid, state, reason))
        except Exception:
            logger.warning("network learning status forwarding failed", exc_info=True)

    def _forward_network_event(self, record: ExecutionRecord, policy_version: int, value: Mapping[str, Any]) -> None:
        try:
            loop = asyncio.get_event_loop()
            clean = {"event_id": str(value.get("event_id", "")).strip(),
                     "observed_at": float(value.get("observed_at", 0)),
                     "event_type": str(value.get("event_type", "")).strip(),
                     "protocol": str(value.get("protocol", "")).strip(),
                     "domain": str(value.get("domain", "")).strip(),
                     "remote_ip": str(value.get("remote_ip", "")).strip(),
                     "remote_port": int(value.get("remote_port", 0) or 0),
                     "dns_qtype": str(value.get("dns_qtype", "")).strip(),
                     "outcome": str(value.get("outcome", "")).strip(),
                     "errno": str(value.get("errno", "")).strip(),
                     "backend": "strace", "policy_version": policy_version}
            loop.create_task(self.security_client.record_network_learning_event(
                record.agent_id, record.instance_id, record.lease_id, record.pid, clean))
        except Exception:
            logger.warning("network learning event forwarding failed", exc_info=True)
    async def _start_hot_network_collector(
        self, record: ExecutionRecord, handle: BackendHandle, policy_version: int,
    ) -> bool:
        collector = self._network_learning_client(record, policy_version)
        if isinstance(collector, PrivilegedNetworkLearningClient):
            started = await asyncio.to_thread(collector.register, instance_id=record.instance_id,
                                               cgroup_path=handle.cgroup_path,
                                               agent_id=record.agent_id, lease_id=record.lease_id,
                                               pid=handle.pid)
        else:
            started = bool(getattr(collector, "enabled", False)) and await asyncio.to_thread(collector.start, trace_dir=handle.trace_dir, attach_pid=handle.pid)
        if started or isinstance(collector, PrivilegedNetworkLearningClient):
            self._network_collectors[record.instance_id] = collector
            return True
        try:
            await self.security_client.report_network_learning_collector(
                record.agent_id, record.instance_id, record.lease_id, record.pid,
                "degraded", "network learning collector unavailable",
            )
        except Exception:
            logger.warning("network learning degraded status failed", exc_info=True)
        return False

    async def apply_network_learning_policy(
        self, agent_id: str, mode: str, policy_version: int,
    ) -> dict[str, int]:
        """Synchronize learning observation with an already-updated host policy."""
        selected_mode = str(mode).strip().lower()
        if selected_mode not in {"whitelist", "deny_all", "allow_all", "learning"}:
            raise ValueError("invalid network mode")
        with self._lock:
            active = [
                (record, self._handles.get(instance_id))
                for instance_id, record in self._records.items()
                if record.agent_id == str(agent_id)
                and record.state == ExecutionState.RUNNING
                and self._handles.get(instance_id) is not None
            ]
        result = {"active_instances": len(active), "collectors_started": 0,
                  "collectors_stopped": 0, "degraded_instances": 0}
        for record, handle in active:
            assert handle is not None
            if selected_mode == "learning":
                if record.instance_id in self._network_collectors:
                    continue
                if handle.backend != "host-process":
                    result["degraded_instances"] += 1
                    continue
                if await self._start_hot_network_collector(
                    record, handle, int(policy_version),
                ):
                    result["collectors_started"] += 1
                else:
                    result["degraded_instances"] += 1
                continue
            collector = self._network_collectors.pop(record.instance_id, None)
            if collector is None:
                continue
            if hasattr(collector, "unregister"):
                await asyncio.to_thread(collector.unregister)
            else:
                await asyncio.to_thread(collector.stop, self.stop_timeout)
            try:
                await self.security_client.report_network_learning_collector(
                    record.agent_id, record.instance_id, record.lease_id, record.pid,
                    "inactive", f"network policy changed to {selected_mode}",
                )
            except Exception:
                logger.warning("network learning inactive status failed", exc_info=True)
            result["collectors_stopped"] += 1
        return result

    def _load(self) -> dict[str, ExecutionRecord]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            records: dict[str, ExecutionRecord] = {}
            for key, value in raw.items():
                if not isinstance(value, dict):
                    continue
                value = dict(value)
                value.setdefault("proxy_port", None)
                value.setdefault("unit_scope", "user")
                records[key] = ExecutionRecord(**value)
            return records
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        payload = {key: value.as_dict() for key, value in self._records.items()}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.state_path)

    def _validate_request(self, request: ExecutionRequest, decision: RuntimeStartDecision) -> None:
        policy = decision.policy
        if policy.get("agent_id") != request.agent_id:
            raise RuntimeExecutionError("policy agent_id 与请求不一致")
        if decision.backend not in self.backends:
            raise RuntimeExecutionError(f"执行后端不可用：{decision.backend}")
        if decision.sandbox_required != (decision.effective_mode == "sandbox"):
            raise RuntimeExecutionError("sandbox_required 与 effective_mode 不一致")
        declared_entrypoint = tuple(str(x) for x in (policy.get("entrypoint") or ()))
        if self.production:
            if not declared_entrypoint:
                raise RuntimeExecutionError("生产策略未声明固定 entrypoint")
            if tuple(request.command) != declared_entrypoint:
                raise RuntimeExecutionError("启动命令与已登记 entrypoint 不一致")
        if request.listen_port is not None:
            if request.target_port is None:
                raise RuntimeExecutionError("端口代理缺少 target_port")
            if decision.effective_mode == "sandbox":
                raise RuntimeExecutionError("当前 bwrap 后端仅允许 host loopback 端口代理")
            constraints = policy.get("host_constraints") or {}
            if not constraints.get("port_proxy_required", False):
                raise RuntimeExecutionError("策略未声明端口代理约束")

        # securityd may be unable to stat a user's Home because its service
        # unit uses ProtectHome=yes.  The Runtime is the trusted process that
        # owns these paths, so validate their existence and type immediately
        # before spawning instead of asking securityd to see through its mount
        # namespace.
        enforce_mounts = self.production or bool(
            decision.policy.get("writable_mounts")
            or decision.policy.get("readonly_bind_mounts")
        )
        if enforce_mounts:
            for target, field in (("/home/agent", "worker_data_root"),
                                  ("/run/bianbu-agent/instances", "worker_socket_root")):
                source = (
                    decision.policy.get("readonly_bind_mounts", ())
                    if target == "/home/agent"
                    else decision.policy.get("writable_mounts", ())
                )
                mounts = [
                    item for item in source
                    if isinstance(item, Mapping) and str(item.get("target", "")) == target
                ]
                if not mounts:
                    raise RuntimeExecutionError(f"策略未声明 {field} 挂载")
                path = Path(str(mounts[0].get("host", ""))).expanduser().resolve()
                if not path.is_dir():
                    raise RuntimeExecutionError(f"{field} 不存在或不是目录：{path}")
            for item in [*decision.policy.get("writable_mounts", ()),
                         *decision.policy.get("readonly_bind_mounts", ())]:
                if not isinstance(item, Mapping):
                    raise RuntimeExecutionError("策略挂载项必须是对象")
                host = Path(str(item.get("host", ""))).expanduser().resolve()
                if not host.exists():
                    raise RuntimeExecutionError(f"策略挂载路径不存在：{host}")
        config_value = str(request.env.get("BIANBU_WORKER_CONFIG_PATH", "")).strip()
        if config_value and not Path(config_value).expanduser().resolve().is_file():
            raise RuntimeExecutionError(f"worker_config_path 不存在或不是文件：{config_value}")

    async def start(self, request: ExecutionRequest) -> ExecutionRecord:
        """Start an explicitly prepared request.

        HTTP callers must use ``BianbuAgentRuntime.start_agent_execution``;
        this lower-level method remains public for embedders and tests, but
        production still requires the fixed entrypoint returned by securityd.
        """
        decision = await self.security_client.prepare_start(RuntimeStartRequest(
            request.agent_id, request.requested_mode))
        if decision is None:
            raise RuntimeExecutionError("security.runtime 未返回启动决策")
        return await self._start_prepared(request, decision)

    async def start_registered(
        self,
        *,
        agent_id: str,
        worker_agent_id: str | None = None,
        requested_mode: str = "host",
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        instance_id: str = "",
        target_port: int | None = None,
        listen_port: int | None = None,
        worker_data_root: str | Path | None = None,
        worker_socket_root: str | Path | None = None,
        worker_config_path: str | Path | None = None,
        security_uds: str | Path | None = None,
        securityd_home: str | Path | None = None,
        memory_uds: str | Path | None = None,
        memory_registry_dir: str | Path | None = None,
        bibit_intent_root: str | Path | None = None,
        runtime_source_root: str | Path | None = None,
        python_runtime_root: str | Path | None = None,
        python_runtime_target: str | Path | None = None,
        user_skills_root: str | Path | None = None,
        fallback_entrypoint: tuple[str, ...] | None = None,
        before_commit: Callable[[BackendHandle, ExecutionRecord, RuntimeStartDecision],
                                 Awaitable[None]] | None = None,
    ) -> ExecutionRecord:
        """Start only the entrypoint registered in securityd's manifest.

        There is deliberately no command parameter.  The temporary marker is
        replaced with the immutable policy entrypoint before backend startup;
        an absent entrypoint is a hard error in every production deployment.

        In development mode (security.required=false), fallback_entrypoint can
        be used when security runtime is unavailable.
        """
        placeholder = ExecutionRequest(
            agent_id, ("__security_runtime_entrypoint__",), requested_mode,
            env or {}, cwd, instance_id, target_port, listen_port,
        )
        decision = await self.security_client.prepare_start(RuntimeStartRequest(
            agent_id, requested_mode,
            worker_agent_id=str(worker_agent_id or ""),
            worker_data_root=str(worker_data_root or ""),
            worker_socket_root=str(worker_socket_root or ""),
            worker_config_path=str(worker_config_path or ""),
            security_uds=str(security_uds or ""),
            securityd_home=str(securityd_home or ""),
            memory_uds=str(memory_uds or ""),
            memory_registry_dir=str(memory_registry_dir or "")))

        if decision is None:
            # The coordinator owns only security-managed launches. Development
            # fallback is performed once by WorkerSupervisor, which retains the
            # process handle and can therefore prove that it exited.
            raise RuntimeExecutionError("security.runtime 未返回启动决策")
        registered_entrypoint = tuple(
            str(item) for item in (decision.policy.get("entrypoint") or ())
        )
        decision = _with_runtime_worker_policy(
            decision,
            memory_uds=memory_uds,
            memory_registry_dir=memory_registry_dir,
            bibit_intent_root=bibit_intent_root,
            runtime_source_root=(runtime_source_root or
                                 _runtime_source_root(registered_entrypoint)),
            python_runtime_root=python_runtime_root,
            python_runtime_target=python_runtime_target,
            user_skills_root=user_skills_root,
        )
        entrypoint = tuple(str(item) for item in (decision.policy.get("entrypoint") or ()))
        if not entrypoint or any(not item.strip() for item in entrypoint):
            raise RuntimeExecutionError("security.runtime 策略未声明固定 Agent entrypoint")

        request = ExecutionRequest(
            placeholder.agent_id, entrypoint, placeholder.requested_mode,
            placeholder.env, placeholder.cwd, placeholder.instance_id,
            placeholder.target_port, placeholder.listen_port,
        )
        return await self._start_prepared(
            request,
            decision,
            worker_agent_id=worker_agent_id,
            before_commit=before_commit,
        )

    async def _start_prepared(
        self, request: ExecutionRequest, decision: RuntimeStartDecision,
        *, worker_agent_id: str | None = None,
        before_commit: Callable[[BackendHandle, ExecutionRecord, RuntimeStartDecision],
                                    Awaitable[None]] | None = None,
    ) -> ExecutionRecord:
        if self.production and str(os.environ.get("BIANBU_RUNTIME_MANAGED", "")) != "1":
            raise RuntimeExecutionError("生产模式必须由受控 Runtime Manager 启动")
        self._validate_request(request, decision)
        instance = request.instance
        logical_agent_id = str(worker_agent_id or request.agent_id).strip()
        if not logical_agent_id:
            raise RuntimeExecutionError("Worker Agent ID 不能为空")
        request = ExecutionRequest(
            request.agent_id, request.command, request.requested_mode,
            {
                **dict(request.env),
                # These values are supervisor-issued identity, not caller
                # supplied command data. They let the fixed Worker entrypoint
                # report the exact lease/mode in Hello after bwrap/systemd
                # has started it.
                "BIANBU_WORKER_INSTANCE_ID": instance,
                "BIANBU_WORKER_AGENT_ID": logical_agent_id,
                # ExecutionRequest.agent_id is the security/runtime Agent ID
                # used to issue the lease; RuntimeStartDecision intentionally
                # contains only the lease decision fields.
                "BIANBU_WORKER_SECURITY_AGENT_ID": request.agent_id,
                "COM_BIANBU_AGENT_ID": logical_agent_id,
                "BIANBU_SECURITY_SUBJECT": f"com.bianbu.agent_id:{logical_agent_id}",
                "BIANBU_WORKER_LEASE_ID": decision.lease_id,
                "BIANBU_WORKER_EFFECTIVE_MODE": decision.effective_mode,
                "BIANBU_WORKER_BACKEND": decision.backend,
            },
            request.cwd, instance,
            request.target_port, request.listen_port)
        observation = decision.policy.get("network") or {}
        if str(observation.get("mode", "")).lower() == "learning" or observation.get("observe") is True:
            preload = os.environ.get("BIANBU_NETWORK_LEARNING_PRELOAD", "/usr/lib/bianbu-agent-runtime/libbianbu-network-learning.so")
            if Path(preload).is_file():
                request = ExecutionRequest(
                    request.agent_id, request.command, request.requested_mode,
                    {**dict(request.env), "LD_PRELOAD": preload,
                     "BIANBU_NETWORK_LEARNING_SOCKET": "/run/bianbu/network-learning/collector.sock",
                     "BIANBU_NETWORK_LEARNING_INSTANCE_ID": instance},
                    request.cwd, instance, request.target_port, request.listen_port)
        backend = self.backends[decision.backend]
        with self._lock:
            if instance in self._handles:
                raise RuntimeExecutionError(f"实例已存在：{instance}")
            record = ExecutionRecord(instance, request.agent_id, decision.backend, 0, "", "",
                                     ExecutionState.STARTING, decision.lease_id,
                                     time.time(),
                                     decision.effective_mode == "sandbox")
            self._records[instance] = record
            self._save()
        handle: BackendHandle | None = None
        committed = False
        proxy: _LoopbackProxy | None = None
        event_loop = asyncio.get_running_loop()

        def collector_event(event: Mapping[str, Any]) -> None:
            """Validate basic event shape and forward asynchronously.

            securityd remains the trust boundary and performs lease/cgroup
            validation; malformed observer output is dropped here.
            """
            try:
                pid = int(event.get("pid", 0))
                number = int(event.get("number", event.get("syscall_nr", -1)))
                syscall = str(event.get("syscall", event.get("name", ""))).strip()
                arch = str(event.get("arch", "x86_64")).strip()
                if pid <= 0 or number < 0 or not syscall or not arch:
                    return
                backend_name = str(event.get("backend", "ebpf-tracepoint"))
                version = int(event.get("policy_version", 0) or 0)
            except (TypeError, ValueError):
                return

            async def forward() -> None:
                try:
                    await self.security_client.record_syscall_interception(
                        request.agent_id, pid, arch, syscall, number,
                        backend_name, version,
                        instance_id=instance, lease_id=decision.lease_id,
                        cgroup_id=event.get("cgroup_id"),
                        worker_agent_id=logical_agent_id,
                        event_id=event.get("event_id"),
                    )
                except Exception:
                    # Audit transport failure must never weaken seccomp.
                    logger.warning("syscall audit event forwarding failed", exc_info=True)

            event_loop.call_soon_threadsafe(lambda: asyncio.create_task(forward()))
        try:
            handle = await asyncio.to_thread(backend.start, request, decision)
            cgroup = handle.cgroup_path
            record.pid = handle.pid
            record.cgroup_path = cgroup
            record.unit = handle.unit
            record.unit_scope = handle.unit_scope
            record.container_id = handle.container_id
            if before_commit is not None:
                await before_commit(handle, record, decision)
            await self.security_client.commit_start(
                decision.lease_id, instance, handle.pid, cgroup,
                _attestation(decision, handle))
            committed = True
            collector = SyscallCollector(event_callback=collector_event)
            if collector.enabled:
                socket_for_trace = str(request.env.get("BIANBU_WORKER_SOCKET", "")).strip()
                trace_dir = (str(Path(socket_for_trace).expanduser().resolve().parent / ".syscall-traces" / instance)
                             if socket_for_trace else "")
                Path(trace_dir).mkdir(parents=True, exist_ok=True)
                collector.start(
                    cgroup_path=cgroup,
                    # The lease is issued and committed for the security
                    # subject in the execution request (normally
                    # ``com.bianbu.runtime``), not the user-facing Worker
                    # Agent ID.  V2 audit validation binds this field to the
                    # committed lease; using logical_agent_id here causes
                    # every event to be rejected as a cross-agent event.
                    agent_id=request.agent_id,
                    instance_id=instance,
                    lease_id=decision.lease_id,
                    policy_version=(decision.policy.get("seccomp_profile") or {}).get("policy_version", 0),
                    kernel_seccomp_log=bool(
                        handle.sandboxed
                        and (decision.policy.get("seccomp_profile") or {}).get("path")),
                    trace_dir=trace_dir,
                )
                self._collectors[instance] = collector
            network_policy = decision.policy.get("network") or {}
            if str(network_policy.get("mode", "")) == "learning" or network_policy.get("observe") is True:
                network_collector = None
                try:
                    policy_version = int(
                        network_policy.get("version")
                        or decision.policy.get("version")
                        or decision.policy.get("policy_version", 0)
                        or 0
                    )
                    network_collector = self._network_learning_client(record, policy_version)
                    if isinstance(network_collector, PrivilegedNetworkLearningClient):
                        started = await asyncio.to_thread(
                            network_collector.register,
                            instance_id=instance, cgroup_path=cgroup,
                            agent_id=request.agent_id, lease_id=decision.lease_id,
                            pid=handle.pid)
                    else:
                        started = bool(getattr(network_collector, "enabled", False)) and await asyncio.to_thread(
                            network_collector.start, trace_dir=_trace_directory(request),
                            attach_pid=handle.pid)
                    if started or isinstance(network_collector, PrivilegedNetworkLearningClient):
                        self._network_collectors[instance] = network_collector
                    else:
                        try:
                            await self.security_client.report_network_learning_collector(
                                request.agent_id, instance, decision.lease_id, handle.pid,
                                "degraded", "network learning collector unavailable")
                        except Exception:
                            logger.warning(
                                "network learning degraded status failed",
                                exc_info=True,
                            )
                except Exception:
                    if network_policy.get("observe") is not True:
                        raise
                    # Passive YOLO observation must never stop an otherwise
                    # committed Worker, including constructor/registration errors.
                    logger.warning("YOLO network observation unavailable", exc_info=True)
                    if network_collector is not None:
                        try:
                            if hasattr(network_collector, "unregister"):
                                await asyncio.to_thread(network_collector.unregister)
                            elif hasattr(network_collector, "stop"):
                                await asyncio.to_thread(network_collector.stop, self.stop_timeout)
                        except Exception:
                            logger.warning("network observation cleanup failed", exc_info=True)
                    try:
                        await self.security_client.report_network_learning_collector(
                            request.agent_id, instance, decision.lease_id, handle.pid,
                            "degraded", "YOLO network observation unavailable")
                    except Exception:
                        logger.warning("network observation degraded status failed", exc_info=True)
            if request.listen_port is not None:
                try:
                    proxy = _LoopbackProxy(request.target_port, request.listen_port)
                except Exception:
                    await asyncio.to_thread(backend.stop, handle, timeout=1.0)
                    raise RuntimeExecutionError("Commit 成功后端口代理创建失败")
            record.state = ExecutionState.RUNNING
            record.container_id = handle.container_id
            record.proxy_port = proxy.port if proxy is not None else None
            with self._lock:
                self._handles[instance] = handle
                if proxy is not None:
                    self._proxies[instance] = proxy
                self._records[instance] = record
                self._save()
            return record
        except Exception as exc:
            network_collector = self._network_collectors.pop(instance, None)
            if network_collector is not None:
                if hasattr(network_collector, "unregister"):
                    await asyncio.to_thread(network_collector.unregister)
                else:
                    await asyncio.to_thread(network_collector.stop, 1.0)
            collector = self._collectors.pop(instance, None)
            if collector is not None:
                await asyncio.to_thread(collector.stop, 1.0)
            if proxy is not None:
                await asyncio.to_thread(proxy.close)
            if handle is not None:
                await asyncio.to_thread(backend.stop, handle, timeout=1.0)
            try:
                if committed:
                    await self.security_client.revoke_agent(
                        request.agent_id, f"启动后处理失败：{exc}"
                    )
                else:
                    await self.security_client.abort_start(decision.lease_id, str(exc))
            except Exception:
                logger.exception("security.runtime cleanup failed after execution startup error")
            record.state = ExecutionState.FAILED
            record.reason = str(exc)
            with self._lock:
                self._records[instance] = record
                self._save()
            raise RuntimeExecutionError(f"受控 Agent 启动失败：{exc}") from exc

    async def stop(self, instance_id: str, *, reason: str = "requested") -> bool:
        with self._lock:
            record = self._records.get(instance_id)
            handle = self._handles.get(instance_id)
        if record is None:
            return False
        record.state = ExecutionState.STOPPING
        with self._lock:
            self._save()
        network_collector = self._network_collectors.pop(instance_id, None)
        if network_collector is not None:
            try:
                await self.security_client.report_network_learning_collector(
                    record.agent_id, record.instance_id, record.lease_id,
                    record.pid, "inactive", reason)
            except Exception:
                logger.warning("network learning inactive status failed", exc_info=True)
            if hasattr(network_collector, "unregister"):
                await asyncio.to_thread(network_collector.unregister)
            else:
                await asyncio.to_thread(network_collector.stop, self.stop_timeout)
        if handle is not None:
            backend = self.backends.get(handle.backend)
            if backend is not None:
                await asyncio.to_thread(backend.stop, handle, timeout=self.stop_timeout)
        collector = self._collectors.pop(instance_id, None)
        if collector is not None:
            await asyncio.to_thread(collector.stop, self.stop_timeout)
        else:
            await asyncio.to_thread(
                _terminate_pid,
                record.pid,
                unit=record.unit,
                scope=record.unit_scope,
                timeout=self.stop_timeout,
            )
        with self._lock:
            proxy = self._proxies.pop(instance_id, None)
        if proxy is not None:
            await asyncio.to_thread(proxy.close)
        # A normal stop must not revoke the whole agent: during package/service
        # restart an older coordinator can finish stopping after a new worker
        # has already committed its lease, and an agent-wide revoke would
        # invalidate that live worker.  Converge only this lease/instance via
        # StopInstance so securityd does not keep a ghost ``running`` row; the
        # daemon still verifies kernel PID/cgroup on every data-plane call, so
        # an old daemon without StopInstance remains safe (call returns None).
        stop_instance = getattr(self.security_client, "stop_instance", None)
        if callable(stop_instance) and record.lease_id and record.state is ExecutionState.STOPPING:
            try:
                await stop_instance(record.lease_id, record.instance_id, reason)
            except Exception:  # noqa: BLE001 - never block local stop
                logger.warning("securityd stop_instance failed", exc_info=True)
        record.state = ExecutionState.STOPPED
        record.reason = reason
        with self._lock:
            self._handles.pop(instance_id, None)
            self._records[instance_id] = record
            self._save()
        return True

    async def revoke(self, agent_id: str, *, reason: str = "security revoke") -> int:
        await self.security_client.revoke_agent(agent_id, reason)
        with self._lock:
            ids = [key for key, value in self._records.items()
                   if value.agent_id == agent_id and value.state in {
                       ExecutionState.RUNNING, ExecutionState.STARTING
                   }]
        for instance_id in ids:
            await self.stop(instance_id, reason=reason)
        return len(ids)

    async def reap_orphans(self) -> int:
        """Runtime 重启后清理仍属于本地 managed ledger 的活动进程。"""
        with self._lock:
            candidates = [value for value in self._records.values()
                          if value.state in {ExecutionState.RUNNING, ExecutionState.STARTING}]
        reaped = 0
        for record in candidates:
            if record.pid <= 0:
                record.state = ExecutionState.FAILED
                record.reason = "orphan record has no started pid"
                continue
            try:
                os.kill(record.pid, 0)
            except (ProcessLookupError, OSError):
                record.state = ExecutionState.STOPPED
                record.reason = "orphan already exited"
                continue
            if record.unit:
                # A transient unit is the ownership boundary.  Never signal a
                # PID that has been reused by an unrelated process.
                if not _unit_owns_pid(record.unit, record.pid, scope=record.unit_scope):
                    record.state = ExecutionState.STOPPED
                    record.reason = "orphan unit no longer owns recorded pid"
                    continue
            else:
                try:
                    if _normalise_cgroup(current_cgroup_path(record.pid)) != _normalise_cgroup(record.cgroup_path):
                        record.state = ExecutionState.STOPPED
                        record.reason = "orphan pid cgroup no longer matches ledger"
                        continue
                except (OSError, RuntimeError):
                    record.state = ExecutionState.STOPPED
                    record.reason = "orphan pid cgroup cannot be verified"
                    continue
            await self.stop(record.instance_id, reason="orphan reaped after Runtime restart")
            reaped += 1
        with self._lock:
            self._save()
        return reaped

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [record.as_dict() for record in self._records.values()]

    async def close(self) -> None:
        """停止本协调器持有的所有活动实例并落盘最终状态。"""
        with self._lock:
            active = [key for key, value in self._records.items()
                      if value.state in {ExecutionState.RUNNING, ExecutionState.STARTING}]
        for instance_id in active:
            await self.stop(instance_id, reason="Runtime coordinator close")


def production_launch_allowed() -> bool:
    """供 run.sh/包装器使用的生产绕过门禁。"""
    if str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() != "production":
        return True
    return str(os.environ.get("BIANBU_RUNTIME_MANAGED", "")) == "1"


__all__ = [
    "BackendHandle", "BwrapBackend", "ExecutionBackend", "ExecutionCoordinator",
    "ExecutionRecord", "ExecutionRequest", "ExecutionState", "HostProcessBackend",
    "RuntimeExecutionError", "production_launch_allowed",
]
