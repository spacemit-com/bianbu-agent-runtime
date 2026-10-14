"""Public exports; execution dependencies load only when requested."""

from importlib import import_module

_EXPORTS = {'BackendHandle': ('bianbu_agent.security.execution_runtime', 'BackendHandle'),
 'BwrapBackend': ('bianbu_agent.security.execution_runtime', 'BwrapBackend'),
 'ExecutionCoordinator': ('bianbu_agent.security.execution_runtime', 'ExecutionCoordinator'),
 'ExecutionRecord': ('bianbu_agent.security.execution_runtime', 'ExecutionRecord'),
 'ExecutionRequest': ('bianbu_agent.security.execution_runtime', 'ExecutionRequest'),
 'ExecutionState': ('bianbu_agent.security.execution_runtime', 'ExecutionState'),
 'HostProcessBackend': ('bianbu_agent.security.execution_runtime', 'HostProcessBackend'),
 'RuntimeExecutionError': ('bianbu_agent.security.execution_runtime', 'RuntimeExecutionError'),
 'RuntimeStartDecision': ('bianbu_agent.security.security_runtime', 'RuntimeStartDecision'),
 'RuntimeStartRequest': ('bianbu_agent.security.security_runtime', 'RuntimeStartRequest'),
 'SecurityRuntimeClient': ('bianbu_agent.security.security_runtime', 'SecurityRuntimeClient'),
 'SecurityRuntimeRejected': ('bianbu_agent.security.security_runtime',
                             'SecurityRuntimeRejected'),
 'SecurityRuntimeUnavailable': ('bianbu_agent.security.security_runtime',
                                'SecurityRuntimeUnavailable')}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = ['BackendHandle',
 'BwrapBackend',
 'ExecutionCoordinator',
 'ExecutionRecord',
 'ExecutionRequest',
 'ExecutionState',
 'HostProcessBackend',
 'RuntimeExecutionError',
 'RuntimeStartDecision',
 'RuntimeStartRequest',
 'SecurityRuntimeClient',
 'SecurityRuntimeRejected',
 'SecurityRuntimeUnavailable']
