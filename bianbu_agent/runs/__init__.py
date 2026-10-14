"""Public contracts loaded independently from persistence implementations."""

from importlib import import_module

_EXPORTS = {
    "RunError": "bianbu_agent.runs.contracts",
    "RunHandle": "bianbu_agent.runs.contracts",
    "RunResult": "bianbu_agent.runs.contracts",
    "Usage": "bianbu_agent.runs.contracts",
    "decode_result": "bianbu_agent.runs.contracts",
    "encode_error": "bianbu_agent.runs.contracts",
    "encode_result": "bianbu_agent.runs.contracts",
    "EventKind": "bianbu_agent.runs.events",
    "RunEvent": "bianbu_agent.runs.events",
    "StoredRun": "bianbu_agent.runs.models",
    "SQLiteRunStore": "bianbu_agent.runs.stores",
}


def __getattr__(name: str):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = [
    "EventKind",
    "RunError",
    "RunEvent",
    "RunHandle",
    "RunResult",
    "SQLiteRunStore",
    "StoredRun",
    "Usage",
    "decode_result",
    "encode_error",
    "encode_result",
]
