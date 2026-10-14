"""控制面中的默认 Agent Worker 生命周期与 Run 代理。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shlex
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from collections.abc import Sequence
from typing import Any, Mapping

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.plugins import DEFAULT_ACCOUNT_ID
from bianbu_agent.runs import (
    RunError,
    RunHandle,
    RunResult,
    decode_result,
    encode_error,
)
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.sessions.inbox import SessionInboxMessage, inbox_message_to_dict
from bianbu_agent.security.runtime_process.worker_rpc import (
    WorkerRPCClient,
    WorkerRPCError,
    default_socket_root,
    make_instance_socket,
)
from bianbu_agent.security.runtime_process.cgroup_memory import (
    read_cgroup_memory,
    memory_limit_evidence,
)
from bianbu_agent.security.execution_runtime import ExecutionCoordinator, ExecutionRecord
from bianbu_agent.security.security_runtime import SecurityRuntimeClient
from bianbu_agent.observability import bind_log_context, log_run_terminal

logger = logging.getLogger(__name__)

# 连接被对端切断的异常族。Worker 被 memcg OOM killer 杀掉时只剩这一类表象，
# 也是唯一值得去查内存记账的失败形态。
_WORKER_CONNECTION_LOST = (EOFError, ConnectionError)

_SANDBOX_CONFIG = "/home/agent/config.yaml"
# Session deletion waits for the cancelled Run streams so the store only drops
# Run projections after their streams stopped writing to them.
_SESSION_DELETE_JOIN_TIMEOUT_SECONDS = 30.0


def _sandbox_config_argument(config_path: Path, data_dir: Path) -> str:
    """Return the config argument a sandboxed Worker is started with.

    Security Runtime binds the Agent data root at ``/home/agent``, so a signed
    entrypoint names the Worker config relative to that root.  Leftovers from
    manifests outside the data root keep the legacy fixed path.
    """
    if not str(config_path):
        return _SANDBOX_CONFIG
    try:
        relative = config_path.expanduser().resolve().relative_to(
            data_dir.expanduser().resolve()
        )
    except (OSError, ValueError):
        return _SANDBOX_CONFIG
    return f"/home/agent/{relative.as_posix()}"


def _default_account_id() -> str:
    return os.environ.get("BIANBU_AGENT_MEMORY_ACCOUNT_ID", "").strip() or DEFAULT_ACCOUNT_ID


def agent_sandbox_requested() -> bool:
    """Return whether startup requested the security-managed Agent sandbox."""
    return str(os.environ.get("BIANBU_SANDBOX", "")).strip().lower() == "agent"


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    agent_id: str
    instance_id: str
    lease_id: str
    effective_mode: str
    backend: str
    sandboxed: bool
    container_id: str
    pid: int
    peer_pid: int
    generation_id: int | None = None


class WorkerSupervisor:
    """Start one fixed-entrypoint Worker and expose its RPC client."""

    def __init__(self, config: RuntimeConfig, *, agent_id: str,
                 metrics: object | None = None,
                 requested_mode: str | None = None) -> None:
        self.config = config
        self.metrics = metrics
        self.agent_id = str(agent_id).strip()
        self.requested_mode = str(requested_mode or "").strip().lower()
        if not self.agent_id:
            raise ValueError("agent_id must not be empty")
        self.security_agent_id = (
            os.environ.get("BIANBU_RUNTIME_SECURITY_ID") or "com.bianbu.runtime"
        ).strip()
        self.instance_id = ""
        # One unpredictable credential per Worker instance. It is transported
        # only through the supervisor-issued environment and private UDS;
        # it is never part of Hello, the manifest digest, or public status.
        self.rpc_token = ""
        root = config.runtime.control.worker_socket_root or default_socket_root(config.data_dir)
        self.socket_root = Path(root)
        self.coordinator = ExecutionCoordinator(
            state_dir=config.data_dir / "runtime",
            security_client=SecurityRuntimeClient(
                required=config.security.required, metrics=metrics,
            ),
        )
        self.client: WorkerRPCClient | None = None
        self.record: ExecutionRecord | None = None
        self.identity: WorkerIdentity | None = None
        self._ready = asyncio.Event()
        self._starting: asyncio.Task[None] | None = None
        self._closed = False
        self._health_task: asyncio.Task[None] | None = None
        self._restart_count = 0
        self._last_health: dict[str, Any] = {}
        # 周期采样的 scope 内存记账：内核回收 scope 后就查不到实时值了，
        # 最后一次采样是 OOM 归因的兜底证据。
        self._cgroup_memory = None
        self._failure: Exception | None = None
        self._event_sequences: dict[str, int] = {}
        self._memory_registration_path: Path | None = None
        self._memory_registration = None
        self._hook_memory_registration = None
        self._development_process: asyncio.subprocess.Process | None = None
        self.catalog_stale = False

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self.client is not None and self.identity is not None

    @property
    def socket_path(self) -> Path:
        if not self.instance_id:
            raise RuntimeError("Worker instance has not started")
        return make_instance_socket(self.socket_root, self.instance_id)

    def _entrypoint(self) -> tuple[str, ...]:
        configured = self.config.runtime.worker.entrypoint
        if configured:
            return configured
        # Development fallback is still a fixed argv and never accepts HTTP
        # command input. Production policy service requires its signed entrypoint.
        return (
            sys.executable, "-m", "bianbu_agent.cli", "--role", "agent-worker",
            "--config", str(self.config.config_path or ""), "worker",
        )

    def _requested_mode(self) -> str:
        """Ask security.runtime to apply its own decision.

        Whether this Worker needs a sandbox follows from the security settings
        the operator declared for the Agent, and securityd is the authority
        that decides it. The Runtime submits ``auto`` so it never names a side
        on the Agent's behalf.
        """
        return "auto"

    @staticmethod
    def _allows_unmanaged_fallback(requested_mode: str) -> bool:
        # ``BIANBU_SANDBOX=agent`` means "run through the managed Worker" (see
        # run.sh); it says nothing about which mode securityd will pick, but it
        # does forbid silently degrading to an unmanaged host process.
        if str(requested_mode).strip().lower() == "sandbox":
            return False
        if agent_sandbox_requested():
            return False
        development = str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() != "production"
        allow_unmanaged = str(os.environ.get("BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER", "")) == "1"
        return development and allow_unmanaged

    async def start(self) -> WorkerIdentity:
        if self.ready:
            assert self.identity is not None
            return self.identity
        if self._starting is not None:
            await self._starting
            assert self.identity is not None
            return self.identity
        self._starting = asyncio.create_task(self._start_impl(), name="bianbu-worker-start")
        if self.metrics is not None:
            self.metrics.inc("runtime_worker_start_seconds", mode="unknown", result="started")
        try:
            await self._starting
        except Exception as exc:
            # Keep the failure available to the control-plane status endpoint.
            # A failed first Worker must not be mistaken for an unstarted
            # control plane, and the supervisor remains retryable.
            self._failure = exc
            raise
        finally:
            self._starting = None
        assert self.identity is not None
        return self.identity

    async def reap_orphans(self) -> int:
        """Reap ledger-owned workers left by an earlier control process."""
        count = await self.coordinator.reap_orphans()
        if self.metrics is not None and count:
            self.metrics.inc("runtime_worker_orphans_reaped_total", count)
        if count:
            await self._audit_event("runtime.orphan_reaped", self.agent_id,
                                    f"count={count}")
        return count

    async def _audit_event(self, event: str, target: str, result: str) -> None:
        audit = getattr(self.coordinator.security_client, "audit_event", None)
        if not callable(audit):
            return
        try:
            await audit(self.agent_id, event, target, result)
        except Exception as exc:  # noqa: BLE001 - audit must not hide cleanup
            logger.warning("securityd audit event failed event=%s: %s", event, exc)

    def status_snapshot(self) -> dict[str, Any]:
        """Return a non-secret Worker view for Runtime health/status APIs."""
        identity = self.identity
        record = self.record
        if identity is not None and self.ready:
            return {
                "state": "running",
                "instance_id": identity.instance_id,
                "pid": identity.pid,
                "effective_mode": identity.effective_mode,
                "backend": identity.backend,
                "sandboxed": identity.sandboxed,
                "generation_id": identity.generation_id,
                "catalog_stale": self.catalog_stale,
                "memory_registration": (self._memory_registration.status()
                                        if self._memory_registration is not None else None),
            }
        if self._starting is not None:
            state = "starting"
        elif self._failure is not None:
            state = "failed"
        elif record is not None:
            state = str(record.state).lower()
        else:
            state = "stopped"
        return {
            "state": state,
            "instance_id": self.instance_id or None,
            "pid": int(record.pid) if record is not None and record.pid > 0 else None,
            "effective_mode": (
                ("sandbox" if record.sandboxed else "host")
                if record is not None else None
            ),
            "backend": record.backend if record is not None else None,
            "sandboxed": bool(record.sandboxed) if record is not None else False,
            "generation_id": None,
            "catalog_stale": self.catalog_stale,
            "error": str(self._failure) if self._failure is not None else None,
        }

    async def _start_impl(self) -> None:
        self._failure = None
        self.instance_id = f"{self.agent_id}-{uuid.uuid4().hex}"
        self.rpc_token = secrets.token_urlsafe(32)
        requested = self._requested_mode()
        from bianbu_agent.system_resources import system_resource_dir

        env = {
            "BIANBU_RUNTIME_ROLE": "agent-worker",
            "BIANBU_WORKER_INSTANCE_ID": self.instance_id,
            "BIANBU_WORKER_SOCKET": str(self.socket_path),
            "BIANBU_WORKER_AGENT_ID": self.agent_id,
            "BIANBU_WORKER_SECURITY_AGENT_ID": self.security_agent_id,
            "COM_BIANBU_AGENT_ID": self.agent_id,
            "BIANBU_SECURITY_SUBJECT": f"com.bianbu.agent_id:{self.agent_id}",
            "BIANBU_WORKER_RPC_TOKEN": self.rpc_token,
            "BIANBU_RUNTIME_DATA_DIR": str(self.config.data_dir.resolve()),
            "BIANBU_SYSTEM_RESOURCE_ROOT": str(system_resource_dir("plugins").parent),
            # These policy-allowlisted flags must cross the Runtime→Worker
            # boundary explicitly.  A direct system-service child does not
            # inherit the parent's environment like systemd-run did.
            "BIANBU_RUNTIME_MODE": os.environ.get("BIANBU_RUNTIME_MODE", "development"),
            "BIANBU_SECURITY": os.environ.get("BIANBU_SECURITY", "1"),
            "BIANBU_RUNTIME_SECURITY": os.environ.get("BIANBU_RUNTIME_SECURITY", "required"),
            # Feature-gated plugin disable list (run.sh --with). The worker owns
            # plugin loading, so this must cross the Runtime→Worker boundary.
            "BIANBU_DISABLED_PLUGINS": os.environ.get("BIANBU_DISABLED_PLUGINS", ""),
        }
        # Memory identity and mount coordinates are trusted Runtime-managed
        # values. Pass them through the Security policy so a sandbox Worker
        # sees the same UDS and registry paths as memoryd on the host.
        # In development mode, use local paths instead of system paths.
        development = str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() != "production"
        if development:
            memory_registry_dir = str(self.config.data_dir / "runtime" / "memory-registry")
            Path(memory_registry_dir).mkdir(parents=True, exist_ok=True)
        else:
            memory_registry_dir = "/run/bianbu/agent-runtime/memory-registry"

        memory_env_defaults = {
            "BIANBU_AGENT_MEMORY_ENABLED": (
                "false" if "bianbu.memory" in os.environ.get("BIANBU_DISABLED_PLUGINS", "").split(",")
                else "true"
            ),
            "BIANBU_AGENT_MEMORY_SOCKET": (
                "/tmp" if "bianbu.memory" in os.environ.get("BIANBU_DISABLED_PLUGINS", "").split(",")
                else "/run/bianbu/agent-memoryd.sock"
            ),
            "BIANBU_AGENT_MEMORY_REGISTRY_DIR": memory_registry_dir,
            "BIANBU_AGENT_MEMORY_ACCOUNT_ID": "default",
            "BIANBU_AGENT_MEMORY_AGENT_NAME": self.agent_id,
            "BIANBU_AGENT_MEMORY_REGISTRATION": "1",
        }
        for key, default in memory_env_defaults.items():
            value = os.environ.get(key, default)
            if value:
                env[key] = value.strip() if isinstance(value, str) else str(value)
            else:
                env[key] = default
        env["BIANBU_AGENT_MEMORY_AGENT_NAME"] = self.agent_id
        # Ask securityd to bind the private socket directory into a sandbox.
        # The path is generated by this trusted supervisor, not accepted from
        # HTTP, and is included in the immutable PrepareStart policy digest.
        socket_root = str(self.socket_root.resolve())
        self.socket_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_root, 0o700)
        # securityd validates every bind source before the child is spawned.
        # Create only Worker-owned state roots here; the signed Agent/config
        # files remain in the immutable data root and are never recreated.
        for relative in (
            Path("workspace"),
            Path("runtime") / "worker",
            Path("logs") / "provider-capture",
            Path("logs") / "operational",
        ):
            path = self.config.data_dir / relative
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass
        env["BIANBU_WORKER_SOCKET_TARGET"] = "/run/bianbu-agent/instances"
        env["BIANBU_WORKER_CONFIG_PATH"] = str(self.config.config_path or "")
        env["BIANBU_WORKER_CWD_TARGET"] = "/home/agent"
        # The control-plane configuration is the source of truth.  Environment
        # overrides remain useful for development, but the Worker must receive
        # the same installed client boundary and UDS selected by the manager.
        security_uds = os.environ.get("BIANBU_SEC_UDS", "").strip() or str(
            self.config.security.securityd_uds
        )
        client_dir = os.environ.get("BIANBU_SECURITY_CLIENT_DIR", "").strip() or str(
            self.config.security.client_dir
        )
        securityd_home = os.environ.get("BIANBU_SECURITYD_HOME", "").strip()
        if not securityd_home and self.config.security.securityd_home is not None:
            securityd_home = str(self.config.security.securityd_home)
        if security_uds:
            env["BIANBU_SEC_UDS"] = security_uds
        if client_dir:
            env["BIANBU_SECURITY_CLIENT_DIR"] = client_dir
        if securityd_home:
            env["BIANBU_SECURITYD_HOME"] = securityd_home
        bibit_intent_root = Path(
            os.environ.get("BIBIT_INTENT_ROOT", "/opt/bibit-intent")
        ).expanduser()
        if not bibit_intent_root.is_dir():
            bibit_intent_root = None
        # start_registered obtains the immutable command, mode, policy and
        # lease from the policy service. It also guarantees cgroup and backend proof.
        # Optional development runs intentionally use the fixed host-process
        # fallback.  This avoids asking systemd for a sandbox/cgroup on a
        # source checkout host that cannot provide one; required/production
        # deployments always take the managed path below.
        direct_dev_fallback = (
            self._allows_unmanaged_fallback(requested)
            and str(os.environ.get("BIANBU_RUNTIME_SECURITY", "required")).lower()
            == "optional"
        )
        # Keep the directory mounted even before the first temporary Skill is
        # added, so subsequent files become visible without replacing Workers.
        user_skills_root = Path.home() / ".bianbu-agent" / "skills"
        user_skills_root.mkdir(parents=True, exist_ok=True)
        python_runtime_target: Path | None = None
        python_executable = Path(sys.executable)
        if development and python_executable.is_symlink():
            link_target = Path(os.readlink(python_executable))
            if link_target.is_absolute():
                python_runtime_target = link_target.parent.parent
        try:
            if direct_dev_fallback:
                raise RuntimeError("development optional host fallback")
            record = await self.coordinator.start_registered(
                agent_id=self.security_agent_id,
                worker_agent_id=self.agent_id,
                requested_mode=requested,
                env=env,
                cwd=str(self.config.data_dir),
                instance_id=self.instance_id,
                worker_data_root=str(self.config.data_dir.resolve()),
                worker_socket_root=socket_root,
                worker_config_path=str(self.config.config_path or ""),
                security_uds=security_uds,
                securityd_home=securityd_home,
                memory_uds=(env["BIANBU_AGENT_MEMORY_SOCKET"]
                            if env.get("BIANBU_AGENT_MEMORY_ENABLED", "true").lower()
                            in {"1", "true", "yes", "on"} else "/tmp"),
                memory_registry_dir=env["BIANBU_AGENT_MEMORY_REGISTRY_DIR"],
                bibit_intent_root=bibit_intent_root,
                runtime_source_root=(Path(__file__).resolve().parents[3]
                                     if development else None),
                python_runtime_root=(Path(sys.executable).resolve().parents[1]
                                     if development else None),
                python_runtime_target=python_runtime_target,
                user_skills_root=user_skills_root,
                fallback_entrypoint=self._entrypoint(),
                before_commit=self._attest_worker,
            )
            await self._audit_event(
                "runtime.worker_spawn", self.instance_id,
                f"pid={record.pid};backend={record.backend}",
            )
            # The security policy may allow only a fixed environment allowlist.
            # Values required for Hello are not user secrets and are injected
            # by the trusted supervisor, so fail closed if policy strips them.
            env.update({
                "BIANBU_WORKER_LEASE_ID": record.lease_id,
                "BIANBU_WORKER_EFFECTIVE_MODE": "sandbox" if record.sandboxed else "host",
                "BIANBU_WORKER_BACKEND": record.backend,
            })
        except Exception as exc:
            # In development without the policy service, allow the explicit fixed
            # entrypoint to be used for protocol/API testing. Production never
            # reaches this branch: even an accidentally inherited development
            # override must not disable the signed Runtime manifest gate.
            if not self._allows_unmanaged_fallback(requested):
                raise
            logger.warning(
                "security.runtime 受管 Worker 启动失败，开发态降级为 unmanaged "
                "host-process（仅用于测试）：agent_id=%s instance_id=%s "
                "mode=development backend=host-process error=%s",
                self.agent_id, self.instance_id, exc)
            record = await self._start_development_process(env)
        self.record = record
        try:
            # _attest_worker already completed Hello before CommitStart.  Keep
            # the same client for subsequent RPC and only use a fresh health
            # probe if the backend replaced it.
            if self.client is None:
                self.client = WorkerRPCClient(self.socket_path, self.instance_id,
                                              token=self.rpc_token,
                                              timeout=self.config.runtime.control.handshake_timeout_seconds)
            # The managed path has already attested in ``before_commit``;
            # development fallback may still be between exec and UDS listen.
            # Use the bounded retry path for both so a normal startup race is
            # not reported as a missing socket.
            hello = await self._wait_hello()
            try:
                self._verify_hello(
                    hello, record, self._decision_from_record(record),
                )
            except Exception as exc:
                await self._audit_event(
                    "runtime.policy_mismatch", self.instance_id, str(exc),
                )
                raise
            self.identity = WorkerIdentity(
                agent_id=self.agent_id, instance_id=self.instance_id,
                lease_id=record.lease_id,
                effective_mode="sandbox" if record.sandboxed else "host",
                backend=record.backend, sandboxed=record.sandboxed,
                container_id=record.container_id, pid=record.pid,
                peer_pid=int(self.client.last_peer_pid or 0),
                generation_id=(int(hello["generation_id"]) if hello.get("generation_id") is not None else None),
            )
            if record.lease_id != "dev" and self._memory_registration_path is not None:
                from bianbu_agent.server.hook_memory_registration import (
                    HookMemoryRegistrationServer, command_registration_socket,
                )
                self._hook_memory_registration = HookMemoryRegistrationServer(
                    command_registration_socket(self.socket_path),
                    worker_pid=record.pid,
                    registration_path=self._memory_registration_path,
                )
                await self._hook_memory_registration.start()
            activated = await self.client.activate()
            if str(activated.get("status", "")) != "active":
                raise RuntimeError("Worker activation failed")
            if self.identity is not None:
                self.identity = replace(
                    self.identity,
                    generation_id=(int(activated["generation_id"])
                                   if activated.get("generation_id") is not None else None),
                )
            self._ready.set()
            self.catalog_stale = False
            await self._audit_event("runtime.worker_hello", self.instance_id, "success")
            if self.metrics is not None:
                self.metrics.set("runtime_worker_instances", 1,
                                 state="running", mode=record.backend)
            self._health_task = asyncio.create_task(
                self._health_loop(), name="bianbu-worker-health")
        except Exception:
            await self.stop(reason="Worker hello/attestation failed")
            raise

    def _sample_cgroup_memory(self) -> None:
        """记下 scope 的内存记账，供 OOM 归因使用。"""

        record = self.record
        if record is None or not record.cgroup_path:
            return
        sample = read_cgroup_memory(record.cgroup_path)
        if sample is not None:
            self._cgroup_memory = sample

    def memory_limit_reason(self) -> str | None:
        """scope 触顶内存限额时返回可读原因，否则 None。"""

        record = self.record
        if record is None:
            return None
        sample = read_cgroup_memory(record.cgroup_path) or self._cgroup_memory
        return memory_limit_evidence(
            sample, unit=str(record.unit or record.cgroup_path or "")
        )

    async def _health_loop(self) -> None:
        interval = self.config.runtime.control.heartbeat_interval_seconds
        timeout = self.config.runtime.control.heartbeat_timeout_seconds
        while not self._closed and self.ready:
            await asyncio.sleep(interval)
            if not self.ready or self.client is None:
                return
            try:
                self._last_health = await asyncio.wait_for(
                    self.client.health(), timeout=timeout)
                self._sample_cgroup_memory()
                if str(self._last_health.get("status", "")) != "running":
                    raise RuntimeError("Worker health status is not running")
                if self._memory_registration is not None:
                    self._memory_registration.maintain()
                    self._last_health["memory_registration"] = self._memory_registration.status()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - health boundary
                manager = getattr(self, "run_manager", None)
                has_active = getattr(manager, "has_active_for", None)
                if isinstance(exc, TimeoutError) and callable(has_active) and has_active(self):
                    logger.warning(
                        "Worker heartbeat timed out during an active Run; deferring failure"
                    )
                    continue
                failure = exc if str(exc).strip() else RuntimeError(type(exc).__name__)
                self._failure = failure
                if self.metrics is not None:
                    self.metrics.inc("runtime_worker_heartbeat_failures_total")
                logger.error("Worker health check failed: %s", failure)
                await self._handle_failure(failure)
                return

    async def _handle_failure(self, error: Exception) -> None:
        if self._closed:
            return
        # 心跳断开通常只剩一句 EOFError；scope 的内存记账能把它变成可读原因。
        evidence = self.memory_limit_reason()
        reason = str(error).strip() or type(error).__name__
        if evidence:
            reason = f"{evidence}（{type(error).__name__}: {error}）"
            self._failure = RuntimeError(reason)
        await self._audit_event("runtime.worker_crash", self.instance_id, reason)
        # Do this before stopping/restarting so every control-plane Run gets a
        # durable terminal result instead of waiting on a dead stream.
        manager = getattr(self, "run_manager", None)
        if manager is not None:
            try:
                await manager.fail_active(reason, supervisor=self)
            except TypeError:
                await manager.fail_active(reason)
        await self.stop(reason=f"Worker health failure: {reason}")
        policy = self.config.runtime.control.restart_policy
        if not policy.enabled or self._restart_count >= policy.max_restarts:
            return
        backoff = policy.backoff_seconds[min(
            self._restart_count, len(policy.backoff_seconds) - 1)]
        self._restart_count += 1
        if self.metrics is not None:
            self.metrics.inc("runtime_worker_restarts_total", reason="health_failure")
        await asyncio.sleep(backoff)
        if not self._closed:
            try:
                await self.start()
            except Exception as exc:  # noqa: BLE001
                logger.error("Worker restart failed: %s", exc)

    async def _start_development_process(self, env: Mapping[str, str]) -> ExecutionRecord:
        if not self.rpc_token:
            self.rpc_token = secrets.token_urlsafe(32)
        if not self.instance_id:
            self.instance_id = f"{self.agent_id}-{uuid.uuid4().hex}"
        env = {**dict(env), "BIANBU_WORKER_RPC_TOKEN": self.rpc_token}
        command = self._entrypoint()
        socket_display = self.socket_path if self.instance_id else "<pending>"
        logger.info(
            "启动开发态 unmanaged Worker：instance=%s socket=%s",
            self.instance_id, socket_display,
        )
        # A signed manifest stores the canonical sandbox config path.  The
        # explicit unmanaged fallback is a host process, so translate only
        # that canonical argument to the trusted local config file; never
        # translate arbitrary HTTP-provided argv values.
        config_path = str(self.config.config_path or "")
        config_argument = _sandbox_config_argument(
            Path(config_path), Path(self.config.data_dir)
        )
        command = tuple(
            config_path if config_path and item in {config_argument, _SANDBOX_CONFIG}
            else item
            for item in command
        )
        # Configs in a source checkout commonly use ``.venv/bin/bianbu-agent``;
        # resolve that executable before spawning so a caller's working
        # directory cannot make the fallback silently exit with no socket.
        if command:
            executable = Path(command[0])
            if not executable.is_absolute():
                candidate = (Path.cwd() / executable).resolve()
                if candidate.exists():
                    command = (str(candidate), *command[1:])
        # The unmanaged path is intentionally development-only, but it still
        # uses the same Worker Hello contract as a managed launch.  Populate a
        # synthetic, non-authorizing lease marker before spawning so the
        # Worker cannot report an empty lease while the supervisor records
        # ``dev``.  This keeps the fallback diagnosable and prevents a false
        # attestation mismatch; production never enters this branch.
        child_env = {**os.environ, **dict(env)}
        child_env.setdefault("BIANBU_WORKER_LEASE_ID", "dev")
        child_env.setdefault("BIANBU_WORKER_EFFECTIVE_MODE", "host")
        child_env.setdefault("BIANBU_WORKER_BACKEND", "host-process")
        # This Worker is deliberately an unmanaged development fallback.  It
        # must not run the direct-worker security.runtime bootstrap below:
        # that bootstrap asks securityd for a new sandbox lease, while this
        # process is intentionally the host-process fallback after that
        # request already failed.
        child_env["BIANBU_WORKER_UNMANAGED_FALLBACK"] = "1"
        child_env["BIANBU_AGENT_ID"] = self.agent_id
        # The control plane may itself be launched inside the outer Runtime bwrap.
        # An explicitly unmanaged development Worker is still recorded as a
        # host-process fallback (no security.runtime lease), so do not inherit
        # the control-plane marker and make its Hello claim sandboxed=True.
        child_env["BIANBU_SANDBOX_ACTIVE"] = "0"
        # Redirect the unmanaged Worker's stdout/stderr to a dev log so startup
        # failures are diagnosable without a managed systemd journal.
        worker_log = self.config.data_dir / "runtime" / "worker-dev.log"
        worker_log.parent.mkdir(parents=True, exist_ok=True)
        with worker_log.open("a") as log_file:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(Path.cwd()), env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
            )
        self._development_process = process
        await asyncio.sleep(0.05)
        if process.returncode is not None:
            logger.warning("开发态 Worker 立即退出：returncode=%s，日志=%s",
                           process.returncode, worker_log)
        logger.info("Worker process started with PID: %s", process.pid)
        return ExecutionRecord(
            instance_id=self.instance_id, agent_id=self.agent_id,
            backend="host-process", pid=int(process.pid), cgroup_path="", unit="",
            state="RUNNING", lease_id="dev", started_at=0.0,
            sandboxed=False, container_id="",
        )

    async def _wait_hello(self) -> dict[str, Any]:
        assert self.client is not None
        deadline = asyncio.get_running_loop().time() + self.config.runtime.control.handshake_timeout_seconds
        last: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await self.client.hello()
            except Exception as exc:  # noqa: BLE001
                last = exc
                await asyncio.sleep(0.05)
        raise RuntimeError(f"Worker hello timeout: {last}")

    async def _attest_worker(self, handle, record: ExecutionRecord, decision) -> None:
        # The mode comes *from* securityd, so there is nothing for the Runtime
        # to reconcile here; the lease records the decision that was applied.
        """Wait for Hello before consuming the one-shot security lease."""
        self.client = WorkerRPCClient(
            self.socket_path, self.instance_id,
            token=self.rpc_token,
            timeout=self.config.runtime.control.handshake_timeout_seconds,
        )
        hello = await self._wait_hello()
        self._verify_hello(hello, record, decision, allow_namespace_pid=True)
        peer_pid = int(self.client.last_peer_pid or 0)
        if peer_pid <= 0:
            raise RuntimeError("Worker RPC did not provide peer PID")
        try:
            peer_cgroup = self.coordinator.current_cgroup(peer_pid)
        except AttributeError:
            from bianbu_agent.security.security_runtime import current_cgroup_path
            peer_cgroup = current_cgroup_path(peer_pid)
        if str(peer_cgroup).rstrip("/") != str(handle.cgroup_path).rstrip("/"):
            raise RuntimeError("Worker RPC peer cgroup mismatch")
        # For bwrap, BackendHandle.pid may be the host-side bwrap launcher;
        # CommitStart must bind the actual host PID owning the RPC socket.
        handle.pid = peer_pid
        handle.cgroup_path = peer_cgroup
        record.pid = peer_pid
        record.cgroup_path = peer_cgroup
        try:
            from bianbu_agent.server.memory_registry import register as register_memory_runtime

            self._memory_registration_path = register_memory_runtime(
                agent_name=self.agent_id,
                pid=peer_pid,
            )
            if self._memory_registration_path is not None:
                from bianbu_agent.server.memory_registry import MemoryRegistration
                self._memory_registration = MemoryRegistration(self._memory_registration_path)
        except Exception as exc:  # noqa: BLE001 - fail startup if memory registration is broken
            raise RuntimeError(f"Worker AgentMemory host registration failed: {exc}") from exc

    @staticmethod
    def _decision_from_record(record: ExecutionRecord):
        return type("Decision", (), {
            "effective_mode": "sandbox" if record.sandboxed else "host",
            "backend": record.backend,
        })()

    def _verify_hello(self, hello: Mapping[str, Any], record: ExecutionRecord,
                      decision, *, allow_namespace_pid: bool = False) -> None:
        expected = {
            "agent_id": self.agent_id, "instance_id": self.instance_id,
            "lease_id": record.lease_id,
            "effective_mode": decision.effective_mode,
            "backend": decision.backend,
        }
        for key, value in expected.items():
            if value and str(hello.get(key) or "") != str(value):
                raise RuntimeError(f"Worker hello {key} mismatch")
        reported_pid = int(hello.get("pid") or 0)
        if reported_pid <= 0:
            raise RuntimeError("Worker hello pid invalid")
        # A bwrap worker reports the PID from its private PID namespace.  The
        # host PID is the authenticated SO_PEERCRED/cgroup PID recorded by the
        # backend; never compare namespace-local PID to the host PID.
        if not record.sandboxed and reported_pid != int(record.pid):
            raise RuntimeError("Worker hello pid mismatch")
        peer_pid = int(self.client.last_peer_pid or 0) if self.client else 0
        if peer_pid <= 0:
            raise RuntimeError("Worker RPC peer pid mismatch")
        if not allow_namespace_pid and peer_pid != int(record.pid):
            raise RuntimeError("Worker RPC peer pid mismatch")
        if bool(hello.get("sandboxed")) != bool(record.sandboxed):
            raise RuntimeError("Worker hello sandboxed mismatch")
        if record.sandboxed and not str(hello.get("container_id") or record.container_id):
            raise RuntimeError("Worker hello container_id missing")

    async def stop(self, *, reason: str = "requested", strict: bool = False) -> None:
        self._ready.clear()
        if self.metrics is not None:
            self.metrics.set("runtime_worker_instances", 0,
                             state="stopped", mode="unknown")
        health_task, self._health_task = self._health_task, None
        if health_task is not None and health_task is not asyncio.current_task():
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.shutdown()
            except Exception:
                pass
        if self.record is not None:
            instance_id = self.record.instance_id
            if self.record.lease_id == "dev":
                await self._stop_development_process()
            else:
                try:
                    await self.coordinator.stop(self.record.instance_id, reason=reason)
                except Exception:
                    logger.exception("Worker stop failed")
                    if strict:
                        raise
            await self._audit_event("runtime.worker_stop", instance_id, reason)
        if self._hook_memory_registration is not None:
            await self._hook_memory_registration.close()
            self._hook_memory_registration = None
        if self._memory_registration_path is not None:
            try:
                from bianbu_agent.server.memory_registry import unregister as unregister_memory_runtime

                if self._memory_registration is not None:
                    self._memory_registration.close()
                else:
                    unregister_memory_runtime(self._memory_registration_path)
            except Exception:
                logger.exception("Worker AgentMemory registration cleanup failed")
            self._memory_registration_path = None
            self._memory_registration = None
        self.record = None
        self.identity = None

    async def _stop_development_process(self) -> None:
        process = self._development_process
        if process is None or process.returncode is not None:
            self._development_process = None
            return
        try:
            process.terminate()
        except ProcessLookupError:
            await process.wait()
            self._development_process = None
            return
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self.coordinator.stop_timeout,
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await asyncio.wait_for(
                process.wait(), timeout=max(0.1, self.coordinator.stop_timeout),
            )
        self._development_process = None

    async def close(self, *, strict: bool = False) -> None:
        self._closed = True
        await self.stop(reason="control runtime close", strict=strict)
        await self.coordinator.close()


class AgentWorkerPool:
    """Own one Worker process supervisor per Agent security boundary."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        metrics: object | None = None,
    ) -> None:
        self.config = config
        self.metrics = metrics
        self._supervisors: dict[str, WorkerSupervisor] = {}
        self._agent_locks: dict[str, asyncio.Lock] = {}
        self._warmups: dict[str, asyncio.Task[None]] = {}
        self._warmup_stop = asyncio.Event()
        self._retired_agents: set[str] = set()
        self.run_manager: object | None = None

    def warm_agent(self, agent_id: str) -> None:
        """Prepare a selected expert without delaying control or session creation.

        A failed prepare is retried under ``runtime.control.restart_policy``.
        Startup can lose a race against the boot clock step (an already issued
        launch lease is judged expired once the wall clock jumps), and a single
        transient failure must not leave the device without a Worker until an
        operator restarts the Agent by hand.
        """
        if agent_id in self._warmups or self._warmup_stop.is_set():
            return

        normalized = self._normalize(agent_id)

        async def warm() -> None:
            policy = self.config.runtime.control.restart_policy
            attempt = 0
            try:
                while True:
                    try:
                        async with self.agent_lock(normalized):
                            await self.for_agent(normalized).start()
                    except Exception as exc:
                        if self._warmup_stop.is_set():
                            logger.info(
                                "Agent Worker 预热在关闭时放弃：agent_id=%s error=%s",
                                normalized, exc,
                            )
                            return
                        if (
                            not policy.enabled
                            or attempt >= policy.max_restarts
                            or normalized in self._retired_agents
                        ):
                            logger.error(
                                "Agent Worker 预热失败：agent_id=%s attempts=%s error=%s",
                                normalized, attempt + 1, exc,
                            )
                            if self.metrics is not None:
                                self.metrics.inc(
                                    "runtime_worker_restarts_total",
                                    reason="warmup_failure",
                                )
                            return
                        backoff = policy.backoff_seconds[
                            min(attempt, len(policy.backoff_seconds) - 1)]
                        attempt += 1
                        logger.warning(
                            "Agent Worker 预热失败，%.1fs 后重试（第 %s/%s 次）：agent_id=%s error=%s",
                            backoff, attempt, policy.max_restarts, normalized, exc,
                        )
                        if self.metrics is not None:
                            self.metrics.inc(
                                "runtime_worker_restarts_total", reason="warmup_retry")
                        try:
                            await asyncio.wait_for(
                                self._warmup_stop.wait(), timeout=backoff)
                        except TimeoutError:
                            continue
                        return
                    if attempt:
                        logger.info(
                            "Agent Worker 预热重试成功：agent_id=%s attempts=%s",
                            normalized, attempt + 1,
                        )
                    return
            finally:
                self._warmups.pop(agent_id, None)

        self._warmups[agent_id] = asyncio.create_task(warm(), name=f"worker-warmup-{agent_id}")

    def agent_lock(self, agent_id: str) -> asyncio.Lock:
        return self._agent_locks.setdefault(self._normalize(agent_id), asyncio.Lock())

    def agent_busy(self, agent_id: str) -> bool:
        return any(
            handle.agent_id == agent_id
            for handle in getattr(self.run_manager, "_active", {}).values()
        )

    def _normalize(self, agent_id: str | None) -> str:
        normalized = str(agent_id or "").strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        return normalized

    def for_agent(self, agent_id: str | None,
                  requested_mode: str | None = None) -> WorkerSupervisor:
        normalized = self._normalize(agent_id)
        if normalized in self._retired_agents:
            raise RuntimeError(f"Agent Worker was deleted: {normalized}")
        supervisor = self._supervisors.get(normalized)
        if supervisor is None:
            supervisor = WorkerSupervisor(
                self.config,
                agent_id=normalized,
                metrics=self.metrics,
                requested_mode=requested_mode,
            )
            if self.run_manager is not None:
                supervisor.run_manager = self.run_manager
            self._supervisors[normalized] = supervisor
        elif requested_mode:
            supervisor.requested_mode = str(requested_mode).strip().lower()
        return supervisor

    @property
    def ready(self) -> bool:
        return any(supervisor.ready for supervisor in self._supervisors.values())

    async def catalogs(self) -> list[dict[str, object]]:
        """Observe ready Workers without starting one or letting one block peers."""
        async def read(agent_id, supervisor):
            try:
                async with asyncio.timeout(2.0):
                    report = await supervisor.client.catalog()
                return {"agent_id": agent_id, "catalog": report}
            except Exception as exc:
                return {"agent_id": agent_id, "error": str(exc) or type(exc).__name__}

        return list(await asyncio.gather(*(
            read(agent_id, worker)
            for agent_id, worker in tuple(self._supervisors.items())
            if agent_id not in self._retired_agents and worker.ready
        )))

    async def reload_installed(self, *, force: bool = False) -> dict[str, object]:
        supervisors = tuple(worker for agent_id, worker in self._supervisors.items()
                            if agent_id not in self._retired_agents and worker.ready)
        responses: list[dict[str, object]] = []
        failures: list[dict[str, str]] = []
        for supervisor in supervisors:
            client = supervisor.client
            if client is None:
                supervisor.catalog_stale = True
                failures.append({
                    "agent_id": supervisor.agent_id,
                    "error": "Worker RPC client is not ready",
                })
                continue
            try:
                response = (
                    await client.reload(force=True)
                    if force
                    else await client.reload()
                )
            except Exception as exc:  # noqa: BLE001 - isolate Worker RPC failures
                supervisor.catalog_stale = True
                failures.append({
                    "agent_id": supervisor.agent_id,
                    "error": str(exc) or type(exc).__name__,
                })
                continue
            supervisor.catalog_stale = False
            generation_id = response.get("generation_id")
            identity = getattr(supervisor, "identity", None)
            if generation_id is not None and identity is not None:
                supervisor.identity = replace(
                    identity,
                    generation_id=int(generation_id),
                )
            responses.append(dict(response))
        latest = responses[-1] if responses else {}
        latest["worker_count"] = len(supervisors)
        latest["reloaded_worker_count"] = len(responses)
        latest["failures"] = failures
        return latest

    async def apply_network_learning_policy(
        self, agent_id: str, mode: str, version: int,
    ) -> dict[str, int]:
        totals = {"active_instances": 0, "collectors_started": 0,
                  "collectors_stopped": 0, "degraded_instances": 0}
        for supervisor in tuple(self._supervisors.values()):
            if str(supervisor.agent_id) != str(agent_id):
                continue
            result = await supervisor.coordinator.apply_network_learning_policy(
                str(supervisor.security_agent_id), str(mode), int(version),
            )
            for key in totals:
                totals[key] += int(result.get(key, 0))
        return totals

    async def remove_agent(self, agent_id: str) -> bool:
        """Stop and forget the Worker owned by a deleted logical Agent."""
        normalized = self._normalize(agent_id)
        self._retired_agents.add(normalized)
        supervisor = self._supervisors.get(normalized)
        if supervisor is None:
            return False
        await supervisor.close(strict=True)
        if self._supervisors.get(normalized) is supervisor:
            self._supervisors.pop(normalized, None)
        return True

    def activate_agent(self, agent_id: str, requested_mode: str | None = None) -> None:
        """Allow a newly created Agent ID to acquire a fresh Worker."""
        self._retired_agents.discard(self._normalize(agent_id))
        if requested_mode:
            self.for_agent(agent_id, requested_mode=requested_mode)

    def status_snapshot(self) -> dict[str, Any]:
        workers = {
            agent_id: supervisor.status_snapshot()
            for agent_id, supervisor in sorted(self._supervisors.items())
        }
        return {
            "state": "running" if any(
                item.get("state") == "running" for item in workers.values()
            ) else "idle",
            "workers": workers,
            "worker_count": len(workers),
        }

    async def close(self) -> None:
        warmups = tuple(self._warmups.values())
        # A backend spawn may be running in a thread. Let startup settle so
        # its handle is registered before closing; cancellation could orphan it.
        # Retries stop between attempts, so shutdown never waits out the full
        # backoff schedule of a Worker that cannot be prepared anyway.
        self._warmup_stop.set()
        await asyncio.gather(*warmups, return_exceptions=True)
        self._warmups.clear()
        supervisors = tuple(self._supervisors.values())
        self._supervisors.clear()
        await asyncio.gather(
            *(supervisor.close() for supervisor in supervisors),
            return_exceptions=True,
        )


