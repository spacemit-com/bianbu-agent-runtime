"""Small dependency-free Prometheus exposition for the control plane."""

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Mapping


_NAMES = (
    "runtime_control_up",
    "runtime_control_http_requests_total",
    "runtime_worker_instances",
    "runtime_worker_start_seconds",
    "runtime_worker_handshake_seconds",
    "runtime_worker_restarts_total",
    "runtime_worker_orphans_reaped_total",
    "runtime_worker_heartbeat_failures_total",
    "runtime_security_prepare_total",
    "runtime_security_commit_total",
    "runtime_security_stop_total",
)


def _labels(values: Mapping[str, object]) -> str:
    if not values:
        return ""
    escaped = []
    for key, value in sorted(values.items()):
        text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        escaped.append(f'{key}="{text}"')
    return "{" + ",".join(escaped) + "}"


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = defaultdict(float)
        self._label_values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        for name in _NAMES:
            self._values[name] = 0.0
        self.set("runtime_control_up", 1)

    def set(self, name: str, value: float, **labels: object) -> None:
        self._put(name, value, labels, add=False)

    def inc(self, name: str, value: float = 1, **labels: object) -> None:
        self._put(name, value, labels, add=True)

    def _put(self, name: str, value: float, labels: Mapping[str, object], *, add: bool) -> None:
        if name not in _NAMES:
            raise ValueError(f"unknown runtime metric: {name}")
        normalized = tuple(sorted((str(k), str(v)) for k, v in labels.items()))
        with self._lock:
            if normalized:
                key = (name, normalized)
                self._label_values[key] = self._label_values[key] + value if add else value
            else:
                self._values[name] = self._values[name] + value if add else value

    def render(self) -> str:
        lines = ["# TYPE runtime_control_up gauge"]
        with self._lock:
            values = dict(self._values)
            labelled = dict(self._label_values)
        for name in _NAMES:
            if name in values:
                lines.append(f"{name} {values[name]:g}")
        for (name, labels), value in sorted(labelled.items()):
            lines.append(f"{name}{_labels(dict(labels))} {value:g}")
        return "\n".join(lines) + "\n"


__all__ = ["RuntimeMetrics"]
