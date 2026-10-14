"""Package roots that hold directory Plugins.

The Worker discovers Plugins from the system resource directory, the roots
declared in ``RuntimeConfig`` and the managed directory inside the user
profile. The Control plane needs the same answer when it loads a host-side
Plugin Console outside the sandbox, so both sides must agree on the order.
"""

from __future__ import annotations

from pathlib import Path

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.system_resources import system_resource_dir


def plugin_package_roots(config: RuntimeConfig) -> tuple[Path, ...]:
    """Return Plugin roots in discovery order, lowest precedence first."""
    managed = Path(config.data_dir) / "plugins"
    return tuple(dict.fromkeys((
        system_resource_dir("plugins"),
        *(Path(item) for item in config.plugin_dirs),
        managed,
    )))


def resolve_plugin_root(config: RuntimeConfig, plugin_id: str) -> Path:
    """Return the directory of the Plugin that discovery would select."""
    roots = plugin_package_roots(config)
    for root in reversed(roots):
        candidate = root / plugin_id
        if candidate.is_dir():
            return candidate
    searched = ", ".join(str(root) for root in roots)
    raise FileNotFoundError(
        f"Plugin {plugin_id!r} was not found in any Plugin root: {searched}"
    )


__all__ = ["plugin_package_roots", "resolve_plugin_root"]
