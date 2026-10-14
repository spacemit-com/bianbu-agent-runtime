"""Public exports; execution dependencies load only when requested."""

from importlib import import_module

_EXPORTS = {'tool': ('langchain_core.tools', 'tool'),
 'get_task_skills': ('bianbu_agent.skills.versions', 'get_task_skills'),
 'mutate_kanban': ('bianbu_agent.tools.kanban_state', 'mutate_kanban'),
 'ToolDirectoryCandidate': ('bianbu_agent.tools.catalog', 'ToolDirectoryCandidate'),
 'ToolDirectoryCatalog': ('bianbu_agent.tools.catalog', 'ToolDirectoryCatalog'),
 'DirectoryToolLoader': ('bianbu_agent.tools.loader', 'DirectoryToolLoader'),
 'ToolLoadContext': ('bianbu_agent.tools.models', 'ToolLoadContext'),
 'ToolPackageContext': ('bianbu_agent.tools.models', 'ToolPackageContext'),
 'ToolSourceInspection': ('bianbu_agent.tools.models', 'ToolSourceInspection'),
 'ProcessHandle': ('bianbu_agent.tools.processes', 'ProcessHandle'),
 'ProcessManager': ('bianbu_agent.tools.processes', 'ProcessManager'),
 'ProcessRequest': ('bianbu_agent.tools.processes', 'ProcessRequest'),
 'ProcessResult': ('bianbu_agent.tools.processes', 'ProcessResult'),
 'ToolExecutionScope': ('bianbu_agent.tools.runtime', 'ToolExecutionScope'),
 'ToolPermissionError': ('bianbu_agent.tools.runtime', 'ToolPermissionError'),
 'ToolRuntime': ('bianbu_agent.tools.runtime', 'ToolRuntime'),
 'ToolRuntimeFactory': ('bianbu_agent.tools.runtime', 'ToolRuntimeFactory'),
 'ToolRuntimeServices': ('bianbu_agent.tools.runtime', 'ToolRuntimeServices')}

def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = ['DirectoryToolLoader',
 'get_task_skills',
 'ToolDirectoryCandidate',
 'ToolDirectoryCatalog',
 'ToolLoadContext',
 'ToolPackageContext',
 'ToolSourceInspection',
 'ToolExecutionScope',
 'ToolPermissionError',
 'ToolRuntime',
 'ToolRuntimeFactory',
 'ToolRuntimeServices',
 'ProcessHandle',
 'ProcessManager',
 'ProcessRequest',
 'ProcessResult',
 'tool',
 'mutate_kanban']
