from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import PurePosixPath
import re
import time
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Mapping,
    Sequence,
)
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.constants import START

from bianbu_agent.engines.langgraph.approval import ApprovalMiddleware
from bianbu_agent.engines.langgraph.conversation import fold_messages
from bianbu_agent.runs.control import RunControl
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.sessions.events import SessionEvent
from bianbu_agent.sessions.inbox import SessionInboxMessage
from bianbu_agent.context.config import CompressionSettingsProvider
from bianbu_agent.context.middleware import (
    CompressionEventSink,
    ContextCompressionMiddleware,
)
from bianbu_agent.engines.reasoning_markup import ThinkStreamParser
from bianbu_agent.engines import decode_tool_name
from bianbu_agent.engines.message_ids import MessageIdCodec
from bianbu_agent.plugins import DEFAULT_ACCOUNT_ID
from bianbu_agent.engines.langgraph.prompts import (
    InitialPrompt,
    initial_human_messages,
    initial_prompt_text,
)


logger = logging.getLogger(__name__)
RuntimeEvent = RunEvent
ExecutionContextFactory = Callable[
    [str, str],
    AbstractContextManager[object],
]
#: A host-supplied reader of one Session's committed transcript events.
TranscriptProvider = Callable[[str], Awaitable[Sequence[SessionEvent]]]

_RM_WORD = re.compile(r"(?<![\w.-])(?:/[\w./-]+/)?rm(?![\w.-])")
_RM_ENV_SPLIT = re.compile(r"(?:^|\s)-Srm(?=$|[\s;&|()<>])")
_CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context_length_exceeded",
    "context length exceeded",
    "too many tokens",
    "reduce the length of the messages or completion",
)






def _text_events(
    text: str,
    *,
    parser: ThinkStreamParser | None,
    round_index: int,
    final: bool = False,
) -> list[RuntimeEvent]:
    parts = (
        [("token", text)]
        if parser is None and text
        else ([] if parser is None else parser.feed(text))
    )
    if final and parser is not None:
        parts.extend(parser.finish())
    return [
        RuntimeEvent(
            kind=kind,
            payload=(
                {"text": part, "round": round_index}
                if kind == "reasoning"
                else {"text": part}
            ),
        )
        for kind, part in parts
    ]


def is_context_overflow_error(error: BaseException) -> bool:
    """Recognize provider context rejection without matching unrelated 400s."""

    seen: set[int] = set()
    selected: BaseException | None = error
    while selected is not None and id(selected) not in seen:
        seen.add(id(selected))
        text = str(selected).casefold()
        if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
            return True
        selected = selected.__cause__ or selected.__context__
    return False


def _static_request_tokens(system_prompt: str, tools: Sequence[Any]) -> int:
    schemas: list[dict[str, object]] = []
    for tool in tools:
        raw_schema = getattr(tool, "args_schema", None)
        schema_method = getattr(raw_schema, "model_json_schema", None)
        if callable(schema_method):
            try:
                raw_schema = schema_method()
            except Exception:
                raw_schema = {}
        schemas.append(
            {
                "name": str(getattr(tool, "name", "")),
                "description": str(getattr(tool, "description", "")),
                "schema": (
                    dict(raw_schema) if isinstance(raw_schema, Mapping) else {}
                ),
            }
        )
    serialized = json.dumps(schemas, ensure_ascii=False, default=str)
    return 4 + (len(str(system_prompt)) + len(serialized) + 3) // 4


def _command_name(value: str) -> str:
    return PurePosixPath(value).name


def _terminal_command_requires_approval(arguments: Mapping[str, object]) -> bool:
    command = arguments.get("command")
    if not isinstance(command, str):
        return False
    # Approval is deliberately lexical and fail-safe. Shell is expressive enough
    # to hide executable names in stdin, aliases, heredocs, wrappers, and
    # expansions; trying to predict the final process tree here creates bypasses.
    # Collapse the two common shell spellings r''m / r\m as well.
    normalized = command.replace("\\\n", "")
    normalized = re.sub(r"\$(?:''|\"\")", "", normalized)
    normalized = re.sub(r"(['\"])\1", "", normalized)
    normalized = re.sub(r"\\(?=[A-Za-z0-9_])", "", normalized)
    return (
        _RM_WORD.search(normalized) is not None
        or _RM_ENV_SPLIT.search(normalized) is not None
    )


def _terminal_tool_call_requires_approval(request: Any) -> bool:
    tool_call = getattr(request, "tool_call", None)
    if not isinstance(tool_call, Mapping):
        return False
    arguments = tool_call.get("args")
    return (
        _terminal_command_requires_approval(arguments)
        if isinstance(arguments, Mapping)
        else False
    )


def _memory_tool_call_requires_approval(request: Any) -> bool:
    """Require HITL only for destructive AgentMemory operations."""

    tool_call = getattr(request, "tool_call", None)
    if not isinstance(tool_call, Mapping):
        return False
    arguments = tool_call.get("args")
    if not isinstance(arguments, Mapping):
        return False
    if str(arguments.get("action") or "").strip().casefold() == "remove":
        return True
    operations = arguments.get("operations")
    return isinstance(operations, Sequence) and any(
        isinstance(operation, Mapping)
        and str(operation.get("action") or "").strip().casefold() == "remove"
        for operation in operations
    )


def _dynamic_hitl_builder() -> Callable[..., Any] | None:
    """返回 security 包的兼容 HITL 构造器；包被裁剪时返回 None。

    只有这个实现会求值 ``InterruptOnConfig.when``。回退到官方
    ``HumanInTheLoopMiddleware`` 意味着 `terminal` / `memory` 退化成无条件弹窗，
    所以只有在 security 包整体不可用时才这么做。
    """
    try:
        from bianbu_agent.security.security_middleware import (
            build_security_hitl_middleware,
        )
    except ImportError:
        return None
    return build_security_hitl_middleware


def _no_execution_context(
    _session_id: str,
    _request_id: str,
) -> AbstractContextManager[object]:
    return nullcontext()


