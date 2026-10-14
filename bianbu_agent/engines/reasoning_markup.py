"""Split in-band provider reasoning markup from the public answer text."""

from __future__ import annotations

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


class ThinkStreamParser:
    """Split MiniMax's in-band think markup without exposing tag fragments."""

    def __init__(self) -> None:
        self._buffer = ""
        self._reasoning = False

    def feed(self, value: str) -> list[tuple[str, str]]:
        self._buffer += value
        output: list[tuple[str, str]] = []
        while self._buffer:
            tag = _THINK_CLOSE if self._reasoning else _THINK_OPEN
            index = self._buffer.find(tag)
            if index >= 0:
                self._append(output, self._buffer[:index])
                self._buffer = self._buffer[index + len(tag) :]
                self._reasoning = not self._reasoning
                continue
            retained = _tag_prefix_suffix_length(self._buffer, tag)
            boundary = len(self._buffer) - retained
            self._append(output, self._buffer[:boundary])
            self._buffer = self._buffer[boundary:]
            break
        return output

    def finish(self) -> list[tuple[str, str]]:
        # ``feed`` retains only a suffix that could become the active
        # delimiter. At EOF/cancellation it must be discarded: emitting it
        # would leak fragments such as ``<thi`` into the public answer.
        self._buffer = ""
        self._reasoning = False
        return []

    def _append(self, output: list[tuple[str, str]], text: str) -> None:
        if not text:
            return
        kind = "reasoning" if self._reasoning else "token"
        if output and output[-1][0] == kind:
            previous_kind, previous_text = output[-1]
            output[-1] = (previous_kind, previous_text + text)
        else:
            output.append((kind, text))


def _tag_prefix_suffix_length(value: str, tag: str) -> int:
    maximum = min(len(value), len(tag) - 1)
    for length in range(maximum, 0, -1):
        if value.endswith(tag[:length]):
            return length
    return 0


def strip_reasoning_markup(value: str) -> str:
    """Return provider text with in-band reasoning markup removed.

    Providers that stream reasoning in ``content`` (MiniMax) wrap it in
    ``<think>`` tags. Callers that consume a complete answer -- Session titles,
    for example -- must look at the same visible text a user would see.
    """

    parser = ThinkStreamParser()
    parts = parser.feed(value)
    parts.extend(parser.finish())
    return "".join(text for kind, text in parts if kind == "token")


__all__ = ["ThinkStreamParser", "strip_reasoning_markup"]
