from __future__ import annotations

import os
import platform
from collections.abc import Mapping, Sequence

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_openai import ChatOpenAI

from bianbu_agent.agents import AgentDeclaration, AgentSpec
from bianbu_agent.context import CompressionSettingsProvider
from bianbu_agent.mcp import resolve_mcp_selectors
from bianbu_agent.engines.langgraph import (
    LangGraphEngine,
    build_langgraph_agent,
    build_plugin_tools,
)
from bianbu_agent.engines.tool_pairing import reconcile_tool_pairs
from bianbu_agent.models import ChatModel, OpenAICompatibleModel


def _invalid_tool_call_ids(messages: Sequence[object]) -> set[str]:
    """Collect the ids of the tool calls LangChain refused to execute."""

    ids: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in getattr(message, "invalid_tool_calls", None) or ():
            if not isinstance(call, Mapping):
                continue
            call_id = str(call.get("id") or "")
            if call_id:
                ids.add(call_id)
    return ids


class _ReasoningChatOpenAI(ChatOpenAI):
    """Preserve provider reasoning and enforce the shared output budget."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        from bianbu_agent.context.config import bounded_output_settings
        payload = bounded_output_settings(
            self, payload,
            default_key="max_output_tokens" if "input" in payload else "max_completion_tokens",
        )
        messages = self._convert_input(input_).to_messages()
        # DeepSeek V4 requires prior reasoning on every tool-enabled request.
        # Keep this provider extension out of other OpenAI-compatible APIs.
        if self.model_name.lower().startswith("deepseek-v4") and payload.get("tools"):
            for message, wire in zip(messages, payload["messages"], strict=True):
                if isinstance(message, AIMessage):
                    reasoning = message.additional_kwargs.get("reasoning_content")
                    if isinstance(reasoning, str):
                        wire["reasoning_content"] = reasoning
        # Providers reject a Run whose history declares tool calls without the
        # matching results, so repair the pairing before it leaves the process.
        if isinstance(payload.get("messages"), list):
            payload["messages"] = reconcile_tool_pairs(
                payload["messages"],
                invalid_call_ids=_invalid_tool_call_ids(messages),
            )
        return payload

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        body = response if isinstance(response, dict) else response.model_dump()
        for generation, choice in zip(result.generations, body.get("choices", [])):
            reasoning = (choice.get("message") or {}).get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self, chunk, default_chunk_class, base_generation_info
    ):
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is None:
            return None
        choices = (
            chunk.get("choices", [])
            or chunk.get("chunk", {}).get("choices", [])
        )
        if not choices or not isinstance(generation.message, AIMessageChunk):
            return generation
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if reasoning:
            generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation


def build_default_engine(
    agent: AgentSpec,
    generation,
    model: ChatModel,
    *,
    transcript_provider: object | None = None,
    capture_callback: object | None = None,
    context_settings: CompressionSettingsProvider | None = None,
) -> LangGraphEngine:
    _publish_sandbox_config(agent)
    tools = build_plugin_tools(
        (generation.tools[name] for name in agent.tools if name in generation.tools),
        hook_runner=generation,
        hook_ids=agent.hooks,
    )
    # Use the resolved execution model, not the saved selector or credentials.
    model_name = getattr(model, "model_name", None) or getattr(model, "model", None)
    provider_id = getattr(model, "provider_id", None)
    profile = [
        "## Agent Profile", "", f"Name: {agent.name}",
        f"Model: {str(model_name).strip() if model_name else 'unknown'}",
        f"Model provider ID: {str(provider_id).strip() if provider_id else 'unknown'}",
    ]
    sections = ["\n".join(profile)]
    if agent.soul:
        sections.append(f"## Agent Soul\n\n{agent.soul}")
    sections.append(_power_dependencies_section(agent, generation))
    exposed_tool_names = tuple(
        str(name).strip()
        for tool in tools
        if (name := getattr(tool, "name", None)) and str(name).strip()
    )
    available_tool_names = ", ".join(exposed_tool_names) or "none"
    sections.append(
        "## Tool Policy\n\n"
        f"Available tool names: {available_tool_names}. "
        "Never call or invent a tool that is not in this list."
    )
    # Inject architecture-specific syscall guidance only inside a real Worker
    # process (or when explicitly requested).  Keeping it out of lightweight
    # SDK/test prompts avoids imposing a large policy catalogue on every model
    # invocation while preserving accurate explanations in production.
    if (os.environ.get("BIANBU_RUNTIME_ROLE") == "agent-worker"
            or os.environ.get("BIANBU_SYSCALL_CONTEXT") == "1"):
        from bianbu_agent.security.syscall_catalog import syscall_context
        sections.append("## Runtime Security Context\n\n" + syscall_context())
    # D-10：Skill 描述过 DLP。
    #
    # 这里只把短 description 注入 system prompt；完整 instructions 是
    # Skill 的实现参考，不应在每轮请求中全部拼入上下文，避免 prompt 膨胀。
    #
    # 处置仍沿用脱敏逻辑，避免描述中的敏感信息进入模型上下文。
    # 不弹确认：prompt 组装不是用户发起的动作，此刻没有可问的人。
    from bianbu_agent.security.security_skill_dlp import scan_skill_sections

    # 按 skill 名（而非 display_name）作键：display_name 可能重复，
    # 撞了会静默丢掉一个 Skill
    skill_texts = {
        name: generation.skills[name].spec.description
        for name in agent.skills
        if name in generation.skills
    }
    # Runtime 的逻辑 AgentSpec.id 与
    # securityd 的稳定 Agent 身份不是同一命名空间。启动阶段绑定的是
    # BIANBU_AGENT_ID；Skill DLP 走正式 UDS 时必须使用同一个身份，
    # 否则 securityd 会正确地以 caller binding 不匹配拒绝扫描，进而把
    # 所有 Skill fail-closed 丢弃。
    security_agent_id = (
        os.environ.get("COM_BIANBU_AGENT_ID")
        or os.environ.get("BIANBU_WORKER_AGENT_ID")
        or os.environ.get("BIANBU_AGENT_ID")
        or agent.id
    ).strip()
    scanned = scan_skill_sections(security_agent_id, skill_texts)
    # A Skill the user attached to this message is not an ambient Agent Skill:
    # it is the procedure the user asked for.  Rendering it as just another
    # ``## Skill:`` entry made the model treat the attachment as background
    # context and improvise instead, so state the request and give the
    # unambiguous handle ``skill_view`` resolves.
    requested = tuple(
        name
        for name in getattr(agent, "requested_skills", ())
        if name in scanned
    )
    requested_set = set(requested)
    for name in agent.skills:
        if name in scanned and name not in requested_set:
            sections.append(
                f"## Skill: {generation.skills[name].spec.display_name}\n\n"
                f"{scanned[name]}")
    if requested:
        requested_lines = [
            "## User-Requested Skills",
            "",
            "The user explicitly attached the Skill(s) below to this message. "
            "They are the intended procedure for this request: load each one "
            "with skill_view using the handle in parentheses and follow it, "
            "preferring it over an improvised approach. If one clearly does "
            "not apply, say why instead of silently ignoring it.",
            "",
        ]
        for name in requested:
            display_name = generation.skills[name].spec.display_name
            requested_lines.append(
                f"### {display_name} ({name})\n\n{scanned[name]}"
            )
        sections.append("\n".join(requested_lines))
    model_options: dict[str, object] = {}
    if callable(getattr(capture_callback, "http_hooks", None)):
        model_options["capture_callback"] = capture_callback
    platform_context = [
        "## Platform Context",
        "",
        "Bibit / Bianbu Agent Runtime",
        f"Agent ID: {agent.id}",
        f"Architecture: {platform.machine().strip() or 'Unknown'}",
    ]
    # The existing optional Hook supplies OS-specific command guidance. Do not
    # repeat that information when it is selected and available in this run.
    if not (
        "platform.context" in agent.hooks
        and "platform.context" in getattr(generation, "hooks", {})
    ):
        platform_context.append(f"Operating system: {platform.system().strip() or 'Unknown'}")
    platform_context.append(
        "Identity does not grant tools, Skills, or permissions."
    )
    platform_context.extend([
        "",
        "Software request boundaries:",
        "A download request (下载) authorizes fetching "
        "the requested artifact only, not installing, upgrading, configuring, "
        "repairing, removing, or running it. A broken or incomplete existing "
        "installation does not expand that authorization. Report it separately "
        "and ask before any repair; do not silently reinterpret download as install.",
    ])
    # The remaining boundaries depend on where this process really runs. A host
    # Worker that is told the sandbox rules refuses privileged work it could do
    # (the K3 session read "do not fall back to terminal sudo" and reported a
    # reboot/install as impossible), while a sandbox Worker that is promised
    # sudo semantics has no way to setuid at all. Unknown means no Worker
    # published the mode, so keep the conservative sandbox wording.
    platform_context.extend(
        _HOST_SOFTWARE_BOUNDARIES
        if _effective_execution_mode() == "host"
        else _SANDBOX_SOFTWARE_BOUNDARIES
    )
    sections.append("\n".join(platform_context))
    # Runtime 自己拥有语言规则：Expert 的 Soul 可能没有这条，或者只有一句
    # 软约束（K3 上中文提问被答成英文就是这种），所以规则不交给 Soul 承载。
    from bianbu_agent.engines.langgraph.language import RESPONSE_LANGUAGE_RULES

    sections.append(RESPONSE_LANGUAGE_RULES)
    system_prompt = "\n\n".join(sections)
    graph = build_langgraph_agent(
        model=to_langchain_model(model, **model_options),
        tools=tools,
        system_prompt=system_prompt,
        context_settings=context_settings,
    )
    return LangGraphEngine(
        graph,
        message_namespace="bianbu-agent-sdk",
        agent_id=agent.id,
        hook_ids=agent.hooks,
        provider_id=str(getattr(model, "provider_id", "") or ""),
        capture_callback=capture_callback,
        system_prompt=system_prompt,
        transcript_provider=transcript_provider,
    )


def _power_dependencies_section(agent: AgentSpec, generation: object | None = None) -> str:
    """State the Powers this Agent declared, and which of them are missing.

    ``materialize`` resolves a manifest's selectors against the installed
    Generation and throws the gap away.  An expert installed from AgentHub
    usually declares more Powers than a fresh device carries, so the difference
    between the declaration and the resolved sets is the worklist the model has
    to be able to see before it can answer or provision anything.
    """

    declaration = agent.declared
    installed_plugins = _installed_plugin_ids(generation)
    entries = []
    missing_powers: list[str] = []
    # Tools report only their gap: the exposed names are already the authoritative
    # list in Tool Policy, and repeating them doubles that section's cost.
    for label, declared, present, show_present in (
        (
            "Tools",
            declaration.tools if declaration is not None else agent.tools,
            agent.tools,
            False,
        ),
        (
            "Skills",
            declaration.skills if declaration is not None else agent.skills,
            agent.skills,
            True,
        ),
        (
            "MCP servers",
            # The declaration may use the APM name (``nws``) of a server this
            # device installed as ``mcp.nws``; report the resolved ID so the
            # expert is not told to install what it already has.
            resolve_mcp_selectors(
                declaration.mcps if declaration is not None else agent.mcps,
                agent.mcps,
            ),
            agent.mcps,
            True,
        ),
        (
            "Plugins",
            declaration.plugins if declaration is not None else agent.plugins,
            tuple(name for name in agent.plugins if name in installed_plugins),
            True,
        ),
    ):
        selected = tuple(dict.fromkeys(str(name) for name in declared))
        available = tuple(str(name) for name in present)
        missing = tuple(name for name in selected if name != "*" and name not in available)
        if not selected and not available:
            continue
        entry = f"- {label}: declared [{_name_list(selected)}]"
        if show_present and (selected != ("*",) or available):
            entry += f"; available [{_name_list(available)}]"
        if missing:
            entry += f"; missing [{_name_list(missing)}]"
            missing_powers.extend(f"{label.lower()}: {name}" for name in missing)
        entries.append(entry)
    lines = [
        "## Power Dependencies",
        "",
        f"- Execution mode: {_execution_mode_label(agent)}",
    ]
    if entries:
        lines.append(
            "- Declaration: the Powers this Agent's agent.yaml names. The "
            "Runtime runs only those installed on this device."
        )
        lines.extend(entries)
    if missing_powers:
        lines.extend(("", _power_install_guidance()))
    return "\n".join(lines)


def _power_install_guidance() -> str:
    """Say how a missing Power can actually be filled in this mode.

    Installing a Power writes under the Agent data root, which the sandbox
    mounts read-only, and APM needs a writable ``~/.apm`` for its catalog lock,
    which the sandbox does not have either -- ``apm list`` itself fails there.
    Promising the host recipe in that mode costs a tool call and leaves the
    model one step from reporting an installation that never happened, so the
    sandbox wording asks for the gap instead. ``unknown`` keeps the sandbox
    wording: no Worker published a mode, so nothing may be assumed about it.
    """

    if _effective_execution_mode() == "host":
        return (
            "Install a missing Power only when this task or the user needs it, "
            "through the using-apm Skill's self-provisioning flow: `apm list "
            "--installed --agent bibit`, `apm search`, `apm show`, `apm install "
            "<name> --type <type> --agent bibit`, then verify. Never claim a "
            "Power you do not have, ask before installing a Plugin or anything "
            "inferred, and without a command tool such as terminal report the "
            "gap instead of pretending to install it. A Skill applies as soon "
            "as it is installed; an MCP server or Plugin applies next turn."
        )
    return (
        "This Worker runs in a sandbox, where the Agent data root is read-only "
        "and `apm` has no writable HOME: nothing can be installed from here, so "
        "do not try. Report the exact missing Power by name and that the user "
        "has to add it on the device -- AgentHub or the Console -- before this "
        "task can use it. Never claim a Power you do not have; without a "
        "command tool such as terminal report the gap the same way."
    )


def _name_list(names: Sequence[str]) -> str:
    return ", ".join(names) if names else "none"


def _installed_plugin_ids(generation: object | None) -> frozenset[str]:
    """Return the Plugin IDs this Generation can actually provide."""

    catalog = getattr(generation, "capability_catalog", None)
    sources = getattr(catalog, "plugin_sources", None)
    if sources is None:
        sources = getattr(generation, "plugins", {}) or {}
    return frozenset(str(name) for name in sources)


# The two boundary sets are model-facing policy, so the wording is part of the
# behaviour: ``_effective_execution_mode`` decides which one a session sees.
_SANDBOX_SOFTWARE_BOUNDARIES = (
    "For download-only requests, use download-only capabilities, never install "
    "tools or installer scripts. Verify the saved artifact and report its exact "
    "user-accessible path; a file in a sandbox or private /tmp is not proof of "
    "delivery to the host. If no suitable download capability is available, "
    "explain the limitation instead of substituting installation.",
    "When the user explicitly requests installation on this device, use an "
    "authorized host installation tool and its approval flow. Do not install "
    "into the agent sandbox, switch execution modes, disable sandbox protections, "
    "or fall back to terminal sudo/nsenter to bypass a failed host tool. "
    "If the host capability is unavailable or fails, report that boundary "
    "rather than claiming a sandbox installation changed the user's device.",
)

_HOST_SOFTWARE_BOUNDARIES = (
    "For download-only requests, use download-only capabilities, never install "
    "tools or installer scripts. Verify the saved artifact and report its exact "
    "user-accessible path. If no suitable download capability is available, "
    "explain the limitation instead of substituting installation.",
    "This Worker runs directly on this device as the login user; it is not inside "
    "a container or a bwrap sandbox. Do not infer isolation, and do not refuse a "
    "task, from a private mount namespace, a seccomp filter, `CapEff: 0`, or a "
    "read-only `/usr`: those are properties of the systemd unit that hosts the "
    "Runtime, while the `Execution mode` line in Power Dependencies above is the "
    "authoritative answer. Privileged steps such as apt, systemctl, reboot or "
    "editing /etc go through sudo: this session has no TTY, so feed the password "
    "on stdin with `sudo -S`. When sudo reports that authentication is required, "
    "ask the user for the password in the conversation and retry — asking is the "
    "expected path, not a sandbox bypass, and reporting the operation as "
    "impossible instead is wrong.",
    "When the user explicitly requests installation on this device, install it on "
    "the host rather than inside the agent workspace, and never claim an "
    "installation happened unless the host command actually succeeded.",
)


def _effective_execution_mode() -> str:
    """Return the mode this process really runs under.

    ``BIANBU_SANDBOX_ACTIVE`` is published by the Runtime for both backends
    (``execution_runtime.py`` sets ``0`` for host Workers and ``1`` inside
    bwrap), so it is the only value the prompt may trust. Nothing in the
    environment means no Worker started this process: an in-process SDK caller.
    """

    active = str(os.environ.get("BIANBU_SANDBOX_ACTIVE", "")).strip().lower()
    if active in {"1", "true", "yes"}:
        return "sandbox"
    if active in {"0", "false", "no"}:
        return "host"
    return "unknown"


def _execution_mode_label(agent: AgentSpec) -> str:
    """Report the mode this process really runs under, plus what was requested."""

    metadata = agent.metadata if isinstance(agent.metadata, Mapping) else {}
    execution = metadata.get("execution")
    requested = ""
    if isinstance(execution, Mapping):
        requested = str(execution.get("requested_mode") or "").strip()
    effective = _effective_execution_mode()
    if requested:
        return f"{effective} (requested: {requested})"
    return effective


def _publish_sandbox_config(agent: AgentSpec) -> None:
    """把 Agent 的沙箱配置发布到环境，供 MCP 子进程启动时读取（INT-RT-16）。

    为什么走环境变量：MCP session 在 mcp/client.py::_open_session 里打开，
    那里只有 MCPServerConfig（server 自身信息），拿不到 Agent 上下文。
    要么把 agent_id 一路穿过 4 层函数签名，要么在此处发布 —— 选后者。

    已知边界：环境变量是**进程级**状态。多个 Agent 各配不同 sandbox.mode 时
    后建的会覆盖先建的。单 Agent 部署、或全体 Agent 同策略时没问题；
    要求严格按 Agent 隔离需多进程部署（每 Agent 一个服务实例）。
    """
    import json
    import os

    try:
        from bianbu_agent.security.security_sandbox import parse_sandbox_config

        cfg = parse_sandbox_config(agent.metadata)
    except ImportError:
        return
    # 配置非法时让 SandboxConfigError 冒泡 —— 配了沙箱却没生效比启动失败更危险

    if not cfg.enabled or cfg.mode != "mcp":
        os.environ.pop("BIANBU_MCP_SANDBOX", None)
        return
    os.environ["BIANBU_MCP_SANDBOX"] = json.dumps({
        "mode": cfg.mode,
        "net": cfg.net,
        "rw_dirs": list(cfg.rw_dirs),
        "mcp_servers": list(cfg.mcp_servers),
    })


def to_langchain_model(
    model: ChatModel, *, capture_callback: object | None = None
) -> BaseChatModel:
    """Adapt a public SDK model declaration at the LangChain boundary."""

    if isinstance(model, BaseChatModel):
        return model
    if isinstance(model, OpenAICompatibleModel):
        options: dict[str, object] = {
            "model": model.model,
            "api_key": model.api_key or "not-set",
            "timeout": model.request_timeout_seconds,
        }
        if model.profile:
            options["profile"] = {key: value for key, value in model.profile.items()
                                  if key not in {"context_source", "context_expires_at"}}
            options["metadata"] = {"model_profile": dict(model.profile)}
        elif model.base_url and model.base_url.rstrip("/") != "https://api.openai.com/v1":
            # A custom endpoint may serve a familiar model name with a much
            # smaller context than the public provider's bundled profile.
            options["profile"] = {}
        if model.base_url:
            options["base_url"] = model.base_url
        if model.temperature is not None:
            options["temperature"] = model.temperature
        if model.max_tokens is not None:
            options["max_tokens"] = model.max_tokens
        if model.provider_id == "minimax":
            options["stream_usage"] = True
        hooks_factory = getattr(capture_callback, "http_hooks", None)
        if callable(hooks_factory):
            hooks = hooks_factory(model.provider_id)
            options["http_client"] = httpx.Client(
                timeout=model.request_timeout_seconds,
                event_hooks={
                    "request": [hooks.on_request],
                    "response": [hooks.on_response],
                }
            )
            options["http_async_client"] = httpx.AsyncClient(
                timeout=model.request_timeout_seconds,
                event_hooks={
                    "request": [hooks.on_request_async],
                    "response": [hooks.on_response_async],
                }
            )
        return _ReasoningChatOpenAI(**options)
    raise TypeError(
        "default SDK engine requires OpenAICompatibleModel or a LangChain "
        "BaseChatModel; inject engine_builder for another model framework"
    )


__all__ = ["build_default_engine", "to_langchain_model"]