class AgentExecutorRouter:
    """Resolve and cache a hard-scoped executor for each Agent profile."""

    def __init__(
        self,
        default_executor: Any,
        *,
        executor_factory: Callable[
            [tuple[str, ...], tuple[str, ...]], Any
        ],
        available_tool_names: Collection[str],
    ) -> None:
        self._default_executor = default_executor
        self._executor_factory = executor_factory
        self._available_tool_names = frozenset(available_tool_names)
        self._executors: dict[tuple[tuple[str, ...], tuple[str, ...]], Any] = {}

    def _resolve(self, runtime_profile: object = None) -> Any:
        if not isinstance(runtime_profile, Mapping):
            return self._default_executor
        tools = tuple(
            sorted(
                {
                    str(name).strip()
                    for name in runtime_profile.get("tools", [])
                    if str(name).strip() in self._available_tool_names
                }
            )
        )
        skills = tuple(
            sorted(
                {
                    str(name).strip()
                    for name in runtime_profile.get("skills", [])
                    if str(name).strip()
                }
            )
        )
        key = (tools, skills)
        executor = self._executors.get(key)
        if executor is None:
            executor = self._executor_factory(tools, skills)
            self._executors[key] = executor
        return executor

    async def stream(
        self,
        prompt: InitialPrompt | None,
        *,
        session_id: str,
        product_session_id: str | None = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
        control: RunControl,
        runtime_profile: Mapping[str, object] | None = None,
    ):
        executor = self._resolve(runtime_profile)
        async for event in executor.stream(
            prompt,
            session_id=session_id,
            product_session_id=product_session_id,
            account_id=account_id,
            control=control,
        ):
            yield event


def _security_middleware_enabled() -> bool:
    """bianbu-security 中间件开关（默认开，BIANBU_SECURITY=0 关）。

    关掉后 interrupt_on 回退为原来的静态 requires_approval 语义，
    行为与未接入 securityd 时完全一致。
    """
    import os

    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return False
    try:
        import bianbu_agent.security.security_middleware  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def build_langgraph_agent(
    *,
    model: BaseChatModel | str,
    system_prompt: str,
    tools: list[Any] | None = None,
    hitl_tools: Collection[str] | None = None,
    context_settings: CompressionSettingsProvider | None = None,
) -> Any:
    """Build a LangGraph Agent from explicit host-provided policy."""

    explicit_tools = tools or []
    loaded_names = {
        str(name)
        for tool in explicit_tools
        if (name := getattr(tool, "name", None))
    }
    protected_names = {
        str(tool.name)
        for tool in explicit_tools
        if (
            isinstance(getattr(tool, "metadata", None), Mapping)
            and tool.metadata.get("requires_approval") is True
        )
    }
    tool_metadata = {
        str(tool.name): dict(tool.metadata)
        for tool in explicit_tools
        if (
            getattr(tool, "name", None)
            and isinstance(getattr(tool, "metadata", None), Mapping)
        )
    }
    protected_names.update(hitl_tools or ())
    security_enabled = _security_middleware_enabled()
    if security_enabled:
        # 所有工具都进入动态候选集，但只有 securityd 返回 CONFIRM（或工具被
        # 显式静态保护）时才会真正 interrupt。worker 的 LangChain 1.2.x
        # 不支持原生 `when`，判定由兼容中间件在 after_model 阶段执行。
        interrupt_on = {
            name: {
                "allowed_decisions": ["approve", "reject"],
            }
            for name in sorted(loaded_names | protected_names)
        }
    else:
        # securityd 不可用只应停掉「动态判权」，不能连工具自己声明的审批
        # 一起停：MCP 的 destructiveHint（映射成 requires_approval）与
        # hitl_tools 是**工具契约**，与 securityd 是否在场无关。整条一起关掉
        # 会把「需要确认」静默降级成「直接执行」——K3 实测 BIANBU_SECURITY=0
        # 时 appcli_run（destructiveHint=true）无审批执行、云端路径拿不到
        # 用户批准就改机器，正是本类缺陷。
        interrupt_on = {
            name: {
                "allowed_decisions": ["approve", "reject"],
            }
            for name in sorted(protected_names & loaded_names)
        }
    terminal_names: set[str] = set()
    memory_names: set[str] = set()
    for tool in explicit_tools:
        name = str(getattr(tool, "name", ""))
        metadata = getattr(tool, "metadata", None)
        qualified_name = (
            str(metadata.get("sdk_qualified_name") or name)
            if isinstance(metadata, Mapping)
            else name
        )
        base_name = _command_name(qualified_name)
        if base_name == "terminal":
            terminal_names.add(name)
        if base_name == "memory":
            # 记忆工具的破坏性只在参数里（action=remove），注解表达不了，
            # 所以无论 securityd 是否在场都由静态谓词兜住。
            memory_names.add(name)
        if (
            not security_enabled
            and name in terminal_names
            and name not in interrupt_on
        ):
            interrupt_on[name] = {
                "allowed_decisions": ["approve", "reject"],
                "when": _terminal_tool_call_requires_approval,
            }
        if base_name == "memory" and name not in interrupt_on:
            interrupt_on[name] = {
                "allowed_decisions": ["approve", "reject"],
                "when": _memory_tool_call_requires_approval,
            }
    from bianbu_agent.engines.langgraph.model_output import ModelOutputMiddleware

    middleware: list[Any] = [ModelOutputMiddleware()]
    compression_events = CompressionEventSink()
    compression_middleware: ContextCompressionMiddleware | None = None
    if isinstance(model, BaseChatModel):
        compression_middleware = ContextCompressionMiddleware(
            model,
            context_settings or CompressionSettingsProvider(),
            compression_events,
            static_tokens=_static_request_tokens(system_prompt, explicit_tools),
        )
        middleware.append(compression_middleware)
    if security_enabled:
        # 必须在 HumanInTheLoopMiddleware 之前（更外层）：
        # 先应用 DLP 脱敏改写，再让 HITL 判是否需审批，
        # 这样审批弹窗里展示的是脱敏后的参数。
        from bianbu_agent.security.security_middleware import (
            build_security_middleware,
        )

        middleware.append(build_security_middleware())
    if interrupt_on:
        # 两个分支都用兼容中间件：LangChain 1.2.x 的原生
        # HumanInTheLoopMiddleware 不读 `when`，直接拿它当 HITL 用会把
        # `terminal` / `memory` 变成**无条件**弹窗（BIANBU_SECURITY=0 的源部署
        # 上就是这样），谓词成了死代码。securityd 关掉时兼容中间件里的
        # securityd 判定自己短路，只留静态谓词。
        dynamic_hitl = _dynamic_hitl_builder()
        if dynamic_hitl is not None:
            middleware.append(
                dynamic_hitl(
                    interrupt_on,
                    protected_names=protected_names,
                    terminal_names=terminal_names,
                    terminal_when=_terminal_tool_call_requires_approval,
                    memory_names=memory_names,
                    memory_when=_memory_tool_call_requires_approval,
                    tool_metadata=tool_metadata,
                )
            )
        else:  # pragma: no cover - security 包被裁剪的安装
            fallback = ApprovalMiddleware(
                interrupt_on=interrupt_on,  # type: ignore[arg-type]
                description_prefix="工具执行需要人工批准",
            )
            fallback._metadata_by_name = tool_metadata
            middleware.append(fallback)
    from bianbu_agent.engines.langgraph.language import ResponseLanguageMiddleware

    # 最后注册 = 最内层：系统提示词里加的语言提醒落在最末尾，并且在
    # ModelOutputMiddleware 重建 system message 的重试路径上也还在。
    middleware.append(ResponseLanguageMiddleware())
    graph = create_agent(
        model=model,
        tools=explicit_tools,
        system_prompt=system_prompt,
        middleware=middleware,
    ).with_config({"recursion_limit": float("inf")})
    setattr(
        graph,
        "_bianbu_allowed_tool_names",
        frozenset(decode_tool_name(name) for name in loaded_names),
    )
    setattr(graph, "_bianbu_tool_metadata", tool_metadata)
    setattr(graph, "_bianbu_context_events", compression_events)
    setattr(graph, "_bianbu_context_middleware", compression_middleware)
    return graph


