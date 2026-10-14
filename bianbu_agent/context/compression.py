from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from bianbu_agent.context.config import CompressionSettings


@dataclass(frozen=True, slots=True)
class CompactionPlan:
    head: tuple[BaseMessage, ...]
    to_summarize: tuple[BaseMessage, ...]
    tail: tuple[BaseMessage, ...]
    before_tokens: int
    static_tokens: int = 0
    input_limit: int = 0
    target_tokens: int = 0


@dataclass(frozen=True, slots=True)
class CompactionResult:
    messages: tuple[BaseMessage, ...]
    before_tokens: int
    after_tokens: int
    retained_messages: int
    marker_id: str
    used_fallback: bool


class ContextCompactor:
    """Plan safe checkpoint rewrites without splitting causal tool groups."""

    def estimate_tokens(self, messages: Sequence[BaseMessage]) -> int:
        return sum(_message_tokens(message) for message in messages)

    def plan(
        self,
        messages: Sequence[BaseMessage],
        settings: CompressionSettings,
        *,
        force: bool = False,
        static_tokens: int = 0,
    ) -> CompactionPlan | None:
        selected = tuple(messages)
        static = max(0, int(static_tokens))
        before = static + self.estimate_tokens(selected)
        if not force and before < settings.threshold_tokens:
            return None
        systems = tuple(message for message in selected if isinstance(message, SystemMessage))
        groups = _causal_groups(tuple(message for message in selected
                                     if not isinstance(message, SystemMessage)))
        if len(groups) < 2:
            return None

        head_count = _head_group_count(groups, settings.protect_first_n)
        latest_request = next((group for group in reversed(groups)
                               if any(isinstance(message, HumanMessage)
                                      and compression_metadata(message) is None
                                      for message in group)), ())
        mandatory = (*systems, *_flatten(groups[:head_count]))
        mandatory_tokens = self.estimate_tokens(mandatory)
        if latest_request not in groups[:head_count]:
            mandatory_tokens += self.estimate_tokens(latest_request)
        available = settings.usable_input_tokens - static - mandatory_tokens
        # Static tool schemas and the current task cannot be summarized away.
        # Avoid repeatedly rewriting history when they already fill the window.
        if available < 64:
            return None
        fixed = static + mandatory_tokens
        target = settings.target_tokens
        threshold = settings.threshold_tokens
        if fixed + 32 >= target:
            # The ordinary target may be smaller than the immutable prompt.
            # Apply hysteresis to the space that can actually be reclaimed.
            target = fixed + available // 2
            threshold = max(threshold, fixed + int(available * .80))
        if not force and before < threshold:
            return None
        summary_reserve = min(512, max(32, (target - fixed) // 4))
        tail_budget = max(0, target - fixed - summary_reserve)
        tail_start = _tail_group_start(
            groups,
            floor=head_count,
            desired_messages=settings.protect_last_n,
            token_budget=tail_budget,
        )
        latest_human_group = next(
            (
                index
                for index in range(len(groups) - 1, head_count - 1, -1)
                if any(
                    isinstance(message, HumanMessage)
                    and compression_metadata(message) is None
                    for message in groups[index]
                )
            ),
            None,
        )
        current_request = ()
        if latest_human_group is not None:
            if latest_human_group + 1 < tail_start:
                # A single long Run may contain many tool rounds without a
                # new user message. Pin the current task separately rather
                # than protecting every old tool round since that request.
                current_request = groups[latest_human_group]
            else:
                tail_start = min(tail_start, latest_human_group)
        middle = tuple(group for index, group in enumerate(groups[head_count:tail_start], head_count)
                       if not current_request or index != latest_human_group)
        retained_tool_limit = max(64, min(settings.target_tokens * 2, tail_budget))
        retained = _flatten((*groups[:head_count], *groups[tail_start:]))
        if not middle and before <= settings.usable_input_tokens and not any(
            isinstance(message, ToolMessage)
            and _content_size(message.content) > retained_tool_limit
            for message in retained
        ):
            return None
        head = _compact_retained_tool_results(
                (*systems, *_flatten(groups[:head_count]), *current_request),
                limit=retained_tool_limit,
            )
        tail = _compact_retained_tool_results(
                _flatten(groups[tail_start:]),
                limit=retained_tool_limit,
            )
        summarized = list(_flatten(middle))
        # The latest causal group can itself exceed the remaining budget
        # (large call arguments or many parallel results). Move whole groups
        # into the summary; never leave orphaned tool results or drop the task.
        tail_groups = list(_causal_groups(tail))
        original_tail_groups = list(_causal_groups(_flatten(groups[tail_start:])))
        retained_limit = target - static - summary_reserve
        while self.estimate_tokens((*head, *_flatten(tail_groups))) > retained_limit:
            removable = next((index for index, group in enumerate(tail_groups)
                              if not any(isinstance(message, HumanMessage)
                                         and compression_metadata(message) is None
                                         for message in group)), None)
            if removable is None:
                return None
            tail_groups.pop(removable)
            summarized.extend(original_tail_groups.pop(removable))
        return CompactionPlan(
            head=head,
            to_summarize=tuple(summarized),
            tail=_flatten(tail_groups),
            before_tokens=before,
            static_tokens=static,
            input_limit=settings.usable_input_tokens,
            target_tokens=target,
        )

    def complete(
        self,
        plan: CompactionPlan,
        summary: str,
        *,
        used_fallback: bool,
        emergency: bool = False,
    ) -> CompactionResult:
        marker_id = f"context-compression:{uuid.uuid4()}"
        payload = {
            "before_tokens": plan.before_tokens,
            "emergency": emergency,
            "retained_messages": len(plan.head) + len(plan.tail),
            "used_fallback": used_fallback,
        }
        marker = HumanMessage(
            content=(
                "Here is a compact summary of earlier conversation context:\n\n"
                + str(summary).strip()
            ),
            id=marker_id,
            additional_kwargs={
                "lc_source": "context_compression",
                "context_compression": payload,
            },
        )
        messages = (*plan.head, marker, *plan.tail)
        after = plan.static_tokens + self.estimate_tokens(messages)
        limit = plan.target_tokens or plan.input_limit
        if limit and after > limit:
            original = str(marker.content)
            low, high = 0, len(original)
            while low < high:
                size = (low + high + 1) // 2
                marker.content = _clip(original, size)
                cost = plan.static_tokens + self.estimate_tokens(messages)
                if cost <= limit:
                    low = size
                else:
                    high = size - 1
            marker.content = _clip(original, low)
            after = plan.static_tokens + self.estimate_tokens(messages)
        marker.additional_kwargs["context_compression"]["after_tokens"] = after
        return CompactionResult(
            messages=messages,
            before_tokens=plan.before_tokens,
            after_tokens=after,
            retained_messages=len(plan.head) + len(plan.tail),
            marker_id=marker_id,
            used_fallback=used_fallback,
        )

    def compact_emergency(
        self,
        messages: Sequence[BaseMessage],
        settings: CompressionSettings,
        *,
        static_tokens: int = 0,
    ) -> CompactionResult | None:
        plan = self.plan(
            messages,
            settings,
            force=True,
            static_tokens=static_tokens,
        )
        if plan is None:
            return None
        return self.complete(
            plan,
            deterministic_summary(plan.to_summarize),
            used_fallback=True,
            emergency=True,
        )


def deterministic_summary(messages: Sequence[BaseMessage]) -> str:
    header = "Previous conversation was compacted deterministically."
    records: list[tuple[int, str]] = []
    results = {message.tool_call_id: message for message in messages
               if isinstance(message, ToolMessage)}
    consumed: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = str(call.get("id") or "")
                name = str(call.get("name") or "tool")
                arguments = json.dumps(call.get("args") or {}, ensure_ascii=False, default=str)
                record = f"- tool {name} call={call_id} args={_meaningful_excerpt(arguments, 700)}"
                result = results.get(call_id)
                if result is not None:
                    consumed.add(call_id)
                    record += "; " + _tool_result_digest(result)
                records.append((1, record))
        if isinstance(message, ToolMessage):
            if message.tool_call_id not in consumed:
                records.append((1, f"- tool {message.name or 'tool'} call={message.tool_call_id}: "
                                + _tool_result_digest(message)))
            continue
        role = (
            "user"
            if isinstance(message, HumanMessage)
            else "assistant"
            if isinstance(message, AIMessage)
            else "system"
            if isinstance(message, SystemMessage)
            else message.type
        )
        text = _message_text(message.content).strip().replace("\x00", "")
        if text:
            # Existing summaries and user constraints need more room than one
            # routine tool record when another long Run is compacted.
            weight = 3 if isinstance(message, (HumanMessage, SystemMessage)) else 1
            records.append((weight, f"- {role}: {text}"))
    if not records:
        return header
    available = 6_000 - len(header) - len(records)
    weight_total = sum(weight for weight, _text in records)
    lines = [header]
    for weight, text in records:
        budget = max(0, available * weight // weight_total)
        if budget:
            lines.append(_meaningful_excerpt(text, budget))
    return "\n".join(lines)[:6_000]


def _tool_result_digest(message: ToolMessage) -> str:
    status = "failed" if message.status == "error" else "completed"
    text = _meaningful_excerpt(_message_text(message.content), 900)
    return f"{status} ({_content_size(message.content):,} chars): {text}"


def _meaningful_excerpt(text: str, limit: int) -> str:
    """Keep work evidence while bounding logs and opaque binary payloads."""
    text = re.sub(r"[A-Za-z0-9+/=_-]{512,}",
                  lambda match: f"[{len(match.group()):,} opaque chars omitted]", text)
    if len(text) <= limit:
        return text
    important = [line for line in text.splitlines() if re.search(
        r"args=|[/\\]|error|fail|SIG\w+|exception|exit.?code|passed|created|changed|saved|wrote",
        line, re.IGNORECASE,
    )]
    if important:
        text = "\n".join(important)
    if limit < 48:
        return text[:limit]
    return _clip(text, limit)


def compression_metadata(message: BaseMessage) -> Mapping[str, object] | None:
    value = message.additional_kwargs.get("context_compression")
    return value if isinstance(value, Mapping) else None


def _causal_groups(
    messages: tuple[BaseMessage, ...],
) -> tuple[tuple[BaseMessage, ...], ...]:
    groups: list[tuple[BaseMessage, ...]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, HumanMessage):
            if compression_metadata(message) is not None:
                groups.append((message,))
                index += 1
                continue
            group = [message]
            candidate = index + 1
            while candidate < len(messages) and isinstance(
                messages[candidate],
                HumanMessage,
            ) and compression_metadata(messages[candidate]) is None:
                group.append(messages[candidate])
                candidate += 1
            groups.append(tuple(group))
            index = candidate
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            ids = {
                str(call.get("id") or "")
                for call in message.tool_calls
                if call.get("id")
            }
            group = [message]
            candidate = index + 1
            while candidate < len(messages):
                tool = messages[candidate]
                if not isinstance(tool, ToolMessage):
                    break
                if str(tool.tool_call_id or "") not in ids:
                    break
                group.append(tool)
                candidate += 1
            groups.append(tuple(group))
            index = candidate
            continue
        groups.append((message,))
        index += 1
    return tuple(groups)


def _head_group_count(
    groups: tuple[tuple[BaseMessage, ...], ...],
    protected_messages: int,
) -> int:
    if protected_messages <= 0:
        return 0
    count = 0
    protected = 0
    for group in groups:
        if any(compression_metadata(message) is not None for message in group):
            break
        count += 1
        protected += sum(
            not isinstance(message, SystemMessage) for message in group
        )
        if protected >= protected_messages:
            break
    return count


def _tail_group_start(
    groups: tuple[tuple[BaseMessage, ...], ...],
    *,
    floor: int,
    desired_messages: int,
    token_budget: int,
) -> int:
    start = len(groups)
    retained_messages = 0
    retained_tokens = 0
    for index in range(len(groups) - 1, floor - 1, -1):
        group = groups[index]
        group_tokens = sum(_message_tokens(message) for message in group)
        if start < len(groups) and (
            retained_messages >= desired_messages
            or retained_tokens + group_tokens > token_budget
        ):
            break
        start = index
        retained_messages += len(group)
        retained_tokens += group_tokens
    # Retain whole user/assistant turns, in addition to indivisible tool-call
    # groups. A tail starting at an assistant answer loses its user request.
    if start < len(groups) and not any(isinstance(message, HumanMessage)
                                       for message in groups[start]):
        for index in range(start - 1, floor - 1, -1):
            if any(isinstance(message, HumanMessage) for message in groups[index]):
                turn_tokens = sum(_message_tokens(message)
                                  for group in groups[index:] for message in group)
                if turn_tokens <= token_budget:
                    start = index
                else:
                    # Retain fewer complete turns instead of pulling an
                    # oversized older request back into the protected tail.
                    start = next((candidate for candidate in range(start, len(groups))
                                  if any(isinstance(message, HumanMessage)
                                         for message in groups[candidate])), start)
                break
    return start


def _flatten(
    groups: Sequence[Sequence[BaseMessage]],
) -> tuple[BaseMessage, ...]:
    return tuple(message for group in groups for message in group)


def _compact_retained_tool_results(
    messages: tuple[BaseMessage, ...],
    *,
    limit: int,
) -> tuple[BaseMessage, ...]:
    compacted: list[BaseMessage] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            compacted.append(message)
            continue
        size = _content_size(message.content)
        if size <= limit:
            compacted.append(message)
            continue
        status = "failed" if message.status == "error" else "excerpt"
        prefix = f"[{size:,} chars; {status}]\n"
        excerpt = _meaningful_excerpt(
            _message_text(message.content), max(0, limit - len(prefix))
        )
        compacted.append(
            message.model_copy(
                deep=True,
                update={
                    "content": prefix + excerpt
                },
            )
        )
    return tuple(compacted)


def _message_tokens(message: BaseMessage) -> int:
    reasoning = message.additional_kwargs.get("reasoning_content")
    content = _message_text(message.content)
    tool_calls = getattr(message, "tool_calls", None)
    serialized_calls = ""
    if tool_calls:
        try:
            serialized_calls = json.dumps(tool_calls, ensure_ascii=False, default=str)
        except Exception:
            serialized_calls = str(tool_calls)
    text = content + serialized_calls + (reasoning if isinstance(reasoning, str) else "")
    non_ascii = sum(ord(character) > 127 for character in text)
    return 4 + non_ascii + math.ceil((len(text) - non_ascii) / 4)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        block_type = str(content.get("type") or "").casefold()
        return str(content.get("text") or "") if "text" in block_type else ""
    if isinstance(content, Sequence) and not isinstance(
        content,
        (str, bytes, bytearray),
    ):
        return "".join(_message_text(item) for item in content)
    return str(content)


def _content_size(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    try:
        return len(json.dumps(content, ensure_ascii=False, default=str))
    except Exception:
        return len(str(content))


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    marker = f" …[{omitted:,} chars omitted]… "
    retained = max(0, limit - len(marker))
    head = (retained + 1) // 2
    tail = retained - head
    return value[:head] + marker + (value[-tail:] if tail else "")


__all__ = [
    "CompactionPlan",
    "CompactionResult",
    "ContextCompactor",
    "compression_metadata",
    "deterministic_summary",
]
