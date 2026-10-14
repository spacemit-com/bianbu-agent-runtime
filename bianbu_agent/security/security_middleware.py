"""bianbu-security 中间件（INT-RT-06 / INT-RT-10 / INT-RT-12）。

`awrap_tool_call` 是 wrapper 式协议（可短路 / 重试 / 改写 request 与 response），
所以三件事能在一处解决：

  1. DLP 脱敏改写（INT-RT-10）—— hook 层的 ContextPatch 没有参数通道，
     由 before_tool hook 把脱敏结果写进 metadata，这里用官方
     `ToolCallRequest.override()` 应用到实际调用。`plugin_tools.py` 零改动。

  2. 拒绝终止（INT-RT-06）—— catch SecurityDenied 后冒泡，终止整轮 run。
     三态模型里 deny 是用户的明确决定；不这样做的话，`create_agent` 默认
     `handle_tool_errors=True` 会把异常包成 ToolMessage，Agent 只是换个
     路径重试同一个被拒绝的工具。

  3. 审批判定（INT-RT-12 的配套）—— 供 `InterruptOnConfig.when` 读取
     securityd 的 CONFIRM 判定。
"""
from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any, Callable, Mapping

logger = logging.getLogger("bianbu_agent.security")

__all__ = [
    "BianbuSecurityMiddleware",
    "build_security_hitl_middleware",
    "securityd_requires_confirm",
    "securityd_record_approval",
    "extract_security_metadata",
]

_PLUGIN_ID = "bianbu.security"


def extract_security_metadata(state: Any) -> Mapping[str, Any]:
    """从 graph state 里取 before_tool hook 写的 securityd 元数据。

    host.py::_dispatch 聚合后是两层：{plugin_id: {自己的 metadata}}，
    所以路径是 metadata["bianbu.security"]["securityd"]。
    """
    if state is None:
        return {}
    meta = None
    if isinstance(state, Mapping):
        meta = state.get("hook_metadata") or state.get("metadata")
    else:
        meta = getattr(state, "hook_metadata", None) or getattr(state, "metadata", None)
    if not isinstance(meta, Mapping):
        return {}
    scoped = meta.get(_PLUGIN_ID)
    if isinstance(scoped, Mapping):
        inner = scoped.get("securityd")
        if isinstance(inner, Mapping):
            return inner
        return scoped
    inner = meta.get("securityd")
    return inner if isinstance(inner, Mapping) else {}


def _plugin_lib_path() -> str | None:
    """定位 bianbu.security 插件的 lib 目录（复用其映射与桥接实现）。

    中间件跑在 Runtime 主干里，而映射表在插件目录下 —— 两者没有包依赖关系，
    只能按约定路径找。找不到就返回 None，调用方按「不需审批」处理。
    """
    import os
    import sys

    override = os.environ.get("BIANBU_SECURITY_PLUGIN_HOME")
    if override and os.path.isdir(override):
        return override
    roots = []
    # Managed Workers receive the canonical, sandbox-visible data root through
    # BIANBU_RUNTIME_DATA_DIR.  BIANBU_AGENT_DATA_DIR is the legacy run.sh
    # spelling.  Looking at only the latter made the HITL precheck miss the
    # plugin inside bwrap, while before_tool still returned CONFIRM; the tool
    # then ran because no interrupt had been created.
    for variable in ("BIANBU_RUNTIME_DATA_DIR", "BIANBU_AGENT_DATA_DIR"):
        data_dir = os.environ.get(variable)
        if data_dir:
            roots.append(os.path.join(data_dir, "plugins", "bianbu.security"))
    # 当前 Runtime 的配置数据目录是 `.bianbu-agent`。
    for dirname in (".bianbu-agent",):
        roots.append(os.path.join(os.getcwd(), dirname, "plugins", "bianbu.security"))
    for r in roots:
        if os.path.isdir(os.path.join(r, "lib")):
            return r
    return None