class LangGraphEngine:
    """Translate a compiled LangGraph message stream into canonical events."""

    def __init__(
        self,
        graph: Any,
        *,
        message_namespace: str = "bianbu-agent",
        execution_context_factory: ExecutionContextFactory | None = None,
        event_logger: logging.Logger | None = None,
        agent_id: str | None = None,
        hook_ids: Sequence[str] = (),
        provider_id: str | None = None,
        capture_callback: object | None = None,
        system_prompt: str | None = None,
        transcript_provider: TranscriptProvider | None = None,
    ) -> None:
        self._graph = graph
        # With no checkpointer the graph is stateless: every Run hands
        # LangGraph the whole conversation to continue, folded from the
        # Session transcript by the host that owns it.
        self._transcript_provider = transcript_provider
        self._message_ids = MessageIdCodec(message_namespace)
        self._assistant_message_ids = MessageIdCodec(
            f"{self._message_ids.prefix.removesuffix('-run:')}-assistant"
        )
        self._execution_context_factory = (
            execution_context_factory or _no_execution_context
        )
        self._logger = event_logger or logger
        self._agent_id = str(agent_id).strip() if agent_id is not None else ""
        self._hook_ids = tuple(str(item) for item in hook_ids)
        self._provider_id = (
            str(provider_id).strip() if provider_id is not None else ""
        )
        self._capture_callback = capture_callback
        self.system_prompt = None if system_prompt is None else str(system_prompt)
        raw_allowed_tool_names = getattr(
            graph,
            "_bianbu_allowed_tool_names",
            None,
        )
        self._allowed_tool_names = (
            None
            if raw_allowed_tool_names is None
            else frozenset(
                decode_tool_name(str(name))
                for name in raw_allowed_tool_names
                if str(name).strip()
            )
        )
        raw_tool_metadata = getattr(graph, "_bianbu_tool_metadata", {})
        self._tool_metadata = {
            str(name): dict(metadata)
            for name, metadata in raw_tool_metadata.items()
            if isinstance(metadata, Mapping)
        }
        self._compression_events = getattr(
            graph,
            "_bianbu_context_events",
            None,
        )
        self._thread_execution_locks: dict[str, asyncio.Lock] = {}

    async def _history(
        self,
        session_id: str,
        thread_id: str | None = None,
    ) -> Sequence[BaseMessage]:
        """The conversation so far, folded from the Session transcript.

        Clearing a Session's context rotates its thread without touching the
        log, so the fold keeps only the turns that belong to ``thread_id``.
        """

        if self._transcript_provider is None:
            return ()
        events = tuple(await self._transcript_provider(str(session_id)))
        selected = thread_id if thread_id else None
        if selected is not None and any(
            event.type == "user/message" for event in events
        ):
            allowed = {
                str(event.run_id)
                for event in events
                if event.type == "user/message"
                and str(event.data.get("thread_id") or session_id) == selected
            }
            events = tuple(
                event
                for event in events
                if event.run_id is None or str(event.run_id) in allowed
            )
        return fold_messages(events, input_id_for_run=self._message_ids.for_run)

    async def _conversation_messages(
        self,
        prompt: InitialPrompt,
        session_id: str,
        thread_id: str,
        control: RunControl,
    ) -> list[BaseMessage]:
        """The message list one Run hands to the stateless graph.

        The Session transcript already holds this Run's user turn -- the host
        writes it before the graph starts -- so the prompt is only appended
        when the fold does not already carry it.  Re-entering the same Run
        (model timeout recovery) therefore never duplicates the input.
        """

        message_id = self._message_ids.for_run(control.request_id)
        history = list(await self._history(session_id, thread_id))
        if any(
            str(getattr(message, "id", "") or "") == message_id
            for message in history
        ):
            return history
        return [
            *history,
            *initial_human_messages(prompt, message_id=message_id),
        ]

    async def claim_step_input(
        self,
        config: Mapping[str, Any],
        control: RunControl,
    ) -> tuple[SessionInboxMessage, ...]:
        """Claim the next-step inbox.

        A transcript-backed engine becomes durable once the Run stream
        publishes ``input_applied``: that event also writes the steering user
        message into the Session log, so the caller only has to re-fold it.  A
        host that still gives the graph its own checkpointer has no transcript
        to fold, so the claimed messages are written into its state instead.
        """

        batch = await control.capture_step_input()
        if not batch or self._transcript_provider is not None:
            return batch
        update_state = getattr(self._graph, "aupdate_state", None)
        if not callable(update_state):
            return batch
        await update_state(
            config,
            {
                "messages": [
                    HumanMessage(
                        content=item.content,
                        id=item.message_id,
                        additional_kwargs={
                            "session_input_mode": "steer",
                            "attachments": [
                                dict(value) for value in item.attachments
                            ],
                        },
                    )
                    for item in batch
                ]
            },
            as_node=START,
        )
        return batch

    async def stream(
        self,
        prompt: InitialPrompt | None,
        *,
        session_id: str,
        product_session_id: str | None = None,
        account_id: str = DEFAULT_ACCOUNT_ID,
        control: RunControl,
    ):
        selected_account_id = str(account_id).strip()
        if not selected_account_id:
            raise ValueError("account_id must not be empty")
        await control.open_step_input_admission()
        await control.refresh_step_input_notification()
        started_calls: set[str] = set()
        rejected_tool_calls: set[str] = set()
        pending_tool_calls: set[str] = set()
        pending_ai_chunk: AIMessageChunk | None = None
        final_chunk_message_key: tuple[Any, ...] | None = None
        final_chunk_usage_emitted = False
        think_parser = (
            ThinkStreamParser() if self._provider_id == "minimax" else None
        )
        tool_round = 0
        call_rounds: dict[str, int] = {}
        call_started_at: dict[str, float] = {}
        product_id = product_session_id or control.session_id or session_id
        self._logger.debug(
            "agent stream started session=%s thread=%s request=%s",
            product_id,
            session_id,
            control.request_id,
        )
        configurable = {
            "account_id": selected_account_id,
            "thread_id": session_id,
            "product_session_id": product_id,
            "session_id": product_id,
            "run_id": control.request_id,
            "request_id": control.request_id,
            "run_control": control,
        }
        if self._agent_id:
            configurable["agent_id"] = self._agent_id
        # 命令 Hook 与工具上下文要能看到用户本轮真正说了什么：`request` 在
        # PreToolUse 里是工具限定名（如 .../set_volume），拿它当用户请求会让
        # 「按关键词判断用户在说什么」的守卫永远命中工具名而放行。
        configurable["user_prompt"] = initial_prompt_text(prompt)
        configurable["hook_ids"] = self._hook_ids
        # LangGraph computes its stop step arithmetically; infinity disables the
        # finite step guard while ordinary cancellation/timeouts remain active.
        config: dict[str, Any] = {
            "configurable": configurable, "recursion_limit": float("inf")
        }
        if self._provider_id:
            config["metadata"] = {
                "provider_id": self._provider_id,
                "agent_id": self._agent_id,
                "session_id": product_id,
                "run_id": control.request_id,
            }
        if self._capture_callback is not None:
            config["callbacks"] = [self._capture_callback]
        agent_input: Any = None
        if prompt is not None:
            agent_input = {
                "messages": await self._conversation_messages(
                    prompt,
                    product_id,
                    session_id,
                    control,
                )
            }
        elif self._transcript_provider is not None:
            # A continuation (timeout recovery, steering)
            # starts from the folded transcript, not from a saved graph state.
            agent_input = {
                "messages": list(await self._history(product_id, session_id))
            }
        compression_token = self._activate_compression_events(
            control.request_id
        )
        graph_stream = _cancellable_graph_stream(
            self._astream_with_context_recovery(
                agent_input,
                trace_session_id=product_id,
                trace_request_id=control.request_id,
                config=config,
                stream_mode=["messages", "custom"],
            ),
            control,
        ).__aiter__()
        stream_closed = False
        try:
            async for streamed in graph_stream:
                for payload in self._drain_compression_events(control.request_id):
                    yield RuntimeEvent(
                        kind="context_compression",
                        payload=payload,
                    )
                if isinstance(streamed, RuntimeEvent):
                    pending_ai_chunk = None
                    final_chunk_message_key = None
                    final_chunk_usage_emitted = False
                    yield streamed
                    continue
                message, metadata = streamed
                if (
                    isinstance(metadata, Mapping)
                    and metadata.get("lc_source") == "context_compression"
                ):
                    if isinstance(message, AIMessageChunk) and (
                        _message_text(message.content) or _message_reasoning(message)
                    ):
                        control.model_progress()
                    continue
                if control.cancelled.is_set():
                    self._logger.info(
                        "agent stream cancelled session=%s request=%s rounds=%s",
                        product_id,
                        control.request_id,
                        tool_round,
                    )
                    return
                if isinstance(message, AIMessageChunk):
                    # Only actual model bytes refresh activity; empty stream
                    # keepalives must not hide a stalled provider.
                    if (_message_text(message.content) or _message_reasoning(message)
                            or any(chunk.get("args") or chunk.get("name")
                                   for chunk in message.tool_call_chunks)):
                        control.model_progress()
                    if pending_ai_chunk is None:
                        final_chunk_message_key = None
                        final_chunk_usage_emitted = False
                    model_round = tool_round + 1
                    reasoning = _message_reasoning(message)
                    if reasoning:
                        yield RuntimeEvent(
                            kind="reasoning",
                            payload={
                                "text": str(reasoning),
                                "round": model_round,
                            },
                        )
                    pending_ai_chunk = (
                        message
                        if pending_ai_chunk is None
                        else pending_ai_chunk + message
                    )
                    final_message: AIMessageChunk | None = None
                    starts: list[RuntimeEvent] = []
                    if message.chunk_position == "last":
                        final_message = pending_ai_chunk
                        starts = _tool_start_events(
                            final_message,
                            started_calls,
                            allowed_tool_names=self._allowed_tool_names,
                            round_index=model_round,
                        )
                        if starts:
                            tool_round += 1
                            _log_tool_batch(
                                starts,
                                session_id=product_id,
                                request_id=control.request_id,
                                round_index=tool_round,
                                call_started_at=call_started_at,
                                event_logger=self._logger,
                            )
                        pending_ai_chunk = None
                    text = _message_text(message.content)
                    for event in _text_events(
                        text,
                        parser=think_parser,
                        round_index=model_round,
                        final=message.chunk_position == "last",
                    ):
                        yield event
                    if final_message is not None:
                        usage = _usage_event(final_message)
                        final_chunk_message_key = _message_dedupe_key(
                            final_message
                        )
                        final_chunk_usage_emitted = usage is not None
                        if usage is not None:
                            yield usage
                        if starts:
                            yield _tool_message_end_event()
                        for event in starts:
                            call_id = str(event.payload["call_id"])
                            if event.kind == "tool_rejected":
                                rejected_tool_calls.add(call_id)
                            call_rounds[call_id] = tool_round
                            pending_tool_calls.add(call_id)
                            yield event
                elif isinstance(message, AIMessage):
                    usage = _usage_event(message)
                    message_key = _message_dedupe_key(message)
                    duplicate_message = (
                        message_key == final_chunk_message_key
                    )
                    duplicate_usage = (
                        duplicate_message and final_chunk_usage_emitted
                    )
                    final_chunk_message_key = None
                    final_chunk_usage_emitted = False
                    model_round = tool_round + 1
                    starts: list[RuntimeEvent] = []
                    if not duplicate_message:
                        reasoning = _message_reasoning(message)
                        if reasoning:
                            yield RuntimeEvent(
                                kind="reasoning",
                                payload={
                                    "text": str(reasoning),
                                    "round": model_round,
                                },
                            )
                        starts = _tool_start_events(
                            message,
                            started_calls,
                            allowed_tool_names=self._allowed_tool_names,
                            round_index=model_round,
                        )
                        if starts:
                            tool_round += 1
                            _log_tool_batch(
                                starts,
                                session_id=product_id,
                                request_id=control.request_id,
                                round_index=tool_round,
                                call_started_at=call_started_at,
                                event_logger=self._logger,
                            )
                        text = _message_text(message.content)
                        direct_parser = (
                            ThinkStreamParser()
                            if self._provider_id == "minimax"
                            else None
                        )
                        for event in _text_events(
                            text,
                            parser=direct_parser,
                            round_index=model_round,
                            final=True,
                        ):
                            yield event
                    if usage is not None and not duplicate_usage:
                        yield usage
                    if starts:
                        yield _tool_message_end_event()
                    for event in starts:
                        call_id = str(event.payload["call_id"])
                        if event.kind == "tool_rejected":
                            rejected_tool_calls.add(call_id)
                        call_rounds[call_id] = tool_round
                        pending_tool_calls.add(call_id)
                        yield event
                elif isinstance(message, ToolMessage):
                    final_chunk_message_key = None
                    final_chunk_usage_emitted = False
                    for event in _text_events(
                        "",
                        parser=think_parser,
                        round_index=tool_round + 1,
                        final=True,
                    ):
                        yield event
                    if pending_ai_chunk is not None:
                        starts = _tool_start_events(
                            pending_ai_chunk,
                            started_calls,
                            allowed_tool_names=self._allowed_tool_names,
                            round_index=tool_round + 1,
                        )
                        if starts:
                            tool_round += 1
                            _log_tool_batch(
                                starts,
                                session_id=product_id,
                                request_id=control.request_id,
                                round_index=tool_round,
                                call_started_at=call_started_at,
                                event_logger=self._logger,
                            )
                        if starts:
                            yield _tool_message_end_event()
                        for event in starts:
                            call_id = str(event.payload["call_id"])
                            if event.kind == "tool_rejected":
                                rejected_tool_calls.add(call_id)
                            call_rounds[call_id] = tool_round
                            pending_tool_calls.add(call_id)
                            yield event
                        pending_ai_chunk = None
                    call_id = str(message.tool_call_id or "")
                    if call_id and call_id not in started_calls:
                        started_calls.add(call_id)
                        pending_tool_calls.add(call_id)
                        event = _tool_call_event(
                            call_id=call_id,
                            name=decode_tool_name(str(message.name or "")),
                            arguments={},
                            allowed_tool_names=self._allowed_tool_names,
                        )
                        if event.kind == "tool_rejected":
                            rejected_tool_calls.add(call_id)
                        else:
                            call_started_at[call_id] = time.monotonic()
                        yield event
                    result = _message_text(message.content)
                    started_at = call_started_at.pop(call_id, None)
                    duration_ms = (
                        None
                        if started_at is None
                        else round((time.monotonic() - started_at) * 1000)
                    )
                    if call_id in rejected_tool_calls:
                        self._logger.warning(
                            "model tool call rejected session=%s request=%s "
                            "round=%s name=%s call_id=%s",
                            product_id,
                            control.request_id,
                            call_rounds.get(call_id),
                            message.name,
                            call_id,
                        )
                    else:
                        self._logger.debug(
                            "tool completed session=%s request=%s round=%s "
                            "name=%s call_id=%s status=%s duration_ms=%s "
                            "result_chars=%s",
                            product_id,
                            control.request_id,
                            call_rounds.get(call_id),
                            message.name,
                            call_id,
                            message.status or "success",
                            duration_ms,
                            len(result),
                        )
                        yield RuntimeEvent(
                            kind="tool_end",
                            payload={
                                "call_id": message.tool_call_id,
                                "name": decode_tool_name(str(message.name or "")),
                                "result": result,
                                "status": message.status,
                                "round": call_rounds.get(call_id),
                            },
                        )
                    pending_tool_calls.discard(call_id)
                    from bianbu_agent.engines.langgraph.plugin_tools import (
                        upgrade_result_message, upgrade_submitted,
                    )

                    upgrade_message = upgrade_result_message(
                        decode_tool_name(str(message.name or '')), result,
                    )
                    if upgrade_message and call_id not in rejected_tool_calls:
                        for event in _text_events(
                            upgrade_message, parser=think_parser,
                            round_index=tool_round, final=True,
                        ):
                            yield event
                    if (
                        not pending_tool_calls
                        and call_id not in rejected_tool_calls
                        and message.status != 'error'
                        and upgrade_submitted(
                            decode_tool_name(str(message.name or '')), result,
                        )
                    ):
                        await _close_async_iterator(graph_stream)
                        stream_closed = True
                        self._logger.info(
                            'system upgrade handed off session=%s request=%s',
                            product_id, control.request_id,
                        )
                        return
                    if (
                        not pending_tool_calls
                        and control.should_wake_for_step_input()
                    ):
                        await _close_async_iterator(graph_stream)
                        stream_closed = True
                        batch = await self.claim_step_input(config, control)
                        for item in batch:
                            yield RuntimeEvent(
                                kind="input_applied",
                                payload={
                                    "message_id": item.message_id,
                                },
                            )
                        if not batch:
                            continue
                        async for event in self.stream(
                            None,
                            session_id=session_id,
                            product_session_id=product_id,
                            control=control,
                        ):
                            yield event
                        return
            for event in _text_events(
                "",
                parser=think_parser,
                round_index=tool_round + 1,
                final=True,
            ):
                yield event
        except Exception:
            for payload in self._drain_compression_events(control.request_id):
                yield RuntimeEvent(
                    kind="context_compression",
                    payload=payload,
                )
            raise
        finally:
            if not stream_closed:
                await _close_async_iterator(graph_stream)
            self._deactivate_compression_events(compression_token)
        for payload in self._drain_compression_events(control.request_id):
            yield RuntimeEvent(kind="context_compression", payload=payload)
        if control.cancelled.is_set():
            self._logger.info(
                "agent stream cancelled session=%s request=%s rounds=%s",
                product_id,
                control.request_id,
                tool_round,
            )
        if not control.cancelled.is_set():
            # An approval no longer ends the Run: the graph parks in its approval
            # middleware and the decision arrives on this same stream, so a
            # finished stream is a finished Run.
            self._logger.debug(
                "agent stream finished session=%s request=%s rounds=%s",
                product_id,
                control.request_id,
                tool_round,
            )
            await control.close_step_input_admission()
        if control.should_wake_for_step_input():
            batch = await self.claim_step_input(config, control)
            for item in batch:
                yield RuntimeEvent(
                    kind="input_applied",
                    payload={
                        "message_id": item.message_id,
                    },
                )
            if not batch:
                return
            async for event in self.stream(
                None,
                session_id=session_id,
                product_session_id=product_id,
                control=control,
            ):
                yield event
            return

    def _drain_compression_events(
        self,
        request_id: str,
    ) -> tuple[dict[str, object], ...]:
        drain = getattr(self._compression_events, "drain", None)
        if not callable(drain):
            return ()
        return tuple(drain(request_id))

    def _activate_compression_events(self, request_id: str) -> object | None:
        activate = getattr(self._compression_events, "activate", None)
        return activate(request_id) if callable(activate) else None

    def _deactivate_compression_events(self, token: object | None) -> None:
        if token is None:
            return
        deactivate = getattr(self._compression_events, "deactivate", None)
        if callable(deactivate):
            deactivate(token)

    async def _astream_with_context_recovery(
        self,
        agent_input: Any,
        *,
        trace_session_id: str,
        trace_request_id: str,
        config: dict[str, Any],
        **kwargs: Any,
    ):
        lock = self._thread_execution_locks.setdefault(
            str(config["configurable"]["thread_id"]),
            asyncio.Lock(),
        )
        async with lock:
            selected_input = agent_input
            recovered = False
            while True:
                iterator = _astream_with_trace_context(
                    self._graph,
                    selected_input,
                    trace_session_id=trace_session_id,
                    trace_request_id=trace_request_id,
                    execution_context_factory=self._execution_context_factory,
                    config=config,
                    **kwargs,
                ).__aiter__()
                try:
                    async for item in iterator:
                        yield item
                    return
                except Exception as exc:
                    compacted = (
                        None
                        if recovered or not is_context_overflow_error(exc)
                        else await self._compact_input_for_overflow(selected_input)
                    )
                    if compacted is None:
                        raise
                    recovered = True
                    # Retry the same stateless graph with the rewritten input.
                    # There is no checkpoint to repair, and the folded input is
                    # the only carrier of the conversation.
                    selected_input = compacted
                finally:
                    await _close_async_iterator(iterator)

    async def _compact_input_for_overflow(
        self,
        agent_input: Any,
    ) -> Any | None:
        """Rewrite one graph input after a provider rejected it as too long."""

        if not isinstance(agent_input, Mapping):
            return None
        messages = agent_input.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, BaseMessage) for message in messages
        ):
            return None
        middleware = getattr(
            self._graph,
            "_bianbu_context_middleware",
            None,
        )
        compact = getattr(middleware, "compact_messages_emergency", None)
        if not callable(compact):
            return None
        compacted = compact(list(messages))
        if compacted is None:
            return None
        return {"messages": list(compacted)}

    async def can_resume_model(self, session_id: str, request_id: str) -> bool:
        """Whether a fresh model step can resume this Run.

        The graph keeps no state between attempts, so resuming always means
        re-folding the Session transcript and asking the model again.  That is
        only possible when the host gave the engine a transcript to fold.
        """

        return self._transcript_provider is not None

    async def has_thread_input(
        self,
        thread_id: str,
        request_id: str,
    ) -> bool:
        """Return whether this Run's stable user input is in the transcript."""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        expected_id = self._message_ids.for_run(request_id)
        return any(
            _is_run_input_message(message, expected_id)
            for message in await self._history(thread_id)
        )

    async def recover_completed_events(
        self,
        thread_id: str,
        request_id: str,
    ) -> list[RuntimeEvent] | None:
        """Recover a completed Run's output from the folded transcript."""

        if not isinstance(thread_id, str) or not thread_id.strip():
            raise ValueError("thread_id must not be empty")
        messages = list(await self._history(thread_id))
        if not messages:
            return None

        expected_id = self._message_ids.for_run(request_id)
        start = next(
            (
                index
                for index, message in enumerate(messages)
                if _is_run_input_message(message, expected_id)
            ),
            None,
        )
        if start is None:
            return None
        end = len(messages)
        for index in range(start + 1, len(messages)):
            if _is_any_run_input_message(
                messages[index],
                prefix=self._message_ids.prefix,
            ):
                end = index
                break
        output = list(messages[start + 1 : end])
        if not output or not isinstance(output[-1], AIMessage):
            return None
        if output[-1].tool_calls:
            return None

        called: set[str] = set()
        completed: set[str] = set()
        rejected: set[str] = set()
        call_rounds: dict[str, int] = {}
        tool_round = 0
        events: list[RuntimeEvent] = []
        for message in output:
            if isinstance(message, AIMessage):
                model_round = tool_round + 1
                reasoning = _message_reasoning(message)
                if reasoning:
                    events.append(
                        RuntimeEvent(
                            kind="reasoning",
                            payload={
                                "text": str(reasoning),
                                "round": model_round,
                            },
                        )
                    )
                starts = _tool_start_events(
                    message,
                    called,
                    allowed_tool_names=self._allowed_tool_names,
                    round_index=model_round,
                )
                if starts:
                    tool_round += 1
                text = _message_text(message.content)
                parser = (
                    ThinkStreamParser()
                    if self._provider_id == "minimax"
                    else None
                )
                events.extend(
                    _text_events(
                        text,
                        parser=parser,
                        round_index=model_round,
                        final=True,
                    )
                )
                usage = _usage_event(message)
                if usage is not None:
                    events.append(usage)
                if starts:
                    events.append(_tool_message_end_event())
                for event in starts:
                    call_id = str(event.payload["call_id"])
                    call_rounds[call_id] = tool_round
                    if event.kind == "tool_rejected":
                        rejected.add(call_id)
                events.extend(starts)
            elif isinstance(message, ToolMessage):
                call_id = str(message.tool_call_id or "")
                if not call_id or call_id not in called:
                    return None
                completed.add(call_id)
                if call_id in rejected:
                    continue
                result = _message_text(message.content)
                events.append(
                    RuntimeEvent(
                        kind="tool_end",
                        payload={
                            "call_id": message.tool_call_id,
                            "name": decode_tool_name(str(message.name or "")),
                            "result": result,
                            "status": message.status,
                            "round": call_rounds.get(call_id),
                        },
                    )
                )
        if called != completed:
            return None
        return events


