"""Directory Tools whose identity and payload belong to one Agent package."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from bianbu_agent.agents.manifest import AgentManifest
from bianbu_agent.plugins import ToolContribution
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext


def load_bundled_tools(
    root: Path,
    manifest: AgentManifest,
    *,
    workspace_root: Path,
    services: Mapping[str, object] | None = None,
) -> dict[str, ToolContribution]:
    """Validate factories and keep qualified identities identical at every stage."""
    tools_root = root / "tools"
    if not tools_root.exists():
        return {}
    if tools_root.is_symlink() or not tools_root.is_dir():
        raise ValueError("Agent tools must be a regular directory")
    tools: dict[str, ToolContribution] = {}
    for child in sorted(tools_root.iterdir()):
        if child.name == "__pycache__":
            continue
        if child.is_symlink() or not child.is_dir():
            raise ValueError(f"invalid bundled Tool directory: {child}")
        qualified_id = f"{manifest.spec.id}/{child.name}"
        try:
            contribution = DirectoryToolLoader().load(
                child,
                context=ToolLoadContext(workspace_root=workspace_root, services=services or {}),
                owner_agent_id=manifest.spec.id,
                data_root=workspace_root / "agent-data" / manifest.spec.id / child.name,
            )
        except Exception as exc:
            raise ValueError(f"invalid bundled Tool {qualified_id}: {exc}") from exc
        tools[qualified_id] = replace(
            contribution,
            spec=replace(contribution.spec, metadata={
                **dict(contribution.spec.metadata),
                "qualified_id": qualified_id,
                "owner_agent_id": manifest.spec.id,
                "origin": "standalone",
                "plugin_id": None,
            }),
        )
    return tools
