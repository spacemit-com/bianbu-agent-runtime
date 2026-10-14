from __future__ import annotations

from bianbu_agent.engines.langgraph.prompts import (
    initial_human_messages,
    initial_prompt_text,
)
from bianbu_agent.sessions import SessionInboxMessage


def test_initial_messages_preserve_claimed_session_input_identity_and_order() -> None:
    """Flattening claimed inputs would destroy durable identity and attachments."""

    claimed = (
        SessionInboxMessage(
            "message-step",
            "change direction",
            attachments=({"name": "steer.txt"},),
        ),
        SessionInboxMessage("message-next", "next correction"),
    )

    messages = initial_human_messages(claimed, message_id="run-generated-id")

    assert [message.id for message in messages] == [
        "message-step",
        "message-next",
    ]
    assert [message.content for message in messages] == [
        "change direction",
        "next correction",
    ]
    assert messages[0].additional_kwargs == {
        "session_input_mode": "steer",
        "attachments": [{"name": "steer.txt"}],
    }


def test_initial_prompt_text_is_the_user_input_joined() -> None:
    """Hook 里的 prompt 必须就是用户那句话，不能是内部标识。"""

    assert initial_prompt_text('把音量调到1') == '把音量调到1'
    assert initial_prompt_text((
        SessionInboxMessage('message-step', 'change direction'),
        SessionInboxMessage('message-next', 'next correction'),
    )) == 'change direction\nnext correction'
    assert initial_prompt_text(None) == ''