class _PendingApproval:
    """The Run stream must surface one approval request before reading on."""

    __slots__ = ("payload",)

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload


async def _cancellable_graph_stream(
    iterator: AsyncIterator[Any],
    control: RunControl,
) -> AsyncIterator[Any]:
    # Both waiters are long-lived, not one per streamed chunk.  A graph parked
    # in its approval middleware produces no further items, so its pending
    # ``anext`` must survive the round that surfaces the approval -- cancelling
    # it there would cancel the very Run that is waiting for the decision.
    approval = asyncio.create_task(control.wait_approval_request())
    next_item = asyncio.create_task(anext(iterator))
    try:
        while True:
            try:
                item, was_cancelled = await _next_graph_item_or_cancel(
                    next_item, control, approval
                )
            except StopAsyncIteration:
                return
            if was_cancelled:
                return
            if isinstance(item, _PendingApproval):
                yield RuntimeEvent(kind="approval_required", payload=item.payload)
                approval = asyncio.create_task(control.wait_approval_request())
                continue
            yield item
            next_item = asyncio.create_task(anext(iterator))
    finally:
        for task in (approval, next_item):
            task.cancel()
        await asyncio.gather(approval, next_item, return_exceptions=True)
        await _close_async_iterator(iterator)


async def _next_graph_item_or_cancel(
    next_item: asyncio.Task[Any],
    control: RunControl,
    approval: asyncio.Task[dict[str, object]],
) -> tuple[Any | None, bool]:
    if control.cancelled.is_set():
        return None, True
    cancelled = asyncio.create_task(control.cancelled.wait())
    try:
        await asyncio.wait(
            {next_item, cancelled, approval},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if control.cancelled.is_set():
            return None, True
        if next_item.done() and not next_item.cancelled():
            # A graph item won the race. The approval waiter stays pending, so
            # the next round surfaces it instead of dropping either one.
            return await next_item, False
        if approval.done() and not approval.cancelled():
            return _PendingApproval(approval.result()), False
        return await next_item, False
    finally:
        if not cancelled.done():
            cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)