def clear_confirm_cache() -> None:
    """兼容旧调用方；动态判权不再缓存跨会话结果。

    WebUI 可以在 Runtime 运行期间修改能力策略。旧的进程内缓存会把修改前的
    ALLOW/CONFIRM 结果带到后续 run，导致 CONFIRM 静默放行或策略修改不生效。
    每次 HITL 预判都直接查询 securityd，优先保证正确性。
    """
    return None


def _runtime_context(request: Any) -> tuple[str, str]:
    """提取当前工具调用的 session/run 上下文供 securityd V2 判权。"""
    runtime = getattr(request, "runtime", None)
    config = getattr(runtime, "config", None)
    if not isinstance(config, Mapping):
        # after_model receives LangGraph Runtime, not ToolRuntime: it has no
        # config attribute. Read the active invocation's task-local config,
        # which also carries the new Run identity when a graph is resumed.
        from langgraph.config import get_config

        try:
            config = get_config()
        except RuntimeError:
            config = None
    configurable = config.get("configurable", {}) if isinstance(config, Mapping) else {}
    if not isinstance(configurable, Mapping):
        configurable = {}
    context = getattr(runtime, "context", None)
    control = getattr(context, "control", None)
    return (
        str(
            configurable.get("session_id")
            or configurable.get("product_session_id")
            or getattr(control, "session_id", "")
            or ""
        ).strip(),
        str(
            configurable.get("run_id")
            or configurable.get("request_id")
            or getattr(control, "request_id", "")
            or ""
        ).strip(),
    )


class _SecurityPolicyDenied(PermissionError):
    """A denial raised before LangGraph can invoke the tool.

    The bundled security hook is selected by an Agent manifest.  The Runtime
    security middleware, however, is global and must remain authoritative even
    when a manifest omits that optional hook.  Keep this small duck-typed
    exception in the Runtime so the run manager can expose the structured
    security error without importing an installable plugin module.
    """

    def __init__(self, *, tool: str, policy: str = "deny") -> None:
        self.tool = tool
        # 三态模型下策略键就是工具名：能力名已经并入工具名，不再有第二套命名。
        self.capability = tool
        self.policy = policy
        self.decision = "DENY"
        self.reason = f"工具 {tool} 的策略为 {policy}，本次调用被拒绝"
        self.degraded = False
        super().__init__(f"[bianbu-security] {self.reason}")

    @property
    def should_terminate_run(self) -> bool:
        return True

    def to_metadata(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "policy": self.policy,
            "decision": self.decision,
            "reason": self.reason,
            "degraded": self.degraded,
            "terminate_run": self.should_terminate_run,
        }


