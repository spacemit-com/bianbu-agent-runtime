"""Lazily resolve public exports without starting Runtime dependencies."""

from importlib import import_module

_EXPORTS = {
    "create_app": "bianbu_agent.server.app",
    "serve_app": "bianbu_agent.server.app",
    "create_runtime": "bianbu_agent.server.factory",
    "create_runtime_app": "bianbu_agent.server.factory",
    "RuntimeProcessOwner": "bianbu_agent.server.process",
    "stop_runtime": "bianbu_agent.server.process",
    "BianbuAgentRuntime": "bianbu_agent.server.runtime",
    "RuntimeDiagnostics": "bianbu_agent.server.runtime",
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
    "BianbuAgentRuntime",
    "RuntimeDiagnostics",
    "RuntimeProcessOwner",
    "create_app",
    "create_runtime",
    "create_runtime_app",
    "serve_app",
    "stop_runtime",
]
