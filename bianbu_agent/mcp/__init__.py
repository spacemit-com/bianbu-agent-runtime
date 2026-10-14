"""Public exports; execution dependencies load only when requested."""

from importlib import import_module

_EXPORTS = {'MCPCallResult': ('bianbu_agent.mcp.client', 'MCPCallResult'),
 'MCPClient': ('bianbu_agent.mcp.client', 'MCPClient'),
 'MCPRemoteTool': ('bianbu_agent.mcp.client', 'MCPRemoteTool'),
 'MCPServerConfig': ('bianbu_agent.mcp.config', 'MCPServerConfig'),
 'resolve_mcp_selectors': ('bianbu_agent.mcp.selectors', 'resolve_mcp_selectors'),
 'selects_mcp': ('bianbu_agent.mcp.selectors', 'selects_mcp'),
 'load_mcp_config': ('bianbu_agent.mcp.config', 'load_mcp_config'),
 'validate_mcp_config': ('bianbu_agent.mcp.config', 'validate_mcp_config'),
 'ManagedMCPDefinition': ('bianbu_agent.mcp.managed', 'ManagedMCPDefinition'),
 'parse_managed_mcp': ('bianbu_agent.mcp.managed', 'parse_managed_mcp'),
 'parse_remote_mcp': ('bianbu_agent.mcp.managed', 'parse_remote_mcp'),
 'write_managed_mcp_package': ('bianbu_agent.mcp.managed', 'write_managed_mcp_package'),
 'InstalledMCP': ('bianbu_agent.mcp.manager', 'InstalledMCP'),
 'MCPPackageManager': ('bianbu_agent.mcp.manager', 'MCPPackageManager'),
 'MCPPreparationManager': ('bianbu_agent.mcp.preparation', 'MCPPreparationManager'),
 'MCPSourceCatalog': ('bianbu_agent.mcp.sources', 'MCPSourceCatalog'),
 'MCPSourceRecord': ('bianbu_agent.mcp.sources', 'MCPSourceRecord'),
 'runtime_mcp_id': ('bianbu_agent.mcp.sources', 'runtime_mcp_id')}

def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = ['MCPCallResult',
 'MCPClient',
 'MCPRemoteTool',
 'MCPServerConfig',
 'ManagedMCPDefinition',
 'InstalledMCP',
 'MCPPackageManager',
 'MCPPreparationManager',
 'MCPSourceCatalog',
 'MCPSourceRecord',
 'load_mcp_config',
 'parse_managed_mcp',
 'parse_remote_mcp',
 'validate_mcp_config',
 'write_managed_mcp_package',
 'resolve_mcp_selectors',
 'selects_mcp',
 'runtime_mcp_id']