def securityd_requires_confirm(request: Any) -> bool:
    """`InterruptOnConfig.when` 的判定函数（INT-RT-12）。

    **直接问 securityd，不读 hook 写的 metadata。**

    原实现从 `request.state` 读 before_tool hook 落的 `requires_approval`，
    但那永远读不到 —— 时序不允许：

        ① 模型产出 tool_call
        ② HITL 中间件 after_model → when(req)        ← 此处，需要判权结果
        ③ 工具开始执行 → _adapt_tool.invoke()
        ④ before_tool hook 跑，才拿到 CONFIRM        ← 太晚

    `when` 在 `after_model` 阶段求值（见 langchain 的
    human_in_the_loop.py），比 before_tool hook 早一整个阶段。实测原实现
    导致 CONFIRM 静默放行：判权说要确认，工具却直接执行了。

    所以这里自己走一遍「工具名 → 能力 → CheckPermissionV2」。开销是一次
    D-Bus 往返（实测 P99 4.357ms）。不缓存结果，避免 WebUI 修改策略后
    Runtime 继续使用旧判定。

    fail 策略：判不出来时返回 True，把决定交给人。before_tool hook 能阻断
    明确 DENY，却无法自己创建 LangGraph interrupt；如果这里 fail-open，
    securityd 随后返回 CONFIRM 也已经来不及弹审批，工具会直接执行。
    """
    import os
    import sys

    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return False

    try:
        tc = getattr(request, "tool_call", None) or {}
        tool_name = str(tc.get("name") or "")
        if not tool_name:
            return False
        args = tc.get("args") or {}

        # YOLO is an Agent-level override and must be evaluated before tool
        # mapping.  Unmapped tools (for example ``skill_view``) normally use
        # the conservative ``unmapped=confirm`` policy; checking that policy
        # first caused YOLO Agents to receive an approval prompt anyway.
        agent_id = (
            os.environ.get("COM_BIANBU_AGENT_ID")
            or os.environ.get("BIANBU_WORKER_AGENT_ID")
            or os.environ.get("BIANBU_AGENT_ID")
            or "com.bianbu.agent"
        )
        try:
            from bianbu_agent.security.security_runtime import security_client_call
            mode = security_client_call("get_agent_security_mode", agent_id)
            if isinstance(mode, Mapping) and str(mode.get("mode", "")).lower() == "yolo":
                logger.info("Agent %s 使用 YOLO 模式，跳过 HITL approval interrupt", agent_id)
                return False
        except Exception as exc:  # noqa: BLE001 — retain fail-safe confirmation
            logger.debug("读取 Agent security mode 失败，继续常规判权：%s", exc)

        plugin_home = _plugin_lib_path()
        if plugin_home is None:
            logger.warning("找不到 bianbu.security 插件目录，审批预判按需确认处理")
            return True
        if plugin_home not in sys.path:
            sys.path.append(plugin_home)

        from lib.mapping import resolve_capability  # noqa: PLC0415

        # 模型看到的是编码后的工具名（plugin_tools.py 用 encode_tool_name
        # 处理过），映射表按限定名索引，所以要先解码
        try:
            from bianbu_agent.engines.names import decode_tool_name  # noqa: PLC0415
            tool_name = decode_tool_name(tool_name) or tool_name
        except Exception:  # noqa: BLE001 — 解码不可用就用原名
            pass

        tool_metadata = getattr(request, "tool_metadata", None)
        if not isinstance(tool_metadata, Mapping):
            tool = getattr(request, "tool", None)
            candidate = getattr(tool, "metadata", None)
            tool_metadata = candidate if isinstance(candidate, Mapping) else {}
        resolved = resolve_capability(
            tool_name, args, tool_metadata=dict(tool_metadata),
        )
        if resolved is None:
            # 未映射工具（D-5）：原来直接 return False —— 不弹审批也不判权，
            # 名字不含关键字的新工具就这么静默执行了。现按统一策略：
            # confirm（默认）弹审批交用户定，allow 放行，deny 交由
            # before_tool hook 的 SecurityDenied 拦截（那里能真正阻断）。
            try:
                from lib.mapping import unmapped_policy  # noqa: PLC0415
                policy = unmapped_policy()
            except Exception:  # noqa: BLE001
                policy = "confirm"
            if policy == "confirm":
                logger.info("未映射工具 %s 需人工审批（策略 unmapped=confirm）", tool_name)
                return True
            return False
        capability = resolved[0]
        if not capability:
            return False       # 显式免判权

        session_id, run_id = _runtime_context(request)

        if not session_id or not run_id:
            # Some LangGraph versions omit runtime.config during after_model.
            # Still perform the authoritative non-session check so an explicit
            # DENY is rejected before creating a misleading HITL prompt.
            logger.warning("工具调用缺少 Session/Run 身份，改用无上下文判权")
            decision = int(security_client_call(
                "check_permission", agent_id, capability,
            ))
            if decision == 2:
                raise _SecurityPolicyDenied(tool=tool_name, policy="deny")
            return decision == 1
        decision = int(security_client_call(
            "check_permission_v2", agent_id, capability,
            {"session_id": session_id, "run_id": run_id},
        ))
        # DENY is not an approval candidate.  Raising here is essential: the
        # Agent manifest is allowed to omit the optional security.before_tool
        # hook, while this Runtime middleware still owns the global security
        # boundary.  Returning False would let LangGraph invoke the tool after
        # securityd had explicitly recorded DENY (the observed echo bypass).
        if decision == 2:            # 2=DENY
            raise _SecurityPolicyDenied(tool=tool_name, policy="deny")
        need = decision == 1          # 1=ASK
        if need:
            logger.info("需人工审批：%s → %s", tool_name, capability)
        return need
    except Exception as e:  # noqa: BLE001 — 判定失败不应让工具调用崩掉
        if isinstance(e, _SecurityPolicyDenied):
            raise
        logger.warning("审批判定失败，按需确认处理：%s", e)
        return True


