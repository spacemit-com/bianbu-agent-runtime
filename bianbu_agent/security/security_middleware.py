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

  3. 审批判定（INT-RT-12 的配套）—— 在 `after_model` 阶段执行
     `bianbu.security` 的 BEFORE_TOOL Hook 链，用它返回的 CONFIRM 决定是否
     interrupt。执行时不再重跑这条链，因此一次工具调用只有一次判权。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection
from typing import Any, Callable, Mapping

logger = logging.getLogger("bianbu_agent.security")

__all__ = [
    "APPROVAL_ALLOW",
    "APPROVAL_ASK",
    "APPROVAL_DENY",
    "APPROVAL_UNKNOWN",
    "BianbuSecurityMiddleware",
    "build_security_hitl_middleware",
    "securityd_approval_decision",
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
    只能按约定路径找。用户插件目录优先；未安装该插件的专家仍可使用随
    Runtime 包发布的系统插件，否则全局安全中间件会因缺少映射实现而把
    securityd 的明确 ALLOW 降级成 UNKNOWN。只有两者都不存在时才返回 None。
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
    try:
        from bianbu_agent.system_resources import system_resource_dir

        roots.append(str(system_resource_dir("plugins") / _PLUGIN_ID))
    except Exception:  # noqa: BLE001 - source deployments may not ship a system root
        pass
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


#: Result of asking securityd whether one call may proceed.  ``UNKNOWN`` means
#: policy could not be evaluated at all (plugin/client missing, transport
#: error); it is distinct from an authoritative ``ALLOW`` so the caller can
#: fall back to a tool's static contract instead of silently dropping it.
APPROVAL_ALLOW = "allow"
APPROVAL_ASK = "ask"
APPROVAL_DENY = "deny"
APPROVAL_UNKNOWN = "unknown"


def securityd_approval_decision(request: Any) -> str:
    """Ask securityd for the decision when the Hook chain is unavailable.

    正常路径由 :func:`run_tool_guard_hooks` 在 ``after_model`` 阶段执行
    ``bianbu.security`` 的 BEFORE_TOOL Hook 链（与工具执行前是同一套
    handler），HITL 只消费那一份结果：同一个工具调用只有 hook 里的一次
    CheckPermission，不会再单独判一遍。

    只有插件没装、Hook 不可用或调用点拿不到 ``hook_runner`` 时才走这里，
    用「工具名 → 能力 → CheckPermissionV2」兜底。结果不缓存，避免 WebUI
    修改策略后 Runtime 继续使用旧判定。

    返回值必须是三态而不是布尔：调用方还要区分

    * ``ALLOW``  —— securityd 明确放行，静态 ``requires_approval`` 不得再
      覆盖它，否则策略页显示 allow、实际仍弹窗；
    * ``ASK``    —— 需要人工确认；
    * ``DENY``   —— 拒绝（本函数直接抛 ``_SecurityPolicyDenied``）；
    * ``UNKNOWN``—— 判不出来，调用方按工具自身的静态契约兜底。
    """
    import os
    import sys

    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        # securityd 不在场：交给工具静态契约（terminal 的 rm、MCP 的
        # destructiveHint 等），既不放行也不凭空要求确认。
        return APPROVAL_UNKNOWN

    try:
        tc = getattr(request, "tool_call", None) or {}
        tool_name = str(tc.get("name") or "")
        if not tool_name:
            return APPROVAL_UNKNOWN
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
                return APPROVAL_ALLOW
        except Exception as exc:  # noqa: BLE001 — retain fail-safe confirmation
            logger.debug("读取 Agent security mode 失败，继续常规判权：%s", exc)

        plugin_home = _plugin_lib_path()
        if plugin_home is None:
            logger.warning("找不到 bianbu.security 插件目录，审批预判按需确认处理")
            return APPROVAL_UNKNOWN
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
            # ask 弹审批交用户定，allow 放行，deny 交由 before_tool hook 的
            # SecurityDenied 拦截（那里能真正阻断）。
            try:
                from lib.mapping import unmapped_policy  # noqa: PLC0415
                policy = unmapped_policy()
            except Exception:  # noqa: BLE001
                policy = "ask"
            if policy == "ask":
                logger.info("未映射工具 %s 需人工审批（策略 unmapped=ask）", tool_name)
                return APPROVAL_ASK
            return APPROVAL_ALLOW if policy == "allow" else APPROVAL_DENY
        capability = resolved[0]
        if not capability:
            return APPROVAL_ALLOW       # 显式免判权

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
            return APPROVAL_ASK if decision == 1 else APPROVAL_ALLOW
        decision = int(security_client_call(
            "check_permission_v2", agent_id, capability, "",
            {"session_id": session_id, "run_id": run_id},
        ))
        # DENY is not an approval candidate.  Raising here is essential: the
        # Agent manifest is allowed to omit the optional security.before_tool
        # hook, while this Runtime middleware still owns the global security
        # boundary.  Returning False would let LangGraph invoke the tool after
        # securityd had explicitly recorded DENY (the observed echo bypass).
        if decision == 2:            # 2=DENY
            raise _SecurityPolicyDenied(tool=tool_name, policy="deny")
        if decision == 1:            # 1=ASK
            logger.info("需人工审批：%s → %s", tool_name, capability)
            return APPROVAL_ASK
        return APPROVAL_ALLOW
    except Exception as e:  # noqa: BLE001 — 判定失败不应让工具调用崩掉
        if isinstance(e, _SecurityPolicyDenied):
            raise
        logger.warning("审批判定失败，按需确认处理：%s", e)
        return APPROVAL_UNKNOWN


def securityd_requires_confirm(request: Any) -> bool:
    """Compatibility wrapper: only an authoritative ASK needs a prompt."""

    return securityd_approval_decision(request) == APPROVAL_ASK


def tool_call_key(tool_call: Mapping[str, Any]) -> str:
    """Stable identity of one tool call inside a model message."""

    call_id = str(tool_call.get("id") or "").strip()
    if call_id:
        return call_id
    name = str(tool_call.get("name") or "")
    arguments = tool_call.get("args") or {}
    return f"{name}:{json.dumps(arguments, sort_keys=True, default=str)}"


def _securityd_metadata(meta: Any) -> Mapping[str, Any]:
    """Read the securityd payload out of an aggregated Hook ContextPatch."""

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


async def run_tool_guard_hooks(
    *,
    runtime: Any,
    tool_call: Mapping[str, Any],
    tool_metadata: Mapping[str, Any],
    hook_runner: Any,
    hook_ids: Collection[str],
) -> tuple[str, Mapping[str, Any] | None]:
    """Run the security BEFORE_TOOL Hook chain once for one tool call.

    This is the only place one tool call is judged.  It runs in
    ``after_model`` — the moment the HITL middleware needs the decision — so
    the audit record and the approval prompt share one Hook result instead of
    calling CheckPermission twice (once here, once at execution).

    Returns ``(decision, effective_arguments)``:

    * ``ALLOW`` / ``ASK`` / ``DENY`` come from the Hook chain;
    * ``UNKNOWN`` means the chain could not judge, so the caller falls back to
      the static tool contract;
    * ``effective_arguments`` carries DLP-restored or masked arguments that the
      tool invocation must use.

    A Hook denial (``PermissionError``) propagates on purpose: a BLOCK has to
    stop the Run before the tool executes.
    """

    import os
    from types import SimpleNamespace

    from bianbu_agent.plugins.contributions import HookPoint, RunContext

    if hook_runner is None or not hook_ids:
        return APPROVAL_UNKNOWN, None
    session_id, run_id = _runtime_context(SimpleNamespace(runtime=runtime))
    if not session_id or not run_id:
        return APPROVAL_UNKNOWN, None
    name = str(tool_call.get("name") or "")
    arguments = tool_call.get("args")
    context = RunContext(
        run_id=run_id,
        session_id=session_id,
        agent_id=(
            os.environ.get("COM_BIANBU_AGENT_ID")
            or os.environ.get("BIANBU_WORKER_AGENT_ID")
            or os.environ.get("BIANBU_AGENT_ID")
            or "com.bianbu.agent"
        ),
        request=name,
        metadata={
            "tool": name,
            "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
            "tool_metadata": dict(tool_metadata or {}),
        },
    )
    patch = await hook_runner.run_hooks(
        HookPoint.BEFORE_TOOL, context, hook_ids=tuple(hook_ids))
    inner = _securityd_metadata(getattr(patch, "metadata", None))
    decision = str(inner.get("decision") or "").strip().upper()
    if decision == "CONFIRM" or inner.get("requires_approval") is True:
        outcome = APPROVAL_ASK
    elif decision == "ALLOW":
        outcome = APPROVAL_ALLOW
    elif decision == "BLOCK":
        outcome = APPROVAL_DENY
    else:
        outcome = APPROVAL_UNKNOWN
    effective = getattr(patch, "tool_arguments", None)
    if not isinstance(effective, Mapping):
        masked = inner.get("dlp_masked_args")
        effective = masked if isinstance(masked, Mapping) else None
    return outcome, (dict(effective) if isinstance(effective, Mapping) else None)


def _apply_effective_arguments(
    result: Any,
    state: Any,
    arguments: Mapping[str, Mapping[str, Any]],
) -> Any:
    """Apply DLP-restored/masked arguments to the tool calls about to run.

    The security Hook chain no longer runs a second time at execution, so the
    argument rewrite it produced in ``after_model`` has to be written onto the
    surviving tool calls here.
    """

    if not arguments:
        return result
    messages = list(result.get("messages") or ()) if isinstance(result, Mapping) else []
    target = messages[0] if messages else None
    if target is None:
        state_messages = (
            state.get("messages", ()) if isinstance(state, Mapping) else ()
        )
        for message in reversed(list(state_messages)):
            if getattr(message, "tool_calls", None):
                target = message
                break
    if target is None:
        return result
    changed = False
    for call in getattr(target, "tool_calls", ()) or ():
        if not isinstance(call, dict):
            continue
        effective = arguments.get(tool_call_key(call))
        if effective is None:
            continue
        call["args"] = dict(effective)
        changed = True
    if not changed:
        return result
    return result if result is not None else {"messages": [target]}


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
    """Record a Runtime HITL approval for exactly this tool call.

    The approval is written to the audit chain only.  It must not create a
    ``session_trust`` grant: that made every later call of the same tool in
    the session skip the prompt, so a high-risk tool such as ``terminal``
    could run again without the user ever seeing a confirmation.  Each call
    that policy marks ``ask`` has to be approved on its own.
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
        scope = str(resolved[1]) if len(resolved) > 1 else ""
        from bianbu_agent.security.security_runtime import security_client_call
        agent_id = (
            os.environ.get("COM_BIANBU_AGENT_ID")
            or os.environ.get("BIANBU_WORKER_AGENT_ID")
            or os.environ.get("BIANBU_AGENT_ID")
            or "com.bianbu.agent"
        )
        written = security_client_call(
            "audit_log", agent_id, "ask", f"{capability}:{scope}",
            "approved=true",
        )
        return int(written or 0) > 0
    except Exception as exc:  # noqa: BLE001 — 批准本身不应被记录失败阻断
        logger.warning("HITL 批准写入审计失败：%s", exc)
        return False


def _sanitize_error_result(request, result, rewritten: bool):
    """Strip restored plaintext from an error ToolMessage built by ToolNode.

    ToolNode converts a validation failure into an error ``ToolMessage`` whose
    text embeds the kwargs it was given.  When this call went through handle
    restoration those kwargs are plaintext, so the echoed segment is dropped
    without inspecting it: the message is built with ``repr``, which escapes
    newlines and quotes, and matching values against it would silently miss.
    The validation block itself can also quote ``input_value`` and carry a URL,
    so the rebuilt message keeps only the field path and the error kind.
    """
    from langchain_core.messages import ToolMessage

    if not rewritten or not isinstance(result, ToolMessage):
        return result
    if str(getattr(result, "status", "")) != "error":
        return result
    content = str(result.content or "")
    tool_call = getattr(request, "tool_call", {}) or {}
    kinds = list(dict.fromkeys(
        match.group("kind") for match in _ERROR_KIND.finditer(content)
    ))
    return ToolMessage(
        content=_rejection_content(tool_call.get("args"), kinds),
        name=str(tool_call.get("name") or result.name or ""),
        tool_call_id=str(tool_call.get("id") or result.tool_call_id or ""),
        status="error",
    )


_ERROR_KIND = re.compile(r"\[type=(?P<kind>[a-z_]+)[,\]]")


def _rejected_arguments_message(request, exc: Exception, rewritten: bool):
    """Answer an argument rejection ourselves when a restore is involved.

    ``ToolInvocationError`` carries the plaintext kwargs in its message and in
    the traceback of the chain, so returning a value here keeps both out of
    logs and out of the model's context.
    """
    if not rewritten or type(exc).__name__ != "ToolInvocationError":
        return None
    from langchain_core.messages import ToolMessage

    tool_call = getattr(request, "tool_call", {}) or {}
    kinds = list(dict.fromkeys(
        str(item.get("type"))
        for item in (getattr(exc, "filtered_errors", None) or ())
        if isinstance(item, Mapping) and item.get("type")
    ))
    content = _rejection_content(tool_call.get("args"), kinds)
    return ToolMessage(
        content=content,
        name=str(tool_call.get("name") or ""),
        tool_call_id=str(tool_call.get("id") or ""),
        status="error",
    )


def _rejection_content(arguments: object, kinds: list[str]) -> str:
    """Name the fields and error kinds without ever quoting a value."""
    fields = sorted(str(name) for name in arguments) if isinstance(arguments, Mapping) else []
    summary = "fields " + ", ".join(fields) if fields else ""
    if kinds:
        summary += (" " if summary else "") + "(" + ", ".join(kinds) + ")"
    return "Tool arguments were rejected" + (f": {summary}" if summary else "")


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

        request, rewritten = self._prepare_tool_arguments(request)
        try:
            result = await handler(request)
            return _sanitize_error_result(request, result, rewritten)
        except PermissionError as exc:
            return self._handle_denial(request, exc)
        except Exception as exc:
            rejected = _rejected_arguments_message(request, exc, rewritten)
            if rejected is not None:
                return rejected
            raise

    def wrap_tool_call(self, request, handler):
        """同步版本（LangChain 会按可用性选择调用哪个）。"""
        if not self.enabled:
            return handler(request)
        request, rewritten = self._prepare_tool_arguments(request)
        try:
            result = handler(request)
            return _sanitize_error_result(request, result, rewritten)
        except PermissionError as exc:
            return self._handle_denial(request, exc)
        except Exception as exc:
            rejected = _rejected_arguments_message(request, exc, rewritten)
            if rejected is not None:
                return rejected
            raise

    # —— 内部 ——
    def _prepare_tool_arguments(self, request):
        """Restore handles and apply hook rewrites; report whether either ran."""
        request, restored = self._restore_dlp_references(request)
        request, rewritten = self._apply_dlp_masking(request)
        return request, restored or rewritten

    def _restore_dlp_references(self, request):
        """Turn model-facing DLP handles back into values before the tool runs.

        This is the trusted side of the boundary: the model only ever sees the
        handle, while a local tool receives the original argument.  The handle
        is scoped to the current session and an expired/forged handle denies the
        call instead of being passed through.
        """
        from bianbu_agent.security.security_dlp import (
            contains_reference,
            current_agent_id,
            current_session_id,
            resolve_references,
        )
        tool_call = dict(getattr(request, "tool_call", {}) or {})
        if not tool_call:
            return request, False
        args = tool_call.get("args")
        if not isinstance(args, (dict, list, str)):
            return request, False
        if not contains_reference(args):
            return request, False
        if agent_security_mode_is_yolo(current_agent_id()):
            return request, False
        session_id, _run_id = _runtime_context(request)
        try:
            restored, count = resolve_references(
                args, session_id=session_id or current_session_id(),
                tool=str(tool_call.get("name") or ""))
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001 - fail closed at the tool boundary
            from bianbu_agent.security.security_dlp import DLPDenied
            raise DLPDenied('DLP 引用还原失败，已阻止工具调用',
                            stage='pre_tool_call',
                            tool=str(tool_call.get("name") or '')) from e
        if not count:
            return request, False
        tool_call["args"] = restored
        logger.info("DLP 已还原 %d 个工具参数引用：%s",
                    count, tool_call.get("name"))
        return request.override(tool_call=tool_call), True

    def _apply_dlp_masking(self, request):
        """把 before_tool hook 脱敏后的参数应用到实际调用（INT-RT-10）。"""
        from bianbu_agent.security.security_dlp import current_agent_id
        if agent_security_mode_is_yolo(current_agent_id()):
            return request, False
        meta = extract_security_metadata(getattr(request, "state", None))
        restored = meta.get("dlp_restored_args")
        masked = meta.get("dlp_masked_args")
        replacement = restored if isinstance(restored, Mapping) and restored else masked
        if not isinstance(replacement, Mapping) or not replacement:
            return request, False
        try:
            tool_call = dict(getattr(request, "tool_call", {}) or {})
            if not tool_call:
                return request, False
            tool_call["args"] = dict(replacement)
            logger.info("DLP 已应用工具参数改写：%s", tool_call.get("name"))
            return request.override(tool_call=tool_call), True
        except Exception as e:  # noqa: BLE001 — 改写失败宁可用原参数，别断调用
            logger.warning("DLP 参数改写失败，沿用原参数：%s", e)
            return request, False

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
    hook_runner: Any = None,
    hook_ids: Collection[str] = (),
    enabled: bool = True,
):
    """构造兼容旧 LangChain 的动态安全审批中间件。

    LangChain 1.2.x 的 ``HumanInTheLoopMiddleware`` 不读取配置中的
    ``when`` 谓词。这里在 ``after_model`` 阶段直接执行 ``bianbu.security``
    的 BEFORE_TOOL Hook 链（与工具执行前是同一套 handler），用它的决策
    决定是否 interrupt；工具执行时不再重复这道判权。

    判权因此只有一个入口、一条审计：Hook 链里的 ``sec_capability`` 调用的
    CheckPermission 就是本次调用的唯一一次判权，HITL 不再单独调用。
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
    security_hooks = tuple(str(name) for name in hook_ids)

    class _DynamicSecurityHITL(ApprovalMiddleware):
        _securityd_enabled = bool(enabled)
        _hook_runner = hook_runner
        _decisions: Mapping[str, str] = {}

        def _needs_interrupt(self, tool_call: Mapping[str, Any], runtime: Any) -> bool:
            name = str(tool_call.get("name") or "")
            if not self._securityd_enabled:
                # No Plugin, no securityd: only the tool's own static contract
                # decides whether the user must approve this call.
                if name in protected:
                    return True
                request = SimpleNamespace(
                    tool_call=tool_call,
                    runtime=runtime,
                    tool_metadata=metadata_by_name.get(name, {}),
                )
                if name in terminals and terminal_when is not None:
                    return bool(terminal_when(request))
                if name in memories and memory_when is not None:
                    return bool(memory_when(request))
                return False
            # The BEFORE_TOOL Hook chain is the single judge.  Its decision is
            # computed once in ``aafter_model`` and cached per tool call; the
            # direct securityd call only remains for installations where the
            # chain cannot run.
            request = SimpleNamespace(
                tool_call=tool_call,
                runtime=runtime,
                tool_metadata=metadata_by_name.get(name, {}),
            )
            cached = self._decisions.get(tool_call_key(tool_call))
            if cached is None:
                if agent_security_mode_is_yolo():
                    return False
                decision = securityd_approval_decision(request)
            else:
                decision = cached
            if decision == APPROVAL_DENY:
                # A Hook chain that reports BLOCK without raising still has to
                # stop the Run; never turn it into an approval prompt.
                raise _SecurityPolicyDenied(tool=name, policy="deny")
            if decision == APPROVAL_ASK:
                return True
            if decision == APPROVAL_ALLOW:
                return False
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

        def _filtered_delegate(
            self,
            state: Any,
            runtime: Any,
            decisions: Mapping[str, str] | None = None,
        ):
            delegate = copy(self)
            delegate._decisions = dict(decisions or {})
            delegate.interrupt_on = {
                name: config
                for name, config in self.interrupt_on.items()
                if any(
                    str(call.get("name") or "") == name
                    and delegate._needs_interrupt(call, runtime)
                    for call in delegate._last_tool_calls(state)
                )
            }
            return delegate

        async def _precheck(
            self, state: Any, runtime: Any
        ) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
            """Judge every tool call once through the security Hook chain."""

            decisions: dict[str, str] = {}
            effective_arguments: dict[str, Mapping[str, Any]] = {}
            if (
                not self._securityd_enabled
                or self._hook_runner is None
                or not security_hooks
            ):
                return decisions, effective_arguments
            for call in self._last_tool_calls(state):
                name = str(call.get("name") or "")
                decision, effective = await run_tool_guard_hooks(
                    runtime=runtime,
                    tool_call=call,
                    tool_metadata=metadata_by_name.get(name, {}),
                    hook_runner=self._hook_runner,
                    hook_ids=security_hooks,
                )
                key = tool_call_key(call)
                if decision != APPROVAL_UNKNOWN:
                    decisions[key] = decision
                if effective is not None:
                    effective_arguments[key] = effective
            return decisions, effective_arguments

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
            # 判权只发生在 Hook 链里；这里先取结果，再按实际的 need 过滤
            # interrupt_on，最后走 ApprovalMiddleware 的"在 Run 内等待决定"
            # 路径。同步 after_model 已由基类显式拒绝。
            decisions, effective_arguments = await self._precheck(state, runtime)
            delegate = self._filtered_delegate(state, runtime, decisions)
            result = await ApprovalMiddleware.aafter_model(delegate, state, runtime)
            return _apply_effective_arguments(
                result, state, effective_arguments)

    return _DynamicSecurityHITL(
        interrupt_on=dict(interrupt_on),
        description_prefix="工具执行需要人工批准",
    )
