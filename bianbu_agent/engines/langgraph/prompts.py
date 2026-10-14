from __future__ import annotations

from collections.abc import Sequence

from langchain_core.messages import HumanMessage

from bianbu_agent.sessions.inbox import SessionInboxMessage


InitialPrompt = str | Sequence[SessionInboxMessage]


def initial_human_messages(
    prompt: InitialPrompt,
    *,
    message_id: str,
) -> list[HumanMessage]:
    """Create stable graph input while preserving the legacy string ID."""

    if isinstance(prompt, str):
        return [HumanMessage(content=prompt, id=message_id)]
    return [
        HumanMessage(
            content=item.content,
            id=item.message_id,
            additional_kwargs={
                "session_input_mode": "steer",
                "attachments": [dict(value) for value in item.attachments],
            },
        )
        for item in prompt
    ]


def initial_prompt_text(prompt: InitialPrompt | None) -> str:
    """The user text a Run was started with, for Hook/审计上下文。

    与 ``initial_human_messages`` 同源：命令 Hook 拿到的 ``prompt`` 必须就是
    用户写下的那句话，不能拿工具名之类的内部标识冒充（Bibit Intent 的
    volume/brightness 守卫就被这件事坑过）。
    """

    if prompt is None:
        return ""
    if isinstance(prompt, str):
        return prompt
    return "\n".join(str(item.content) for item in prompt)


__all__ = ["InitialPrompt", "initial_human_messages", "initial_prompt_text"]
