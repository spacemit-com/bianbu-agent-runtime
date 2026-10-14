"""SecurityBridge — bianbu-agent hook ↔ securityd 干预点框架的桥接（INT-RT-01/03/07）。

三件事：
  1. 把 securityd 的 InterceptorRegistry 拉起来（sec_capability@10 + DLP@50 + audit@90）
  2. async 边界：hook 是 async，securityd 的 D-Bus 客户端同步阻塞
     → run_in_executor 包裹，避免阻塞 FastAPI 事件循环（INT-RT-03）
  3. fail 策略分档：securityd 不可达时 L1/L2 fail-open、L3/L4 fail-closed（INT-RT-07）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from .errors import SecurityDenied

logger = logging.getLogger("bianbu.security.bridge")


class _HelperSecurityClient:
    """Security client adapter for Workers running a different Python ABI.

    Debian's dbus-python extension is often built for CPython 3.12 while the
    Runtime Worker uses 3.14.  Calls are delegated to the Runtime helper,
    which executes the versioned client under system Python and preserves the
    normal fail-closed behavior when that process cannot reach securityd.
    """

    def __getattr__(self, name: str):
        from bianbu_agent.security.security_runtime import security_client_call

        def call(*args, **kwargs):
            return security_client_call(name, *args, **kwargs)

        return call


__all__ = ["SecurityBridge", "get_bridge", "HIGH_RISK_TOOLS", "agent_security_mode_is_yolo"]

# securityd 不可达时仍须拒绝的高危工具（INT-RT-07 fail-closed 档）。
#
# 三态模型下策略键就是工具名。名字取自 securityd/capabilities.py 的权威字典，
# 覆盖会写文件、执行代码、外发消息、装 skill、派生子 Agent、改系统配置等
# 具有持久副作用的工具；securityd 挂掉时不能让这些工具静默放行。
HIGH_RISK_TOOLS = frozenset({
    "write_file", "patch", "terminal", "process",
    "skill_manage", "cronjob",
    "send_message", "send_email",
    "ha_call_service", "rl_start_training", "rl_edit_config",
    "delegate_task", "image_generate",
})

_ENV_HOME = "BIANBU_SECURITYD_HOME"
_DEFAULT_HOMES = (
    "/usr/lib/bianbu-securityd",
    "/home/bianbu/impl/bianbu-securityd",
    "/data/workspace2026-new/work0618/agent-os-security-design-dev/impl/bianbu-securityd",
)


def _discover_home() -> Optional[Path]:
    env = os.environ.get(_ENV_HOME)
    candidates = ([Path(env)] if env else []) + [Path(p) for p in _DEFAULT_HOMES]
    for p in candidates:
        if (p / "securityd" / "interceptor.py").exists():
            return p
    return None


class SecurityBridge:
    """单例。首次使用时惰性初始化 securityd。"""

    _instance: Optional["SecurityBridge"] = None
    _init_lock = threading.Lock()

    def __init__(self, home: Path, *, with_dlp: bool = True):
        self.home = home
        self.available = False
        self.reason = ""
        self._registry = None
        self._sc = None
        self._uds_transport = None
        self._dlp_ctx: dict[str, Any] = {}
        try:
            self._bootstrap(with_dlp=with_dlp)
            self.available = True
        except Exception as e:  # noqa: BLE001 — 初始化失败不能让 bianbu-agent 起不来
            self.reason = f"{type(e).__name__}: {e}"
            logger.warning("securityd 桥接初始化失败，将按 fail 分档降级：%s", self.reason)

    # —— 初始化 ——
    def _bootstrap(self, *, with_dlp: bool) -> None:
        # 用 append 而非 insert(0)，且必须 append：
        # securityd 根目录下有 tests/、cli/、data/ 等通用名字，其中 tests 与
        # bianbu-agent 自己的 tests 同名。二者都是命名空间包（无 __init__.py），
        # Python 会把同名路径**合并**进 __path__ —— 插到最前面就会让
        # securityd/tests 排在 bianbu-agent/tests 之前，污染测试进程的模块解析
        # （实测导致 test_public_contract 在同进程后续用例中失败）。
        # append 保证宿主自己的路径始终优先。
        for p in (str(self.home), str(self.home / "client")):
            if p not in sys.path:
                sys.path.append(p)

        from securityd.interceptor import InterceptorRegistry  # noqa: PLC0415
        import bianbu_security_handlers as handlers            # noqa: PLC0415

        # 用私有 registry 而非 securityd 的 global_registry：
        # global_registry 是进程级单例，一旦注册 handler 就对同进程内所有
        # 后续调用生效 —— 在测试进程里会污染那些本该与安全无关的用例
        # （实测 test_public_contract 因此失败）。私有实例天然随桥接销毁。
        registry = InterceptorRegistry()

        handlers.register_core_handlers(registry, with_audit=True)
        registry.set_audit_sink(
            handlers.make_audit_sink(client_provider=self.client))

        from .uds_transport import SecurityUdsTransport  # noqa: PLC0415

        self._uds_transport = SecurityUdsTransport()

        if with_dlp and os.environ.get("BIANBU_AGENT_DLP", "1") != "0":
            from securityd.dlp.handlers import (  # noqa: PLC0415
                DLPSessionStore, register_dlp_handlers,
            )
            from securityd.dlp.pii_scanner import PIIScanner  # noqa: PLC0415
            from securityd.dlp.policy_store import DLPPolicyStore  # noqa: PLC0415
            from .uds_transport import (  # noqa: PLC0415
                HybridDLPPolicyStore, HybridPIIScanner,
            )

            register_dlp_handlers(registry)
            # DLP handler 从 context 取这三个依赖。不注入会抛 KeyError，
            # 而干预点框架「异常不中断链」会把它吞成静默 ALLOW —— DLP 形同未启用。
            store = HybridDLPPolicyStore(DLPPolicyStore(), self._uds_transport)
            scanner = HybridPIIScanner(PIIScanner(), self._uds_transport, store)
            self._dlp_ctx = {
                "dlp_scanner": scanner,
                "dlp_store": store,
                "dlp_sessions": DLPSessionStore(),
            }
            logger.info("DLP 全链路已挂载（8 个干预点，中央策略 + 大载荷 UDS）")

        self._registry = registry
        logger.info("securityd 桥接就绪：home=%s", self.home)

    def restore_session_state(self, state: dict, session_id: str) -> None:
        """Restore only private JSON state supplied by the Runtime process."""
        sessions = self._dlp_ctx.get("dlp_sessions")
        if sessions is None:
            return
        mapping = state.get("pii_map", {})
        if not isinstance(mapping, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in mapping.items()
        ):
            raise ValueError("invalid private DLP session mapping")
        sessions.merge(session_id, mapping)

    def session_state(self, session_id: str) -> dict:
        """Return the mapping through private state, never public metadata."""
        sessions = self._dlp_ctx.get("dlp_sessions")
        mapping = sessions.get(session_id) if sessions is not None else {}
        return {"pii_map": mapping} if mapping else {}

    # —— 客户端 ——
    def client(self):
        """D-Bus 客户端。每次取模块级单例，让 monkeypatch 可生效。"""
        if self._sc is None:
            # On CPython 3.14 the native dbus extension may be unavailable;
            # use the shared helper bridge instead of failing every check.
            try:
                import bianbu_security_client as sc  # noqa: PLC0415
            except (ImportError, ModuleNotFoundError):
                sc = _HelperSecurityClient()
            self._sc = sc
        return self._sc

    # —— 干预点调用 ——
    async def invoke(self, hook_point, agent_id: str, data: Any, **ctx):
        """async 边界：把同步的 registry.invoke 丢到线程池执行。

        registry.invoke 内部会走 D-Bus（同步阻塞）。直接 await 会卡住
        FastAPI 的事件循环，并发请求会被串行化。
        """
        if not self.available or self._registry is None:
            raise RuntimeError(self.reason or "securityd 桥接不可用")
        merged = {**self._dlp_ctx, **ctx}   # 调用方可覆盖（测试注入替身用）
        loop = asyncio.get_running_loop()
        def run():
            stage = getattr(hook_point, 'value', str(hook_point))
            if stage in {'input', 'pre_model_call', 'post_model_call',
                         'pre_tool_call', 'post_tool_call', 'output'} and agent_security_mode_is_yolo(
                             agent_id, client_provider=self.client):
                from securityd.interceptor import InterceptResult
                result = InterceptResult(metadata={
                    'mode': 'yolo', 'dlp_scanned': False, 'checks_skipped': True})
                self.audit(agent_id, f'hook:{stage}', stage,
                           'decision=ALLOW;mode=yolo;checks=skipped')
                self._audit_large_payload(hook_point, agent_id, data, result, merged)
                return result
            result = self._registry.invoke(
                hook_point, agent_id, data,
                client_provider=self.client, **merged)
            self._audit_large_payload(hook_point, agent_id, data, result, merged)
            return result

        return await loop.run_in_executor(None, run)

    def _audit_large_payload(self, hook_point, agent_id: str, data: Any,
                             result, context: dict) -> None:
        """>1KB 的完整治理载荷走正式 audit.batch；失败不覆盖 DLP 决策。"""
        threshold = int(os.environ.get("BIANBU_AGENT_AUDIT_UDS_THRESHOLD", "1024"))
        try:
            document = json.dumps(data, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            document = str(data)
        if len(document.encode("utf-8")) <= threshold or self._uds_transport is None:
            return
        hp = getattr(hook_point, "value", str(hook_point))
        decision = getattr(getattr(result, "decision", ""), "value", "")
        try:
            response = self._uds_transport.call({
                "op": "audit.batch",
                "agent_id": str(agent_id),
                "session_id": str(context.get("session_id", "")),
                "run_id": str(context.get("run_id", "")),
                "entries": [{
                    "op_type": f"hook_payload:{hp}",
                    "target": str(decision),
                    "result": str(getattr(result, "message", ""))[:1024],
                    "payload": document,
                }],
            })
            result.metadata["audit_payload_transport"] = "uds"
            result.metadata["audit_payload_seq"] = int(response["audit_seq"])
        except Exception as exc:  # noqa: BLE001 — DLP 结果优先，显式暴露审计降级
            result.metadata["audit_payload_transport"] = "failed"
            result.metadata["audit_payload_error"] = str(exc)
            logger.error("完整审计载荷写入 UDS 失败：%s", exc)

    def hook_point(self, name: str):
        """按名字取 securityd 的 HookPoint 枚举值。"""
        from securityd.interceptor import HookPoint  # noqa: PLC0415
        return HookPoint(name)

    # —— fail 策略分档（INT-RT-07）——
    @staticmethod
    def degrade(tool: str, capability: str, error: Any) -> None:
        """securityd 不可达时的降级：高危 fail-closed，低危 fail-open。

        全放行等于安全形同虚设；全拒绝则 securityd 一挂整个服务全废。
        折中：把"能造成实际破坏"的能力挡住，其余放行并记日志。
        """
        if os.environ.get("BIANBU_SECURITY_FAIL_OPEN") == "1":
            logger.warning("securityd 不可达，FAIL_OPEN 全放行：%s（%s）", tool, error)
            return
        if str(tool) in HIGH_RISK_TOOLS or str(capability) in HIGH_RISK_TOOLS:
            raise SecurityDenied(
                tool=tool, capability=capability,
                reason=f"securityd 不可达，高危工具 fail-closed 拦截（{error}）",
                degraded=True)
        logger.warning(
            "securityd 不可达，低危能力 fail-open 放行：%s(%s)（%s）",
            tool, capability, error)

    # —— 审计（不经干预点链的直接记账）——
    def audit(self, agent_id: str, op_type: str, target: str, result: str) -> None:
        document = json.dumps({"target": target, "result": result},
                              ensure_ascii=False, separators=(",", ":"))
        threshold = int(os.environ.get("BIANBU_AGENT_AUDIT_UDS_THRESHOLD", "1024"))
        if (self._uds_transport is not None
                and len(document.encode("utf-8")) > threshold):
            try:
                self._uds_transport.call({
                    "op": "audit.batch", "agent_id": str(agent_id),
                    "entries": [{"op_type": str(op_type), "target": str(target),
                                 "result": "direct", "payload": document}],
                })
                return
            except Exception as e:  # noqa: BLE001 — 回退 D-Bus 元数据审计
                logger.error("完整审计 UDS 失败，回退 D-Bus：%s", e)
        try:
            self.client().audit_log(agent_id, op_type, target, result)
        except Exception as e:  # noqa: BLE001 — 审计失败不阻断主流程
            logger.debug("审计写入失败：%s", e)


def get_bridge(*, with_dlp: bool = True) -> SecurityBridge:
    """取全局单例。多个 hook 共用一个 registry，避免重复注册 handler。"""
    if SecurityBridge._instance is None:
        with SecurityBridge._init_lock:
            if SecurityBridge._instance is None:
                home = _discover_home()
                if home is None:
                    logger.warning(
                        "未找到 securityd（设 %s 指向 impl/bianbu-securityd）",
                        _ENV_HOME)
                    inst = SecurityBridge.__new__(SecurityBridge)
                    inst.home = None
                    inst.available = False
                    inst.reason = f"securityd 未找到（未设 {_ENV_HOME}）"
                    inst._registry = None
                    inst._sc = None
                    inst._uds_transport = None
                    inst._dlp_ctx = {}
                    SecurityBridge._instance = inst
                else:
                    SecurityBridge._instance = SecurityBridge(home, with_dlp=with_dlp)
    return SecurityBridge._instance


def agent_security_mode_is_yolo(agent_id: str, *, client_provider=None) -> bool:
    try:
        provider = client_provider or get_bridge().client
        value = provider().get_agent_security_mode(str(agent_id))
        return isinstance(value, dict) and str(value.get("mode", "")).lower() == "yolo"
    except Exception:  # noqa: BLE001
        return False
