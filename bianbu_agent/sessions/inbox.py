"""Immutable values for the durable Session input inbox."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class SessionInboxMessage:
    """One user input accepted by a Session before a Run claims it."""

    message_id: str
    content: str
    attachments: tuple[Mapping[str, object], ...] = ()
    skill_ids: tuple[str, ...] = ()
    created_at: float = 0.0

    def __post_init__(self) -> None:
        message_id = str(self.message_id).strip()
        content = str(self.content).strip()
        if not message_id:
            raise ValueError("message_id must not be empty")
        if not content:
            raise ValueError("content must not be empty")
        attachments = tuple(
            MappingProxyType(dict(attachment)) for attachment in self.attachments
        )
        skill_ids = tuple(
            dict.fromkeys(str(skill_id).strip() for skill_id in self.skill_ids)
        )
        if any(not skill_id for skill_id in skill_ids):
            raise ValueError("skill_ids must not contain empty values")
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "content", content)
        object.__setattr__(self, "attachments", attachments)
        object.__setattr__(self, "skill_ids", skill_ids)
        object.__setattr__(self, "created_at", float(self.created_at))


@dataclass(frozen=True, slots=True)
class SessionInboxSnapshot:
    """Steering messages pending at the next model-step boundary."""

    next_step: tuple[SessionInboxMessage, ...] = ()


def inbox_message_to_dict(message: SessionInboxMessage) -> dict[str, object]:
    return {
        "message_id": message.message_id,
        "content": message.content,
        "attachments": [dict(attachment) for attachment in message.attachments],
        "skill_ids": list(message.skill_ids),
        "created_at": message.created_at,
    }


def inbox_message_from_dict(raw: Mapping[str, object]) -> SessionInboxMessage:
    unknown = set(raw) - {
        "message_id",
        "content",
        "attachments",
        "skill_ids",
        "created_at",
    }
    if unknown:
        raise ValueError(
            f"unknown inbox message fields: {', '.join(sorted(unknown))}"
        )
    attachments = raw.get("attachments")
    if not isinstance(attachments, list) or any(
        not isinstance(attachment, Mapping) for attachment in attachments
    ):
        raise ValueError("inbox message attachments must be a list of objects")
    skill_ids = raw.get("skill_ids", [])
    if not isinstance(skill_ids, list) or any(
        not isinstance(skill_id, str) for skill_id in skill_ids
    ):
        raise ValueError("inbox message skill_ids must be a list of strings")
    return SessionInboxMessage(
        message_id=str(raw.get("message_id") or ""),
        content=str(raw.get("content") or ""),
        attachments=tuple(dict(attachment) for attachment in attachments),
        skill_ids=tuple(skill_ids),
        created_at=float(raw.get("created_at") or 0.0),
    )


class SessionInboxProjection:
    """Validated replay projection for one Session's inbox events."""

    def __init__(self) -> None:
        self._messages: dict[str, SessionInboxMessage] = {}
        self._claimed_by: dict[str, str] = {}
        self._next_step: list[SessionInboxMessage] = []

    def known(self, message_id: str) -> SessionInboxMessage | None:
        return self._messages.get(str(message_id))

    def claimed_run(self, message_id: str) -> str | None:
        return self._claimed_by.get(str(message_id))

    def snapshot(self) -> SessionInboxSnapshot:
        return SessionInboxSnapshot(next_step=tuple(self._next_step))

    def claimable(self) -> tuple[SessionInboxMessage, ...]:
        return tuple(self._next_step)

    def apply_splice(self, message: SessionInboxMessage) -> None:
        if message.message_id in self._messages:
            raise ValueError(
                f"duplicate persisted inbox message_id: {message.message_id}"
            )
        self._messages[message.message_id] = message
        self._next_step.append(message)

    def apply_claim(self, message_ids: tuple[str, ...], *, run_id: str) -> None:
        step_prefix = tuple(
            message.message_id for message in self._next_step[:len(message_ids)]
        )
        if not message_ids or message_ids != step_prefix:
            raise ValueError(
                f"invalid persisted inbox claim: expected a step prefix of "
                f"{step_prefix!r}, got {message_ids!r}"
            )
        for message_id in message_ids:
            self._claimed_by[message_id] = str(run_id)
        del self._next_step[:len(message_ids)]

    def to_state(self) -> dict[str, object]:
        """The projection as plain JSON, for the persisted projection cache.

        Splices and claims are not replayed to get here: a claim removes an
        exact prefix, so the three fields carry the same information and
        restore it without re-deriving event order.
        """

        return {
            "messages": [
                inbox_message_to_dict(message) for message in self._messages.values()
            ],
            "claimed_by": dict(self._claimed_by),
            "next_step": [message.message_id for message in self._next_step],
        }

    @classmethod
    def from_state(cls, raw: object) -> "SessionInboxProjection | None":
        """Rebuild one projection from cache state; ``None`` when unusable."""

        if not isinstance(raw, Mapping):
            return None
        messages = raw.get("messages")
        claimed_by = raw.get("claimed_by")
        next_step = raw.get("next_step")
        if (
            not isinstance(messages, list)
            or not isinstance(claimed_by, Mapping)
            or not isinstance(next_step, list)
        ):
            return None
        projection = cls()
        try:
            for entry in messages:
                if not isinstance(entry, Mapping):
                    return None
                message = inbox_message_from_dict(entry)
                projection._messages[message.message_id] = message
            for message_id in next_step:
                if not isinstance(message_id, str):
                    return None
                message = projection._messages.get(message_id)
                if message is None:
                    return None
                projection._next_step.append(message)
            for message_id, run_id in claimed_by.items():
                if not isinstance(message_id, str) or not isinstance(run_id, str):
                    return None
                projection._claimed_by[message_id] = run_id
        except Exception:  # noqa: BLE001 - a cache row must never break a start
            return None
        return projection


__all__ = [
    "SessionInboxMessage",
    "SessionInboxProjection",
    "SessionInboxSnapshot",
    "inbox_message_from_dict",
    "inbox_message_to_dict",
]