def agent_security_mode_is_yolo(agent_id: str | None = None) -> bool:
    """Return the persisted Security Agent mode without environment bypasses."""
    import os
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        # 没有 securityd 就没有持久化的模式可读；不要为一个必然失败的 D-Bus
        # 往返付钱，也不要让「securityd 不在」看起来像「用户开了 YOLO」。
        return False
    aid = str(agent_id or os.environ.get("COM_BIANBU_AGENT_ID")
              or os.environ.get("BIANBU_WORKER_AGENT_ID")
              or os.environ.get("BIANBU_AGENT_ID") or "com.bianbu.agent")
    try:
        from bianbu_agent.security.security_runtime import security_client_call
        value = security_client_call("get_agent_security_mode", aid)
        return isinstance(value, Mapping) and str(value.get("mode", "")).lower() == "yolo"
    except Exception:  # noqa: BLE001 - normal fail-safe path
        return False


def securityd_record_approval(*, tool_name: str, args: Mapping[str, Any],
                              session_id: str, run_id: str,
                              tool_metadata: Mapping[str, object] | None = None) -> bool:
    """将 Runtime HITL 的批准写入 securityd 的 once_per_session 信任表。

    HITL 是 Runtime/LangGraph 的用户交互，不会经过 securityd 的阻塞式
    RequestConfirmV2；批准恢复前由此调用受 caller/session 校验保护的 V2
    接口。对 trusted/always/L4 能力返回 False，不影响本次已批准调用。
    """
    import os
    import sys

    if not session_id or not run_id:
        return False
    try:
        plugin_home = _plugin_lib_path()
        if plugin_home is None:
            return False
        if plugin_home not in sys.path:
            sys.path.append(plugin_home)
        from lib.mapping import resolve_capability  # noqa: PLC0415
        try:
            from bianbu_agent.engines.names import decode_tool_name  # noqa: PLC0415
            tool_name = decode_tool_name(tool_name) or tool_name
        except Exception:  # noqa: BLE001
            pass
        resolved = resolve_capability(
            tool_name, args, tool_metadata=dict(tool_metadata or {}),
        )
        if resolved is None:
            return False
        capability = resolved[0]
        if not capability:
            return False
        from bianbu_agent.security.security_runtime import security_client_call
        agent_id = (
            os.environ.get("COM_BIANBU_AGENT_ID")
            or os.environ.get("BIANBU_WORKER_AGENT_ID")
            or os.environ.get("BIANBU_AGENT_ID")
            or "com.bianbu.agent"
        )
        return bool(security_client_call(
            "grant_session_trust_v2", agent_id, capability,
            {"session_id": session_id, "run_id": run_id},
        ))
    except Exception as exc:  # noqa: BLE001 — 批准本身不应被记录失败阻断
        logger.warning("HITL 批准写入 session trust 失败：%s", exc)
        return False


