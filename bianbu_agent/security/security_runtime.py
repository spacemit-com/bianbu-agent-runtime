"""Runtime Bianbu Agent Runtime 与 ``io.bianbu.Security.Runtime`` 的适配层。

本模块只负责把 Runtime 的执行协调请求转换成 securityd 控制面调用。
securityd 决定 ``effective_mode`` 并签发一次性 lease；真正的进程/沙箱后端
仍由 Runtime 执行，不能在这里根据用户请求自行放宽策略。

为了支持没有安装 securityd Python 源码的生产镜像，优先导入已安装的
``bianbu_security_client``；开发环境才通过 ``BIANBU_SECURITYD_HOME`` 加入
client 目录。所有 Runtime 方法都在工作线程中调用同步 dbus-python。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)


class SecurityRuntimeUnavailable(RuntimeError):
    """security.runtime 不可达或客户端未安装。"""


class SecurityRuntimeRejected(RuntimeError):
    """security.runtime 拒绝了启动请求。"""


@dataclass(frozen=True, slots=True)
class RuntimeStartRequest:
    agent_id: str
    requested_mode: str = "host"
    worker_agent_id: str = ""
    worker_data_root: str = ""
    worker_socket_root: str = ""
    worker_config_path: str = ""
    security_uds: str = ""
    securityd_home: str = ""
    memory_uds: str = ""
    memory_registry_dir: str = ""

    def as_dict(self) -> dict[str, str]:
        result = {"requested_mode": self.requested_mode}
        if self.worker_agent_id:
            result["worker_agent_id"] = self.worker_agent_id
        if self.worker_data_root:
            result["worker_data_root"] = self.worker_data_root
        if self.worker_socket_root:
            result["worker_socket_root"] = self.worker_socket_root
        if self.worker_config_path:
            result["worker_config_path"] = self.worker_config_path
        if self.security_uds:
            result["security_uds"] = self.security_uds
        if self.securityd_home:
            result["securityd_home"] = self.securityd_home
        if self.memory_uds:
            result["memory_uds"] = self.memory_uds
        if self.memory_registry_dir:
            result["memory_registry_dir"] = self.memory_registry_dir
        return result


@dataclass(frozen=True, slots=True)
class RuntimeStartDecision:
    lease_id: str
    policy_json: str
    effective_mode: str
    backend: str
    sandbox_required: bool
    expires_at: int
    reason: str = ""

    @property
    def policy(self) -> dict[str, Any]:
        try:
            value = json.loads(self.policy_json)
        except (TypeError, ValueError) as exc:
            raise SecurityRuntimeRejected("security.runtime 返回非法 policy_json") from exc
        if not isinstance(value, dict):
            raise SecurityRuntimeRejected("security.runtime policy_json 必须是对象")
        return value


def _client_search_dirs() -> list[str]:
    """客户端发布目录，按优先级排列。

    生产安装只暴露 ``client`` 发布目录（由 Security 客户端包提供），不把
    securityd 源码根目录加入 Runtime 的导入路径。``BIANBU_SECURITYD_HOME``
    仅保留给开发/测试环境使用。
    """
    candidates: list[str] = []
    configured = os.environ.get("BIANBU_SECURITY_CLIENT_DIR", "").strip()
    if configured:
        candidates.append(configured)
    # Debian 安装的安全客户端目录。这里是 client 的发布边界，不是
    # securityd Python 包的源码导入路径。
    candidates.extend((
        "/usr/lib/bianbu-agent-securityd/client",
        "/usr/lib/bianbu-securityd/client",
    ))
    home = os.environ.get("BIANBU_SECURITYD_HOME", "").strip()
    if home:
        # 源码根目录只允许显式开发配置；生产模式不得依赖它。
        if _production_mode():
            raise SecurityRuntimeUnavailable(
                "生产模式未安装 bianbu_security_client（请设置 BIANBU_SECURITY_CLIENT_DIR）"
            )
        candidates.append(str(Path(home) / "client"))
        candidates.append(home)
    return candidates


def _client_dependency_dirs() -> list[str]:
    """客户端二进制依赖（dbus-python）所在的系统目录。

    Runtime 的 venv 不打包 dbus-python，Debian 把它装在 dist-packages。只有
    客户端本身已找到、缺的是它的依赖时才追加这些目录，并且追加到
    ``sys.path`` 末尾，避免系统包整体覆盖 venv 内的同名依赖。
    """
    configured = os.environ.get("BIANBU_SECURITY_CLIENT_SITE_DIRS", "").strip()
    dirs = [path for path in configured.split(os.pathsep) if path] if configured else []
    dirs.extend((
        "/usr/lib/python3/dist-packages",
        "/usr/local/lib/python3/dist-packages",
    ))
    return dirs


def _import_client():
    import bianbu_security_client as client  # type: ignore

    return client


def _client_module():
    """加载版本化安全客户端，不把 securityd 内部包作为 Runtime 依赖。"""
    # `except ... as name` 会在 except 块结束时删掉这个名字，后面再引用它
    # 只会得到 UnboundLocalError —— 那会把「没装安全客户端」这条真原因
    # 盖成一句 Python 内部报错，用户和运维都无从下手。所以另存一份。
    first_error: ImportError | None = None
    try:
        return _import_client()
    except ImportError as first:
        first_error = first
        last_error = first

    # Installed clients live outside the Runtime venv. An initial import
    # failure does not imply a dbus ABI mismatch: try the published client
    # directories before callers fall back to the system-Python helper.
    for path in _client_search_dirs():
        if not os.path.isdir(path):
            continue
        if path not in sys.path:
            sys.path.append(path)
        try:
            return _import_client()
        except ImportError as exc:
            last_error = exc

    if getattr(last_error, "name", None) not in (None, "bianbu_security_client"):
        # 客户端已找到，缺的是它自己的依赖（例如 dbus-python）。
        for path in _client_dependency_dirs():
            if os.path.isdir(path) and path not in sys.path:
                sys.path.append(path)
        try:
            return _import_client()
        except ImportError as exc:
            last_error = exc

    if getattr(last_error, "name", None) != "bianbu_security_client":
        raise SecurityRuntimeUnavailable(
            f"安全客户端加载失败：{last_error}；请检查安全客户端依赖"
        ) from last_error
    raise SecurityRuntimeUnavailable(
        "未找到 bianbu_security_client；请安装 Runtime 安全客户端") from first_error


def _production_mode() -> bool:
    return str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production"


def security_client_module():
    """返回版本化客户端，供 Runtime 的身份绑定/DLP 适配层复用。"""
    return _client_module()


def security_client_call(method: str, *args, **kwargs):
    """Call a security client method across the Python/dbus ABI boundary.

    Development images may run Runtime on Python 3.14 while dbus-python is
    installed only for the system Python 3.12.  Keep synchronous middleware
    checks on the same helper bridge used by :class:`SecurityRuntimeClient`.
    """
    try:
        client = security_client_module()
        return getattr(client, method)(*args, **kwargs)
    except (ImportError, ModuleNotFoundError, SecurityRuntimeUnavailable):
        # A helper has a different PID and cannot use this Worker's lease.
        if os.environ.get("BIANBU_WORKER_MANAGED") == "1":
            raise
        if not _bridge_available():
            raise
        payload = json.dumps({"method": method, "args": args,
                              "kwargs": kwargs}, ensure_ascii=False)
        env = os.environ.copy()
        client_dir = env.get("BIANBU_SECURITY_CLIENT_DIR", "")
        home = env.get("BIANBU_SECURITYD_HOME", "")
        env["PYTHONPATH"] = client_dir or (str(Path(home) / "client") if home else "")
        proc = subprocess.run(["/usr/bin/python3", str(_bridge_helper()), payload],
                              capture_output=True, text=True, env=env,
                              timeout=30, check=False)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
        return json.loads(proc.stdout)


def _bridge_helper() -> Path:
    return Path(__file__).with_name("dbus_client_helper.py")


def _bridge_available() -> bool:
    """helper 是不可用进程内客户端时的唯一回退通道。

    受管 Worker 不能走 helper：helper 有独立 PID，无法使用该 Worker 的 lease。
    """
    if os.environ.get("BIANBU_WORKER_MANAGED") == "1":
        return False
    return _bridge_helper().is_file()


def security_client_available() -> bool:
    """本进程能否调用版本化安全客户端（进程内或经 helper 桥接）。

    调用方用它在降级前探测；``False`` 表示计量、策略读取这类可选能力应当
    明确降级，而不是在每次模型调用时失败。
    """
    try:
        security_client_module()
    except SecurityRuntimeUnavailable:
        return _bridge_available()
    return True


def _normalise_result(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value).items()}


class SecurityRuntimeClient:
    """异步安全 Runtime 客户端。

    ``required`` 为真时所有错误均 fail-closed；开发态可显式设为假，让调用
    方决定是否继续使用旧的 run.sh 入口，但不会伪造 lease 或 effective_mode。
    """

    def __init__(self, *, required: bool | None = None, metrics: object | None = None):
        self.metrics = metrics
        self.required = (
            str(os.environ.get("BIANBU_RUNTIME_SECURITY", "required")).lower()
            not in {"0", "false", "off", "optional"}
            if required is None else bool(required)
        )
        if _production_mode():
            # 生产 Runtime 不允许通过 optional 降级绕过 security.runtime。
            self.required = True

    async def _call(self, method: str, *args, **kwargs):
        def invoke():
            try:
                client = _client_module()
                return getattr(client, method)(*args, **kwargs)
            except (ImportError, ModuleNotFoundError, SecurityRuntimeUnavailable) as exc:
                if os.environ.get("BIANBU_WORKER_MANAGED") == "1":
                    raise
                helper = Path(__file__).with_name("dbus_client_helper.py")
                if not helper.exists():
                    raise exc
                payload = json.dumps({"method": method, "args": args,
                                      "kwargs": kwargs}, ensure_ascii=False)
                env = os.environ.copy()
                client_dir = env.get("BIANBU_SECURITY_CLIENT_DIR", "")
                home = env.get("BIANBU_SECURITYD_HOME", "")
                env["PYTHONPATH"] = client_dir or (str(Path(home) / "client") if home else "")
                proc = subprocess.run(["/usr/bin/python3", str(helper), payload],
                                      capture_output=True, text=True, env=env,
                                      timeout=30, check=False)
                if proc.returncode:
                    raise RuntimeError(proc.stderr.strip() or proc.stdout.strip()) from exc
                return json.loads(proc.stdout)

        try:
            return await asyncio.to_thread(invoke)
        except Exception as exc:  # noqa: BLE001 - D-Bus/client boundary
            if self.required:
                raise SecurityRuntimeUnavailable(f"security.runtime {method} 失败：{exc}") from exc
            logger.warning("security.runtime %s 不可用（optional）：%s", method, exc)
            return None

    async def prepare_start(self, request: RuntimeStartRequest) -> RuntimeStartDecision | None:
        extra = {key: value for key, value in request.as_dict().items()
                 if key != "requested_mode"}
        try:
            if extra:
                result = await self._call(
                    "prepare_runtime_start", request.agent_id,
                    request.requested_mode, **extra)
            else:
                result = await self._call(
                    "prepare_runtime_start", request.agent_id, request.requested_mode)
        except Exception:
            self._metric_inc("runtime_security_prepare_total", mode=request.requested_mode, result="error")
            raise
        self._metric_inc("runtime_security_prepare_total", mode=request.requested_mode,
                         result="skipped" if result is None else "success")
        if result is None:
            return None
        result = _normalise_result(result)
        required = ("lease_id", "policy_json", "effective_mode",
                    "backend", "sandbox_required", "expires_at")
        missing = [key for key in required if key not in result]
        if missing:
            raise SecurityRuntimeRejected(f"PrepareStart 缺少字段：{missing}")
        decision = RuntimeStartDecision(
            lease_id=str(result["lease_id"]), policy_json=str(result["policy_json"]),
            effective_mode=str(result["effective_mode"]), backend=str(result["backend"]),
            sandbox_required=bool(result["sandbox_required"]),
            expires_at=int(result["expires_at"]), reason=str(result.get("reason", "")),
        )
        if decision.effective_mode not in {"sandbox", "host"}:
            raise SecurityRuntimeRejected("PrepareStart 返回非法 effective_mode")
        if decision.sandbox_required != (decision.effective_mode == "sandbox"):
            raise SecurityRuntimeRejected("PrepareStart sandbox_required 与 effective_mode 不一致")
        decision.policy
        return decision

    async def bind_agent(self, agent_id: str) -> bool:
        """Bind the control-plane caller, including the Python ABI bridge."""
        result = await self._call("bind_agent", str(agent_id))
        return bool(result)

    async def record_syscall_interception(self, agent_id: str, pid: int,
                                          arch: str, syscall: str, number: int,
                                          backend: str, policy_version: int,
                                          **identity: Any) -> dict[str, Any] | None:
        """Forward an observed syscall event to securityd.

        Older security clients expose only the legacy seven-argument method;
        identity metadata is therefore retained locally and ignored by that
        interface until the server supports the extended schema.
        """
        # Prefer the identity-bound V2 API whenever metadata is available.
        # Older security client bundles may not expose it, so transparently
        # fall back to the legacy API for development/package compatibility.
        if identity.get("instance_id") and identity.get("lease_id"):
            try:
                return await self._call(
                    "record_syscall_interception_v2", str(agent_id),
                    str(identity["instance_id"]), str(identity["lease_id"]),
                    int(pid), int(identity.get("cgroup_id") or 0), str(arch),
                    str(syscall), int(number), str(backend), int(policy_version),
                    str(identity.get("event_id") or ""), str(identity.get("worker_agent_id") or ""),
                )
            except (AttributeError, TypeError) as exc:
                logger.info("securityd lacks syscall interception V2; using legacy API: %s", exc)
            except SecurityRuntimeUnavailable as exc:
                # _call wraps missing client methods in this boundary error;
                # only downgrade when the failure clearly indicates an absent
                # API, never on policy/identity denial.
                message = str(exc).lower()
                missing_api = any(marker in message for marker in (
                    "has no attribute", "unknownmethod", "unknown method",
                    "not found", "未找到", "缺少"))
                if not missing_api:
                    raise
                logger.info("securityd lacks syscall interception V2; using legacy API")
        return await self._call("record_syscall_interception", str(agent_id),
                                int(pid), str(arch), str(syscall), int(number),
                                str(backend), int(policy_version))

    async def get_sandbox_policy(self, lease_id: str) -> dict[str, Any]:
        result = await self._call("get_runtime_sandbox_policy", lease_id)
        if result is None:
            raise SecurityRuntimeUnavailable("optional 模式不能获取 sandbox policy")
        return _normalise_result(result)

    async def record_network_learning_event(
        self, agent_id: str, instance_id: str, lease_id: str, pid: int,
        event: Mapping[str, Any],
    ) -> bool:
        result = await self._call(
            "record_network_learning_event", str(agent_id), str(instance_id),
            str(lease_id), int(pid), dict(event))
        return bool(result)

    async def report_network_learning_collector(
        self, agent_id: str, instance_id: str, lease_id: str, pid: int,
        state: str, reason: str = "",
    ) -> bool:
        result = await self._call(
            "report_network_learning_collector", str(agent_id), str(instance_id),
            str(lease_id), int(pid), str(state), str(reason))
        return bool(result)

    async def commit_start(self, lease_id: str, instance_id: str, pid: int,
                           cgroup_path: str, attestation: Mapping[str, Any]) -> dict[str, Any] | None:
        try:
            result = await self._call("commit_runtime_start", lease_id, instance_id,
                                      int(pid), cgroup_path, dict(attestation))
        except Exception:
            self._metric_inc("runtime_security_commit_total", result="error")
            raise
        self._metric_inc("runtime_security_commit_total",
                         result="skipped" if result is None else "success")
        return None if result is None else _normalise_result(result)

    async def abort_start(self, lease_id: str, reason: str) -> bool:
        result = await self._call("abort_runtime_start", lease_id, reason)
        return bool(result) if result is not None else False

    async def revoke_agent(self, agent_id: str, reason: str) -> bool:
        result = await self._call("revoke_runtime_agent", agent_id, reason)
        return bool(result) if result is not None else False

    async def stop_instance(self, lease_id: str, instance_id: str, reason: str) -> dict[str, Any] | None:
        """正常停止后收敛 securityd 侧 lease/instance（不撤销整个 Agent）。

        旧 securityd 没有 StopInstance；此时返回 None 并记录 warning，不阻塞停止。
        缺失该调用会让 securityd 的 runtime_instance 永远停留在 running（缺陷 D-9）。
        """
        if not lease_id or not instance_id:
            return None
        try:
            result = await self._call("stop_runtime_instance", lease_id, instance_id, reason)
        except Exception as exc:  # noqa: BLE001 - old daemon / transport failure
            logger.warning("securityd StopInstance unavailable for %s: %s", instance_id, exc)
            self._metric_inc("runtime_security_stop_total", result="error")
            return None
        self._metric_inc("runtime_security_stop_total",
                         result="skipped" if result is None else "success")
        return None if result is None else _normalise_result(result)

    async def state(self, agent_id: str) -> dict[str, Any]:
        result = await self._call("get_runtime_state", agent_id)
        return {} if result is None else _normalise_result(result)

    async def audit_event(self, agent_id: str, event: str, target: str,
                          result: str) -> int | None:
        value = await self._call("audit_log", agent_id, event, target, result)
        return None if value is None else int(value)

    def _metric_inc(self, name: str, **labels: object) -> None:
        """记录指标，永不因为指标本身失败而顶掉调用方真正的异常。"""
        increment = getattr(self.metrics, "inc", None)
        if not callable(increment):
            return
        try:
            increment(name, **labels)
        except Exception as exc:  # noqa: BLE001 - 指标不能污染错误路径
            logger.debug("记录指标 %s 失败：%s", name, exc)


def current_cgroup_path(pid: int | None = None) -> str:
    selected = int(pid or os.getpid())
    rows = Path(f"/proc/{selected}/cgroup").read_text(encoding="utf-8").splitlines()
    for row in rows:
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[2].strip():
            if parts[0] == "0":
                return parts[2].strip()
    for row in rows:
        parts = row.split(":", 2)
        if len(parts) == 3 and parts[2].strip():
            return parts[2].strip()
    raise RuntimeError(f"进程 {selected} 没有可证明的 cgroup")


def current_attestation(decision: RuntimeStartDecision, *, pid: int | None = None,
                        instance_id: str = "") -> tuple[str, dict[str, Any]]:
    selected_pid = int(pid or os.getpid())
    if not instance_id:
        instance_id = f"pa-{selected_pid}"
    sandboxed = str(os.environ.get("BIANBU_SANDBOX_ACTIVE", "0")).lower() in {"1", "true", "yes"}
    container_id = os.environ.get("BIANBU_CONTAINER_ID", "")
    if decision.effective_mode == "sandbox" and sandboxed and not container_id:
        # bwrap does not expose an OCI ID.  This identifier is still bound to
        # the committed PID and policy, so a later PID/cgroup mismatch fails.
        container_id = f"bwrap:{selected_pid}"
    seccomp = decision.policy.get("seccomp_profile") if isinstance(decision.policy, Mapping) else None
    return current_cgroup_path(selected_pid), {
        "backend": decision.backend,
        "pid": selected_pid,
        "sandboxed": sandboxed,
        "container_id": container_id,
        # 只有 bwrap 沙箱在内核里装载了 securityd 版本化过滤器并叠加
        # SECCOMP_FILTER_FLAG_LOG；host Worker 为保留 sudo/setuid 语义不装载
        # 过滤器（见 HostProcessBackend），这里必须如实报告没有内核 seccomp
        # 审计来源，securityd 据此判断内核观测器对该实例是否有审计覆盖（缺陷 D-3）。
        "kernel_seccomp_log": bool(
            sandboxed and isinstance(seccomp, Mapping) and seccomp.get("path")),
    }


__all__ = [
    "RuntimeStartDecision", "RuntimeStartRequest", "SecurityRuntimeClient",
    "SecurityRuntimeRejected", "SecurityRuntimeUnavailable", "current_attestation",
    "current_cgroup_path", "security_client_available", "security_client_call",
    "security_client_module",
]