async def _close_async_iterator(iterator: AsyncIterator[Any]) -> None:
    close = getattr(iterator, "aclose", None)
    if not callable(close):
        return
    result = close()
    if inspect.isawaitable(result):
        await result


async def _astream_with_trace_context(
    graph: Any,
    agent_input: Any,
    *,
    trace_session_id: str,
    trace_request_id: str,
    execution_context_factory: ExecutionContextFactory,
    **kwargs: Any,
):
    with execution_context_factory(trace_session_id, trace_request_id):
        iterator = graph.astream(agent_input, **kwargs).__aiter__()
        try:
            async for item in iterator:
                if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str):
                    mode, payload = item
                    if mode == "custom":
                        if isinstance(payload, Mapping) and payload.get("kind") == "progress":
                            yield RuntimeEvent("progress", dict(payload.get("payload") or {}))
                        continue
                    if mode == "messages":
                        yield payload
                        continue
                yield item
        finally:
            await _close_async_iterator(iterator)


def _log_tool_batch(
    starts: Sequence[RuntimeEvent],
    *,
    session_id: str,
    request_id: str,
    round_index: int,
    call_started_at: dict[str, float],
    event_logger: logging.Logger | None = None,
) -> None:
    calls = []
    started_at = time.monotonic()
    for event in starts:
        call_id = str(event.payload.get("call_id") or "")
        if call_id and event.kind == "tool_start":
            call_started_at[call_id] = started_at
        calls.append(
            {
                "name": str(event.payload.get("name") or ""),
                "call_id": call_id,
                "status": (
                    "rejected" if event.kind == "tool_rejected" else "started"
                ),
            }
        )
    (event_logger or logger).debug(
        "tool batch emitted session=%s request=%s round=%s count=%s "
        "model_parallel_batch=%s calls=%s",
        session_id,
        request_id,
        round_index,
        len(calls),
        len(calls) > 1,
        json.dumps(calls, ensure_ascii=False, separators=(",", ":")),
    )


