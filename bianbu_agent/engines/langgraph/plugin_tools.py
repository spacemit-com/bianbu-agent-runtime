from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
import inspect
import json
import logging
import os
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

from bianbu_agent import memory_identity
from bianbu_agent.context import compact_tool_result
from bianbu_agent.engines import encode_tool_name
from bianbu_agent.plugins.contributions import (
    HookPoint,
    RunContext,
    ToolCallContext,
    ToolContribution,
)

logger = logging.getLogger("bianbu_agent.security")

SYSTEM_UPGRADE_TOOL = 'bianbu.bibit.intent/bibit-admin/pkg_update'


def upgrade_result_message(name: str, content: str) -> str | None:
    if name != SYSTEM_UPGRADE_TOOL:
        return None
    value = content
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return value
        elif isinstance(value, dict):
            if isinstance(value.get('message'), str):
                return value['message']
            value = value.get('result')
        else:
            break
    return '系统更新工具已返回，请查看工具结果；这不表示系统升级已完成。'


def upgrade_submitted(name: str, content: str) -> bool:
    """Only hand off after an explicit successful background submission."""
    if name != SYSTEM_UPGRADE_TOOL:
        return False
    value: Any = content
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return False
        elif isinstance(value, dict):
            if value.get('ok') is True and isinstance(value.get('message'), str):
                return '已提交' in value['message']
            value = value.get('result')
        else:
            return False
    return False

# One warning per identity field is enough: a Run makes many tool calls, and the
# point of the log is to say *which* field the Runtime had to recover, not to
# repeat it for every call.
_recovered_identities: set[str] = set()


def _recover_identity(field: str, environment: str, recovered: str) -> str:
    """Fall back to the identity memoryd has registered for this Worker.

    Recovering is the only correct answer once ``configurable`` does not carry
    the field: memoryd compares the value with the registration the Worker
    published from its environment, so a placeholder is read as an attempt to
    speak for someone else and the call is refused (K3 2026-09-22).
    """

    if field not in _recovered_identities:
        _recovered_identities.add(field)
        source = (
            "环境身份"
            if os.environ.get(environment, "").strip()
            else f"默认身份（{environment} 未设置）"
        )
        logger.warning(
            "工具调用上下文缺少 %s，已回退为%s %s=%s；"
            "memoryd 按注册身份校验，缺字段或占位身份会被判为身份不符"
            "（Unauthenticated）并拒绝记忆读写",
            field,
            source,
            environment,
            recovered,
        )
    return recovered


def _tool_identity(configurable: Mapping[str, object]) -> tuple[str, str]:
    """Resolve ``(account_id, agent_id)`` for one tool call.

    ``configurable`` holds the identity the Run was started with. An explicit
    empty account stays empty so ``ToolCallContext`` rejects it — a caller that
    says "no account" is a programming error, not a missing default.
    """

    account = configurable.get("account_id")
    if account is None:
        account = _recover_identity(
            "account_id",
            memory_identity.ACCOUNT_ENV,
            memory_identity.env_account_id(),
        )
    agent = configurable.get("agent_id")
    agent_text = "" if agent is None else str(agent).strip()
    if not agent_text:
        agent_text = _recover_identity(
            "agent_id",
            memory_identity.AGENT_NAME_ENV,
            memory_identity.env_agent_name(),
        )
    return str(account), agent_text


def _hook_snapshot(configurable: Mapping[str, object]) -> tuple[str, ...] | None:
    if "hook_ids" not in configurable:
        return None
    raw = configurable["hook_ids"]
    if isinstance(raw, (str, bytes, bytearray)):
        return (str(raw),)
    try:
        return tuple(str(item) for item in raw)
    except TypeError:
        return ()


def _tool_result_failed(result: Any) -> bool:
    """工具结果是否自述失败。

    目前唯一可靠信号是 MCP 的 ``isError``（``mcp/plugin.py`` 存成 metadata 的
    ``mcp_error``）。服务端必须在 ``ok: false`` 时置位：少了它，Runtime 看到的
    永远是「调用成功」，事后复核会把一句「未通过」贴到一次根本没跑起来的调用
    后面（见 Bibit 侧 ``mcp_server/server.py::_mark_tool_failure``）。
    """

    metadata = getattr(result, "metadata", None)
    return isinstance(metadata, Mapping) and metadata.get("mcp_error") is True


def _failure_reason(result: Any) -> str:
    """日志里带一句失败原因，便于从 Runtime 日志追到工具侧结论。"""

    content = str(getattr(result, "content", "") or "").strip()
    return content[:120] if content else "no message"


