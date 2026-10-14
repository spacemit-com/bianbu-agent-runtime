from __future__ import annotations


class MessageIdCodec:
    """Create and recognize the stable message IDs one host addresses Runs with."""

    def __init__(self, namespace: str = "bianbu-agent") -> None:
        normalized = str(namespace).strip().removesuffix("-run:")
        if not normalized:
            raise ValueError("message namespace must not be empty")
        self._prefix = f"{normalized}-run:"

    @property
    def prefix(self) -> str:
        return self._prefix

    def for_run(self, request_id: str) -> str:
        normalized = str(request_id).strip()
        if not normalized:
            raise ValueError("request_id must not be empty")
        return f"{self._prefix}{normalized}"