def _tool_start_events(
    message: AIMessage | AIMessageChunk,
    started_calls: set[str],
    *,
    allowed_tool_names: Collection[str] | None = None,
    round_index: int | None = None,
) -> list[RuntimeEvent]:
    events: list[RuntimeEvent] = []
    for tool_call in message.tool_calls:
        call_id = str(tool_call.get("id") or "")
        name = decode_tool_name(str(tool_call.get("name") or ""))
        if not call_id or not name or call_id in started_calls:
            continue
        started_calls.add(call_id)
        events.append(
            _tool_call_event(
                call_id=call_id,
                name=name,
                arguments=tool_call.get("args") or {},
                allowed_tool_names=allowed_tool_names,
                round_index=round_index,
            )
        )
    return events


def _tool_call_event(
    *,
    call_id: str,
    name: str,
    arguments: Mapping[str, object],
    allowed_tool_names: Collection[str] | None,
    round_index: int | None = None,
) -> RuntimeEvent:
    payload: dict[str, object] = {
        "call_id": call_id,
        "name": name,
        "arguments": dict(arguments),
    }
    if round_index is not None:
        payload["round"] = round_index
    if allowed_tool_names is not None and name not in allowed_tool_names:
        payload.update(
            {
                "reason": "tool_not_available",
                "allowed_tools": sorted(set(allowed_tool_names)),
            }
        )
        return RuntimeEvent(kind="tool_rejected", payload=payload)
    return RuntimeEvent(kind="tool_start", payload=payload)