def _tool_context(
    config: RunnableConfig,
    *,
    hook_ids: tuple[str, ...] | None = None,
) -> ToolCallContext:
    configurable = config.get("configurable", {})
    if not isinstance(configurable, Mapping):
        configurable = {}
    metadata = {str(key): value for key, value in configurable.items()}
    if hook_ids is not None:
        metadata["hook_ids"] = hook_ids
    account_id, agent_id = _tool_identity(configurable)
    return ToolCallContext(
        run_id=str(
            configurable.get("run_id")
            or configurable.get("request_id")
            or "unknown-run"
        ),
        session_id=str(
            configurable.get("session_id")
            or configurable.get("product_session_id")
            or configurable.get("thread_id")
            or "unknown-session"
        ),
        agent_id=agent_id,
        workspace_ref=str(configurable.get("workspace_ref") or ""),
        metadata=metadata,
        account_id=account_id,
        progress=_stream_progress_sink,
    )


def _stream_progress_sink(payload: Mapping[str, object]) -> None:
    """把工具自述的进度写进 LangGraph custom stream。

    engine 只把 ``{"kind": "progress"}`` 转成 ``RunEvent("progress")``；
    这里不做任何过滤，工具负责给出可展示的阶段文案。不在图内执行时
    ``get_stream_writer()`` 抛 ``RuntimeError``，静默丢弃而不是让工具失败。
    """

    from langgraph.config import get_stream_writer

    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({"kind": "progress", "payload": dict(payload)})


def _hook_context(
    context: ToolCallContext,
    qualified_name: str,
    arguments: Mapping[str, object],
    **metadata: object,
) -> RunContext:
    # RunnableConfig also holds live engine controls and LangGraph callbacks.
    # These belong to the in-process Tool context, never the command Hook JSON
    # protocol. Preserve public extension metadata and validate it normally.
    internal = {"run_control", "checkpoint_map", "checkpoint_id", "checkpoint_ns"}
    public_metadata = {
        key: value for key, value in context.metadata.items()
        if key not in internal and not key.startswith("__pregel_")
    }
    return RunContext(
        run_id=context.run_id,
        session_id=context.session_id,
        agent_id=context.agent_id,
        request=qualified_name,
        metadata={
            **public_metadata,
            "tool": qualified_name,
            "arguments": dict(arguments),
            **metadata,
        },
        account_id=context.account_id,
    )