class BianbuSecurityMiddleware:
    """挂在 HumanInTheLoopMiddleware 之前（更外层）。

    继承 AgentMiddleware 在导入期完成 —— 该基类来自 langchain，
    在此惰性引入以便无 langchain 环境下本模块仍可被导入（便于单测）。
    """

    def __init__(self, *, enabled: bool = True):
        self.enabled = enabled

    def wrap_model_call(self, request, handler):
        from bianbu_agent.security.security_dlp import scan_request, buffered_model_config, scan_response, dlp_checks_disabled
        if not self.enabled:
            return handler(request)
        if dlp_checks_disabled(stage='model_call'):
            return handler(request)
        from langchain_core.runnables.config import set_config_context
        prepared = scan_request(request)
        with set_config_context(buffered_model_config()) as context:
            response = context.run(handler, prepared)
        return scan_response(response)

    async def awrap_model_call(self, request, handler):
        import asyncio
        from bianbu_agent.security.security_dlp import scan_request, buffered_model_config, scan_response, dlp_checks_disabled
        if not self.enabled:
            return await handler(request)
        if await asyncio.to_thread(dlp_checks_disabled, stage='model_call'):
            return await handler(request)
        prepared = await asyncio.to_thread(scan_request, request)
        from langchain_core.runnables.config import set_config_context
        with set_config_context(buffered_model_config()) as context:
            response = await asyncio.create_task(handler(prepared), context=context)
        return await asyncio.to_thread(scan_response, response)

    # —— LangChain 中间件协议 ——
    async def awrap_tool_call(self, request, handler):
        if not self.enabled:
            return await handler(request)

        request = self._apply_dlp_masking(request)

        try:
            return await handler(request)
        except PermissionError as exc:
            return self._handle_denial(request, exc)

    def wrap_tool_call(self, request, handler):
        """同步版本（LangChain 会按可用性选择调用哪个）。"""
        if not self.enabled:
            return handler(request)
        request = self._apply_dlp_masking(request)
        try:
            return handler(request)
        except PermissionError as exc:
            return self._handle_denial(request, exc)

    # —— 内部 ——
    def _apply_dlp_masking(self, request):
        """把 before_tool hook 脱敏后的参数应用到实际调用（INT-RT-10）。"""
        from bianbu_agent.security.security_dlp import current_agent_id
        if agent_security_mode_is_yolo(current_agent_id()):
            return request
        meta = extract_security_metadata(getattr(request, "state", None))
        masked = meta.get("dlp_masked_args")
        if not isinstance(masked, Mapping) or not masked:
            return request
        try:
            tool_call = dict(getattr(request, "tool_call", {}) or {})
            if not tool_call:
                return request
            tool_call["args"] = dict(masked)
            logger.info("DLP 已脱敏工具参数：%s", tool_call.get("name"))
            return request.override(tool_call=tool_call)
        except Exception as e:  # noqa: BLE001 — 改写失败宁可用原参数，别断调用
            logger.warning("DLP 参数改写失败，沿用原参数：%s", e)
            return request

    def _handle_denial(self, request, exc: PermissionError):
        """明确的 deny 一律终止整轮 run（INT-RT-06）。

        三态模型里 deny 是用户的明确决定，不能靠 Agent 换个说法重试绕过；
        必须冒泡，不能落进 create_agent 的 handle_tool_errors=True 变成
        ToolMessage 让 Agent 换路径重来。
        """
        terminate = bool(getattr(exc, "should_terminate_run", False))
        tool_call = dict(getattr(request, "tool_call", {}) or {})
        name = str(tool_call.get("name") or "")

        if terminate:
            logger.warning("越权拒绝，终止整轮 run：%s", name)
            raise exc

        logger.info("越权拒绝，交回模型：%s", name)
        from langchain_core.messages import ToolMessage  # noqa: PLC0415

        return ToolMessage(
            content=str(exc),
            tool_call_id=str(tool_call.get("id") or ""),
            name=name,
            status="error",
        )


def build_security_middleware(*, enabled: bool = True):
    """构造中间件实例，动态混入 langchain 的 AgentMiddleware 基类。

    不在模块顶层继承，是为了让本模块在没有 langchain 的环境里也能导入
    （单测只验分档与脱敏逻辑时不需要 langchain）。
    """
    try:
        from langchain.agents.middleware import AgentMiddleware  # noqa: PLC0415
    except ImportError:
        return BianbuSecurityMiddleware(enabled=enabled)

    class _Middleware(BianbuSecurityMiddleware, AgentMiddleware):
        pass

    return _Middleware(enabled=enabled)