def _tool_message_end_event() -> RuntimeEvent:
    """Close one assistant turn before its tool calls are displayed."""

    return RuntimeEvent(
        kind="message_end",
        payload={"has_tool_calls": True},
    )


def _usage_event(
    message: AIMessage | AIMessageChunk,
) -> RuntimeEvent | None:
    try:
        metadata = message.usage_metadata
    except Exception:
        return None
    if metadata is None:
        return None

    prompt_tokens = _usage_token_count(
        metadata,
        ("input_tokens", "prompt_tokens"),
        minimum=1,
    )
    completion_tokens = _usage_token_count(
        metadata,
        ("output_tokens", "completion_tokens"),
        minimum=0,
    )
    if prompt_tokens is None:
        return None
    if completion_tokens is None:
        completion_tokens = 0
    return RuntimeEvent(
        kind="usage",
        payload={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    )


def _usage_token_count(
    metadata: Any,
    names: tuple[str, ...],
    *,
    minimum: int,
) -> int | None:
    missing = object()
    for name in names:
        value: Any = missing
        try:
            getter = getattr(metadata, "get", None)
        except Exception:
            getter = None
        if callable(getter):
            try:
                value = getter(name, missing)
            except TypeError:
                try:
                    value = getter(name)
                except Exception:
                    value = missing
            except Exception:
                value = missing
        if value is missing:
            try:
                value = metadata[name]
            except Exception:
                try:
                    value = getattr(metadata, name)
                except Exception:
                    value = missing
        if value is missing or isinstance(value, bool):
            continue
        try:
            count = int(value)
        except Exception:
            continue
        if count >= minimum:
            return count
    return None


def _message_dedupe_key(
    message: AIMessage | AIMessageChunk,
) -> tuple[Any, ...]:
    tool_calls = tuple(
        (
            str(tool_call.get("name") or ""),
            _stable_value(tool_call.get("args") or {}),
        )
        for tool_call in message.tool_calls
    )
    return ("content", _message_text(message.content), tool_calls)


def _stable_value(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:
        try:
            return repr(value)
        except Exception:
            return f"<{type(value).__name__}>"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "output_text"}:
            parts.append(str(block.get("text", "")))
    return "".join(parts)


def _message_reasoning(message: AIMessage | AIMessageChunk) -> str:
    """Read provider reasoning from LangChain's supported metadata locations."""
    for metadata in (
        getattr(message, "additional_kwargs", None),
        getattr(message, "response_metadata", None),
    ):
        if not isinstance(metadata, Mapping):
            continue
        value = metadata.get("reasoning_content")
        if value:
            return str(value)
    content = getattr(message, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") not in {"reasoning", "thinking"}:
                continue
            value = (
                block.get("reasoning_content")
                or block.get("reasoning")
                or block.get("text")
            )
            if value:
                parts.append(str(value))
        if parts:
            return "".join(parts)
    return ""


def _is_run_input_message(message: object, expected_id: str) -> bool:
    if isinstance(message, Mapping):
        role = str(message.get("role") or message.get("type") or "")
        message_id = str(message.get("id") or "")
    else:
        role = str(
            getattr(message, "type", None)
            or getattr(message, "role", None)
            or ""
        )
        message_id = str(getattr(message, "id", None) or "")
    return role in {"user", "human"} and message_id == expected_id


def _is_any_run_input_message(message: object, *, prefix: str) -> bool:
    if isinstance(message, Mapping):
        role = str(message.get("role") or message.get("type") or "")
        message_id = str(message.get("id") or "")
    else:
        role = str(
            getattr(message, "type", None)
            or getattr(message, "role", None)
            or ""
        )
        message_id = str(getattr(message, "id", None) or "")
    return (
        role in {"user", "human"}
        and message_id.startswith(prefix)
    )