def _adapt_tool(
    contribution: ToolContribution,
    hook_runner: object | None,
    hook_ids: tuple[str, ...] | None,
    hook_ids_resolver: Callable[[str], Awaitable[tuple[str, ...]]] | None,
) -> StructuredTool:
    async def invoke(config: RunnableConfig, **arguments: Any) -> str:
        configurable = config.get("configurable", {})
        if not isinstance(configurable, Mapping):
            configurable = {}
        selected_hook_ids = _hook_snapshot(configurable)
        if selected_hook_ids is None:
            selected_hook_ids = hook_ids
        if selected_hook_ids is None and hook_ids_resolver is not None:
            agent_id = _tool_identity(configurable)[1]
            selected_hook_ids = tuple(await hook_ids_resolver(agent_id))
        context = _tool_context(config, hook_ids=selected_hook_ids)
        qualified_name = str(
            contribution.spec.metadata.get("qualified_id")
            or contribution.spec.name
        )
        tool_metadata = dict(contribution.spec.metadata)
        effective_args = dict(arguments)
        context_sections = []
        if hook_runner is not None:
            patch = await _run_hooks(
                hook_runner,
                HookPoint.BEFORE_TOOL,
                _hook_context(
                    context, qualified_name, arguments,
                    tool_metadata=tool_metadata,
                ),
                selected_hook_ids,
            )
            context_sections.extend(getattr(patch, "context_sections", ()))
            # DLP 脱敏：hook 层拿不到参数改写通道，把结果放在
            # metadata["bianbu.security"]["securityd"]["dlp_masked_args"]，
            # 由这里应用到实际调用。不应用的话脱敏就白算了。
            masked = _dlp_masked_args(patch)
            if masked is not None:
                effective_args = masked
                logger.info("工具 %s 的参数已按 DLP 策略脱敏", qualified_name)
        result = await contribution.handler(context, dict(effective_args))
        tool_failed = _tool_result_failed(result)
        content = result.content
        if hook_runner is not None:
            patch = await _run_hooks(
                hook_runner,
                HookPoint.AFTER_TOOL,
                _hook_context(
                    context,
                    qualified_name,
                    effective_args,
                    tool_metadata=tool_metadata,
                    result_metadata=dict(result.metadata),
                    result=content,
                ),
                selected_hook_ids,
            )
            from bianbu_agent.security.security_middleware import extract_security_metadata
            from bianbu_agent.security.security_dlp import DLPDenied

            security = extract_security_metadata(patch)
            if security.get("result_blocked"):
                raise DLPDenied(
                    str(security.get("block_reason") or "工具返回值被 DLP 策略拦截"),
                    stage="post_tool_call", tool=qualified_name,
                )
            if "dlp_masked_result" in security:
                masked_result = security["dlp_masked_result"]
                if not isinstance(masked_result, str):
                    raise DLPDenied("工具返回值脱敏结果无效", stage="post_tool_call",
                                    tool=qualified_name)
                content = masked_result
            after_sections = tuple(getattr(patch, "context_sections", ()) or ())
            if tool_failed:
                # 事后复核描述的是「这次调用产出了什么」：调用自己就没成功时它
                # 没有指称对象，拼进结果只会让模型把「参数错误」和「独立复核未
                # 通过」读成因果（K3 实测）。Hook 照样执行，只是不并进工具结果。
                if after_sections:
                    logger.info(
                        "工具 %s 自述失败（%s），跳过 after_tool Hook 的 %d 段上下文",
                        qualified_name,
                        _failure_reason(result),
                        len(after_sections),
                    )
            else:
                context_sections.extend(after_sections)
        if context_sections:
            content += "\n\nHook context:\n" + "\n\n".join(context_sections)
        return compact_tool_result(content)

    metadata = dict(contribution.spec.metadata)
    metadata["plugin_id"] = contribution.plugin_id
    qualified_name = str(metadata.get("qualified_id") or contribution.spec.name)
    metadata["sdk_qualified_name"] = qualified_name
    input_schema = dict(contribution.spec.input_schema)
    if input_schema.get("type") == "object":
        input_schema.setdefault("required", [])
    return StructuredTool(
        name=encode_tool_name(qualified_name),
        description=contribution.spec.description,
        args_schema=input_schema,
        coroutine=invoke,
        return_direct=(
            contribution.plugin_id == 'bianbu.bibit.intent'
            and qualified_name == SYSTEM_UPGRADE_TOOL
        ),
        metadata=metadata,
    )


def build_plugin_tools(
    contributions: Iterable[ToolContribution],
    hook_runner: object | None = None,
    hook_ids: tuple[str, ...] | None = None,
    hook_ids_resolver: Callable[[str], Awaitable[tuple[str, ...]]] | None = None,
) -> list[StructuredTool]:
    """Translate framework-neutral contributions at the LangChain boundary."""

    return [
        _adapt_tool(item, hook_runner, hook_ids, hook_ids_resolver)
        for item in contributions
    ]


def _dlp_masked_args(patch) -> dict | None:
    """从 ContextPatch 里取 DLP 脱敏后的参数，取不到返回 None。

    路径 metadata["bianbu.security"]["securityd"]["dlp_masked_args"] ——
    host.py::run_hooks 把各插件的 metadata 按 plugin_id 分层聚合。
    """
    meta = getattr(patch, "metadata", None)
    if not isinstance(meta, Mapping):
        return None
    scoped = meta.get("bianbu.security")
    inner = scoped.get("securityd") if isinstance(scoped, Mapping) else None
    if not isinstance(inner, Mapping):
        inner = meta.get("securityd") if isinstance(meta.get("securityd"), Mapping) else None
    if not isinstance(inner, Mapping):
        return None
    masked = inner.get("dlp_masked_args")
    return dict(masked) if isinstance(masked, Mapping) else None


async def _run_hooks(hook_runner, point, context, hook_ids):
    """执行某个 hook 点，**返回聚合后的 ContextPatch**。

    原实现签名是 `-> None` 且丢弃返回值，导致 BEFORE_TOOL / AFTER_TOOL 的
    ContextPatch 全部作废 —— 依赖它传递的 DLP 脱敏结果（dlp_masked_args）
    到不了中间件，脱敏形同虚设。返回出去由调用方决定怎么用。
    """
    runner = hook_runner.run_hooks
    if "hook_ids" in inspect.signature(runner).parameters:
        return await runner(point, context, hook_ids=hook_ids)
    return await runner(point, context)


__all__ = ["build_plugin_tools"]