def build_security_hitl_middleware(
    interrupt_on: Mapping[str, Any],
    *,
    protected_names: Collection[str] = (),
    terminal_names: Collection[str] = (),
    terminal_when: Callable[[Any], bool] | None = None,
    memory_names: Collection[str] = (),
    memory_when: Callable[[Any], bool] | None = None,
    tool_metadata: Mapping[str, Mapping[str, object]] | None = None,
):
    """构造兼容旧 LangChain 的动态安全审批中间件。

    LangChain 1.2.x 的 ``HumanInTheLoopMiddleware`` 不读取配置中的
    ``when`` 谓词，导致只靠 ``before_tool`` 写入的 CONFIRM 标记被工具执行
    之后才看到。这里在其 ``after_model`` 阶段先做一次 securityd 预判，
    再委托官方中间件处理 interrupt/resume，保持前端协议和批准恢复逻辑
    不变。
    """
    from copy import copy
    from types import SimpleNamespace

    from bianbu_agent.engines.langgraph.approval import ApprovalMiddleware

    protected = frozenset(str(name) for name in protected_names)
    terminals = frozenset(str(name) for name in terminal_names)
    memories = frozenset(str(name) for name in memory_names)
    metadata_by_name = {
        str(name): dict(metadata)
        for name, metadata in (tool_metadata or {}).items()
    }

    class _DynamicSecurityHITL(ApprovalMiddleware):
        def _needs_interrupt(self, tool_call: Mapping[str, Any], runtime: Any) -> bool:
            name = str(tool_call.get("name") or "")
            if agent_security_mode_is_yolo():
                return False
            # 直接在 after_model 时序调用 securityd；before_tool hook 仍负责
            # 最终 DENY/审计，二者不能互相替代。
            request = SimpleNamespace(
                tool_call=tool_call,
                runtime=runtime,
                tool_metadata=metadata_by_name.get(name, {}),
            )
            # Always ask securityd first.  An explicit DENY raises here and
            # must not be hidden by a static approval marker (otherwise the
            # UI prompts for approval and then the before_tool hook blocks it).
            if securityd_requires_confirm(request):
                return True
            if name in protected:
                return True
            if name in terminals and terminal_when is not None:
                return bool(terminal_when(request))
            # AgentMemory remove is destructive in an argument, not in the tool
            # contract, so MCP annotations cannot express it.  Ask the predicate
            # after securityd so an approved session cannot delete silently.
            if name in memories and memory_when is not None:
                return bool(memory_when(request))
            return False

        def _filtered_delegate(self, state: Any, runtime: Any):
            active = {
                name: config
                for name, config in self.interrupt_on.items()
                if any(
                    str(call.get("name") or "") == name
                    and self._needs_interrupt(call, runtime)
                    for call in self._last_tool_calls(state)
                )
            }
            delegate = copy(self)
            delegate.interrupt_on = active
            return delegate

        @staticmethod
        def _last_tool_calls(state: Any) -> tuple[Mapping[str, Any], ...]:
            messages = state.get("messages", []) if isinstance(state, Mapping) else []
            for message in reversed(messages):
                calls = getattr(message, "tool_calls", None)
                if calls:
                    return tuple(call for call in calls if isinstance(call, Mapping))
            return ()

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._metadata_by_name = metadata_by_name

        async def aafter_model(self, state: Any, runtime: Any):
            # 先按本次调用的实际 need 过滤 interrupt_on，再走 ApprovalMiddleware
            # 的"在 Run 内等待决定"路径；同步 after_model 已由基类显式拒绝。
            delegate = self._filtered_delegate(state, runtime)
            return await ApprovalMiddleware.aafter_model(delegate, state, runtime)

    return _DynamicSecurityHITL(
        interrupt_on=dict(interrupt_on),
        description_prefix="工具执行需要人工批准",
    )