class ControlRunManager:
    """RunManager-compatible control-plane facade backed by Worker RPC."""

    def __init__(
        self,
        store: object,
        supervisor: WorkerSupervisor | AgentWorkerPool,
        *,
        on_idle=None,
    ) -> None:
        from bianbu_agent._internal.change_signal import AsyncRevisionSignal

        self._store = store
        self._supervisor = supervisor
        if isinstance(supervisor, AgentWorkerPool):
            supervisor.run_manager = self
        self._run_supervisors: dict[str, WorkerSupervisor] = {}
        self._dispatching: set[str] = set()
        self._accepted: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._active: dict[str, RunHandle] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self._waiting: set[str] = set()
        self._changes = AsyncRevisionSignal()
        self._on_idle = on_idle

    @property
    def change_revision(self) -> int: return self._changes.revision
    async def wait_for_change(self, after: int) -> int:
        return await self._changes.wait(after)

    @property
    def active_count(self) -> int: return len(self._active)
    @property
    def waiting_count(self) -> int: return len(self._waiting)

    def _worker_for(self, agent_id: str | None) -> WorkerSupervisor:
        selector = getattr(self._supervisor, "for_agent", None)
        if callable(selector):
            return selector(agent_id)
        return self._supervisor

    def _worker_for_run(self, run_id: str) -> WorkerSupervisor | None:
        return self._run_supervisors.get(run_id)

    def has_active_for(self, supervisor: WorkerSupervisor) -> bool:
        return any(
            self._run_supervisors.get(run_id) is supervisor
            for run_id in self._active
        )

    async def run(self, session_id: str, prompt: str | Sequence[SessionInboxMessage], *, agent_id: str | None = None,
                  account_id: str | None = None,
                  model: object | str | None = None, request_id: str | None = None,
                  idempotency_key: str | None = None, idempotency_fingerprint: str | None = None,
                  metadata: Mapping[str, object] | None = None, thread_id: str | None = None) -> RunHandle:
        if self._closing: raise RuntimeError("RunManager is closing")
        selected_account_id = (
            _default_account_id() if account_id is None else str(account_id).strip()
        )
        if not selected_account_id:
            raise ValueError("account_id must not be empty")
        supervisor = self._worker_for(agent_id)
        run_id = str(request_id or uuid.uuid4())
        generation_id = int(
            getattr(getattr(supervisor, "identity", None), "generation_id", 0) or 0
        )
        handle = RunHandle(request_id=run_id, session_id=str(session_id), store=self._store,
                           control=lambda action, payload: self._control(run_id, action, payload),
                           agent_id=str(agent_id or ""),
                           generation_id=generation_id,
                           on_committed=self._changes.notify)
        prompt_text = (
            prompt if isinstance(prompt, str)
            else "\n\n".join(message.content for message in prompt)
        )
        request = {
            "prompt": prompt_text, "idempotency_key": str(idempotency_key or ""),
            "idempotency_fingerprint": str(idempotency_fingerprint or ""),
            "agent_id": str(agent_id or ""), "account_id": selected_account_id,
            "metadata": dict(metadata or {}),
            "generation_id": generation_id,
            "thread_id": str(thread_id or session_id),
        }
        if not isinstance(prompt, str):
            request["input_messages"] = [
                inbox_message_to_dict(message) for message in prompt
            ]
            request["skill_ids"] = list(dict.fromkeys(
                skill_id
                for message in prompt
                for skill_id in message.skill_ids
            ))
        await handle.start(request)
        self._active[run_id] = handle
        self._run_supervisors[run_id] = supervisor
        with bind_log_context(
            agent_id=str(agent_id or ""),
            session_id=str(session_id),
            run_id=run_id,
            plugin_generation=generation_id,
        ):
            task = asyncio.create_task(
                self._stream(
                    handle, supervisor, prompt, agent_id, idempotency_key,
                    idempotency_fingerprint, metadata, selected_account_id,
                    thread_id=str(thread_id or session_id),
                ),
                name=f"worker-run-{run_id}",
            )
        self._tasks[run_id] = task
        return handle

    async def _stream(self, handle: RunHandle, supervisor: WorkerSupervisor,
                      prompt: str | Sequence[SessionInboxMessage], agent_id: str | None,
                      idempotency_key: str | None, idempotency_fingerprint: str | None,
                      metadata: Mapping[str, object] | None, account_id: str, *,
                      thread_id: str) -> None:
        try:
            if handle._finished.is_set():
                return
            if not supervisor.ready:
                await handle.publish(RunEvent("progress", {"phase": "starting_agent"}))
                await supervisor.start()
                if handle._finished.is_set() or handle.request_id in self._cancel_requested:
                    return
                handle.generation_id = int(getattr(supervisor.identity, "generation_id", 0) or 0)
                await handle.publish(RunEvent("progress", {
                    "phase": "preparing_request", "generation_id": handle.generation_id,
                }))
            assert supervisor.client is not None
            if getattr(supervisor, "catalog_stale", False):
                refreshed = await supervisor.client.reload()
                supervisor.catalog_stale = False
                generation_id = refreshed.get("generation_id")
                if generation_id is not None:
                    handle.generation_id = int(generation_id)
                    if supervisor.identity is not None:
                        supervisor.identity = replace(
                            supervisor.identity,
                            generation_id=int(generation_id),
                        )
            flush_session = getattr(self._store, "flush_session", None)
            async def event(message: Mapping[str, Any]) -> None:
                kind = message.get("type")
                if kind == "run.accepted":
                    self._accepted.add(handle.request_id)
                    if handle.request_id in self._cancel_requested:
                        await supervisor.client.cancel(handle.request_id)
                elif kind == "run.event":
                    raw = message.get("event") or {}
                    run_event = RunEvent(
                        str(raw.get("kind") or "error"),
                        dict(raw.get("payload") or {}),
                    )
                    await handle.publish(run_event)
                    if callable(flush_session) and run_event.kind in {
                        "tool_start", "tool_end",
                    }:
                        await flush_session(handle.session_id)
                elif kind == "run.result":
                    result = decode_result(dict(message.get("result") or {}))
                    await handle.finish(result)
            if callable(flush_session):
                await flush_session(handle.session_id)
            payload = {
                "run_id": handle.request_id, "session_id": handle.session_id,
                "thread_id": thread_id,
                "agent_id": agent_id or "", "prompt": (
                    prompt if isinstance(prompt, str)
                    else "\n\n".join(message.content for message in prompt)
                ),
                "account_id": account_id,
                "idempotency_key": idempotency_key or "",
                "idempotency_fingerprint": idempotency_fingerprint or "",
                "metadata": dict(metadata or {}),
            }
            if not isinstance(prompt, str):
                payload["input_messages"] = [
                    inbox_message_to_dict(message) for message in prompt
                ]
            if handle._finished.is_set() or handle.request_id in self._cancel_requested:
                return
            self._dispatching.add(handle.request_id)
            await supervisor.client.run_start(payload, event)
        except Exception as exc:
            if not handle._finished.is_set():
                # Worker 自己能给出归因时（错误帧、RPC 错误）优先用它：一律
                # 盖成 worker_rpc_error 会把「谁失败了、为什么失败」抹平成一个
                # 泛化代码，客户端只剩一句通用文案。
                if isinstance(exc, WorkerRPCError):
                    error = RunError(
                        code=exc.code,
                        message=str(exc) or type(exc).__name__,
                        details=exc.details,
                    )
                else:
                    # Worker 被 OOM 杀掉时连接只是断开，Runtime 若原样上报就
                    # 只剩 worker_rpc_error / EOFError，定位全靠翻 dmesg。
                    probe = getattr(supervisor, "memory_limit_reason", None)
                    evidence = (
                        probe()
                        if isinstance(exc, _WORKER_CONNECTION_LOST)
                        and callable(probe) else None
                    )
                    details: dict[str, object] = {}
                    if evidence:
                        details["exception"] = type(exc).__name__
                        record = getattr(supervisor, "record", None)
                        if record is not None:
                            details["unit"] = str(record.unit or "")
                    error = RunError(
                        code="worker_oom_killed" if evidence else "worker_rpc_error",
                        message=evidence or str(exc) or type(exc).__name__,
                        details=details,
                    )
                if error.code == 'worker_rpc_error' and (
                    isinstance(exc, _WORKER_CONNECTION_LOST)
                    or (isinstance(exc, WorkerRPCError) and str(exc) == 'EOFError')
                ):
                    from bianbu_agent.server.maintenance import upgrade_snapshot

                    upgrade = await upgrade_snapshot()
                    if upgrade['maintenance']:
                        error = RunError(
                            code='runtime_upgrading',
                            message='Runtime 自升级导致连接中断；本轮请求已中断，系统升级结果请在重连后查询。',
                            details={**dict(error.details or {}), 'exception': type(exc).__name__, 'upgrade': upgrade},
                        )
                try:
                    await handle.publish(RunEvent("error", encode_error(error)))
                except Exception:
                    pass
                await handle.finish(RunResult(
                    request_id=handle.request_id,
                    status="failed",
                    error=error,
                ))
        finally:
            result = handle._result
            if result is not None:
                log_run_terminal(
                    logger,
                    status=result.status,
                    error_code=(result.error.code if result.error is not None else ""),
                )
            self._dispatching.discard(handle.request_id)
            self._accepted.discard(handle.request_id)
            self._cancel_requested.discard(handle.request_id)
            self._active.pop(handle.request_id, None)
            self._tasks.pop(handle.request_id, None)
            self._run_supervisors.pop(handle.request_id, None)
            if not self._active and callable(self._on_idle):
                self._on_idle()

    async def _control(self, run_id: str, action: str, payload: Mapping[str, object]) -> object:
        supervisor = self._worker_for_run(run_id)
        if action == "cancel":
            handle = self._active.get(run_id)
            if handle is None or handle._finished.is_set():
                return False
            self._cancel_requested.add(run_id)
            if run_id not in self._dispatching:
                # Keep the shared Worker startup alive for other sessions, but
                # never send this cancelled input once it becomes ready.
                await handle.finish(RunResult(request_id=run_id, status="cancelled"))
                return True
            if run_id not in self._accepted:
                # The Worker has not registered the Run yet. Send cancellation
                # when its acceptance arrives instead of losing it in that gap.
                return True
            if supervisor is None or supervisor.client is None:
                return False
            return await supervisor.client.cancel(run_id)
        if supervisor is None or supervisor.client is None: return False
        if action in {"approve", "reject", "answer"}:
            key = "approval_id" if action != "answer" else "question_id"
            return await supervisor.client.approval(run_id, action, {key: payload.get(key), "reason": payload.get("reason", ""), "answer": payload.get("answer", "")})
        return False

    async def get(self, request_id: str) -> RunHandle | None:
        if request_id in self._active: return self._active[request_id]
        stored = await self._store.get(request_id)
        return None if stored is None else await RunHandle.restore(stored, store=self._store)

    async def get_record(self, request_id: str) -> object | None:
        stored = await self._store.get(request_id)
        if stored is None: return None
        # An active control-plane Run is still being streamed by the Worker.
        # Its durable record legitimately has no terminal result yet; do not
        # pass it through RunHandle.restore(), whose purpose is to reconcile
        # an orphan left by a process restart and therefore marks an unfinished
        # record as ``run_interrupted``. Calling that path while the Session
        # summary is reconciling races the worker task and can make the UI show
        # "发送失败" even though the Worker is about to produce a response.
        if request_id in self._active:
            return stored
        if getattr(stored, "result", None) is None:
            handle = await RunHandle.restore(stored, store=self._store)
            return await self._store.get(request_id)
        return stored

    async def list(self, session_id: str | None = None) -> list[object]: return await self._store.list(session_id=session_id)
    async def delete_many(self, request_ids: list[str] | tuple[str, ...]) -> bool:
        if any(item in self._active for item in request_ids): return False
        records = await asyncio.gather(*(self._store.get(item) for item in request_ids))
        if any(item is None for item in records): return False
        for item in request_ids: await self._store.delete(item)
        self._changes.notify()
        return True
    async def delete_session(self, session_id: str) -> int:
        # Cancel and then *join* the in-flight Run streams before the store
        # drops the Session.  A stream that lands after the Run projection is
        # gone can only report an unknown request_id, which surfaced as an
        # unretrieved ``KeyError`` in the control plane and as a failed Run.
        tasks = [
            task for run_id, task in self._tasks.items()
            if run_id in self._active
            and self._active[run_id].session_id == session_id
        ]
        for run_id, handle in tuple(self._active.items()):
            if handle.session_id == session_id: await handle.cancel()
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_SESSION_DELETE_JOIN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # A Worker that never answers the cancellation must not block
                # Session deletion; the stream is now tolerant of the vanished
                # projection, so it still unwinds without a stray exception.
                logger.warning(
                    "Session %s deleted while %d Run stream(s) were still "
                    "winding down", session_id, len(tasks),
                )
        deleted = await self._store.delete_session(session_id)
        if deleted: self._changes.notify()
        return deleted

    async def deliver_step_input(
        self,
        run_id: str,
    ) -> bool:
        if run_id not in self._active:
            raise RuntimeError("Run is not active")
        record = self._active[run_id]
        pending = await self._store.pending_input(record.session_id)
        if not pending.next_step:
            return False
        supervisor = self._worker_for_run(run_id)
        if supervisor is None or supervisor.client is None:
            raise RuntimeError("Run is not active")
        return await supervisor.client.step_input(run_id, pending.next_step)
    async def close(self) -> None:
        self._closing = True
        active = tuple(self._active.values())
        for handle in active:
            try: await handle.cancel()
            except Exception: pass
        tasks = tuple(self._tasks.items())
        for run_id, task in tasks:
            # Cancellation of a startup waiter propagates into the shared
            # backend spawn, which may still be registering a process created
            # in a thread. Drain startup first, then let the pool close it.
            if run_id in self._dispatching:
                task.cancel()
        await asyncio.gather(*(task for _, task in tasks), return_exceptions=True)
        for handle in active:
            if not handle._finished.is_set():
                await handle.finish(RunResult(
                    request_id=handle.request_id,
                    status="cancelled",
                    metadata={"reason": "control_shutdown"},
                ))

    async def fail_active(
        self,
        reason: str,
        *,
        supervisor: WorkerSupervisor | None = None,
    ) -> None:
        """Publish a terminal failure for every Run when Worker is lost."""
        for run_id, handle in tuple(self._active.items()):
            if (
                supervisor is not None
                and self._run_supervisors.get(run_id) is not supervisor
            ):
                continue
            if not handle._finished.is_set():
                await handle.finish(RunResult(
                    request_id=run_id, status="failed",
                    metadata={"error": "worker_unavailable", "reason": reason},
                ))


__all__ = [
    "AgentWorkerPool", "ControlRunManager", "WorkerIdentity",
    "WorkerSupervisor", "agent_sandbox_requested",
]
