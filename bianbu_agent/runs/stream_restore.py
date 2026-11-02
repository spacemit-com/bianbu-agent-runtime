"""Restore DLP stand-ins in a token stream without leaking boundaries."""

from __future__ import annotations

from collections.abc import Mapping

from bianbu_agent.security.security_dlp import replace_stand_ins


class StandInStreamRestorer:
    """Incrementally replace stand-ins that may be split across chunks.

    A model emits text in arbitrary chunks, so a stand-in such as a phone
    number can arrive as ``"1387"`` and ``"9455773"``.  Replacing each chunk
    independently would miss it.  The restorer holds back the longest suffix
    that could still grow into a stand-in, replaces the safe prefix in one
    pass, and releases the tail when the stream ends.

    Mappings can grow during a Run (an ``after_tool`` Hook masks a tool
    result before the next model step).  A mapping that is already known when
    a chunk arrives is therefore always safe; callers should update before
    feeding the chunk that may contain it.
    """

    def __init__(self) -> None:
        self._mapping: dict[str, str] = {}
        self._pending = ""
        self._hold = 0

    def update(self, mapping: Mapping[str, str] | None) -> None:
        if not mapping:
            return
        self._mapping.update(
            (str(key), str(value)) for key, value in mapping.items() if key
        )
        self._hold = max((len(key) for key in self._mapping), default=0)

    def feed(self, text: str) -> str:
        text = str(text or "")
        if not text:
            return ""
        if not self._mapping:
            return text
        self._pending += text
        # A stand-in can be split at every possible boundary.  Replacing and
        # then holding a suffix is wrong when that suffix is also a mapping
        # key: it would be restored now and then restored again on the next
        # chunk.  Instead, choose a safe cut in the *raw* text and replace only
        # the prefix before it.  The cut must leave any suffix that could still
        # grow into a stand-in; scanning every held start is what makes a cut
        # safe when the tail is itself a complete value.
        cut = self._safe_cut(self._pending)
        if cut <= 0:
            return ""
        body, self._pending = self._pending[:cut], self._pending[cut:]
        return replace_stand_ins(body, self._mapping)

    def _safe_cut(self, text: str) -> int:
        if not self._mapping:
            return len(text)
        # Only the last ``_hold`` characters can be a prefix of a key, so this
        # stays bounded even when a provider sends one huge chunk.
        cut = len(text)
        for start in range(len(text) - 1, max(-1, len(text) - self._hold - 1), -1):
            prefix = text[start:]
            if any(key.startswith(prefix) for key in self._mapping):
                cut = start
        return cut

    def flush(self) -> str:
        pending, self._pending = self._pending, ""
        return replace_stand_ins(pending, self._mapping)

    def discard_pending(self) -> None:
        self._pending = ""


__all__ = ["StandInStreamRestorer"]
