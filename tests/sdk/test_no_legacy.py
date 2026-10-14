import ast
from importlib.util import find_spec
from pathlib import Path

import pytest


PACKAGE_ROOT = Path("bianbu_agent")
def _legacy_imports(root: Path) -> list[tuple[Path, str]]:
    offenders: list[tuple[Path, str]] = []
    for path in root.rglob("*.py"):
        if "_legacy" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        offenders.extend(
            (path, name)
            for name in names
            if name == "bianbu_agent._legacy"
            or name.startswith("bianbu_agent._legacy.")
        )
    return sorted(offenders)


def test_current_sdk_source_does_not_import_legacy() -> None:
    """Catch a current SDK module retaining or adding a legacy dependency."""

    assert _legacy_imports(PACKAGE_ROOT) == []


def test_sdk_uses_canonical_inventory_type_identity() -> None:
    """Catch snapshot producers and consumers using duplicate legacy classes."""

    import bianbu_agent.inventory as inventory
    import bianbu_agent.plugins.manager as plugin_manager
    import bianbu_agent.server.runtime as sdk

    names = (
        "CapabilityKind",
        "CapabilityOrigin",
        "CapabilitySource",
        "InstalledCapability",
        "InventorySnapshot",
    )
    assert all(getattr(sdk, name) is getattr(inventory, name) for name in names)
    assert plugin_manager.CapabilityKind is inventory.CapabilityKind
    assert plugin_manager.InventorySnapshot is inventory.InventorySnapshot


def test_removed_internal_modules_do_not_remain_importable() -> None:
    removed = (
        "bianbu_agent.plugin_manager",
        "bianbu_agent.plugin_specs",
        "bianbu_agent.console.files",
        "bianbu_agent.console.settings",
        "bianbu_agent.tools._branding",
        "bianbu_agent.tools.definitions",
        "bianbu_agent.tools.integrity",
    )

    assert {name: find_spec(name) for name in removed} == {
        name: None for name in removed
    }


def test_retired_compatibility_surfaces_stay_removed() -> None:
    """Retired on-disk formats and migration shims must not come back."""

    import bianbu_agent.agents.contracts as contracts
    import bianbu_agent.hooks as hooks
    import bianbu_agent.mcp.manager as mcp_manager
    import bianbu_agent.models as models
    import bianbu_agent.plugins.manager as plugin_manager
    import bianbu_agent.sessions.log_store as log_store
    from bianbu_agent.api.contracts import PackageKind
    from bianbu_agent.config import RuntimeConfig
    from bianbu_agent.home import AgentHome

    # Hooks are Plugin members only: the standalone Hook package manager, its
    # data root, its selectors and its package kind must not come back.
    assert not hasattr(hooks, "HookPackageManager")
    assert find_spec("bianbu_agent.hooks.manager") is None
    assert not hasattr(AgentHome, "hooks_dir")
    assert "hook_dirs" not in RuntimeConfig.__dataclass_fields__
    assert {kind.value for kind in PackageKind} == {"skill", "agent"}

    assert not hasattr(mcp_manager.MCPPackageManager, "migrate_legacy_plugins")
    assert not hasattr(log_store, "migrate_legacy_transcripts")
    assert not hasattr(models.ModelRegistry, "accepts_legacy_agent_model")
    assert not hasattr(plugin_manager.PluginManager, "snapshot")
    assert "session-logs" not in (
        PACKAGE_ROOT / "sessions" / "log_store.py"
    ).read_text(encoding="utf-8")
    assert "agent-json-migration" not in (
        PACKAGE_ROOT / "agents" / "stores" / "manifest.py"
    ).read_text(encoding="utf-8")
    # No read path may guess a declaration a retired Runtime published: the
    # publisher reconciles the profile directory, and a record the contract
    # rejects is retired or reported instead of being skipped.
    import bianbu_agent.agents.packages as packages
    import bianbu_agent.provisioning as provisioning

    assert find_spec("bianbu_agent.agents.system_migration") is None
    assert not hasattr(packages.AgentPackageManager, "_read_system_package")
    # A user record this contract rejects is retired, never skipped in place.
    assert not hasattr(packages.AgentPackageManager, "_read_package")
    assert hasattr(packages.AgentPackageManager, "_read_user_package")
    provisioning_source = (PACKAGE_ROOT / "provisioning.py").read_text(encoding="utf-8")
    assert "resource-layout-version" not in provisioning_source
    assert "legacy-user-agents" not in provisioning_source
    assert not hasattr(provisioning, "ensure_starter_agent")
    with pytest.raises(ValueError):
        contracts.AgentSpec(id="legacy", name="Legacy", plugins=None)

    # An Agent names built-in tools only, and the retired launch section is
    # gone together with the helper that kept reading it.
    import bianbu_agent.agents.manager as agent_manager
    import bianbu_agent.api.catalog_dependencies as catalog_dependencies
    from bianbu_agent.api.contracts import _validate_agent_metadata

    assert hasattr(agent_manager, "is_mcp_tool")
    assert not hasattr(agent_manager, "_is_mcp_tool")
    assert not hasattr(catalog_dependencies, "active_mcp_tool_ids")
    # The execution boundary is derived from the Agent's security settings, so
    # neither metadata section may carry a declared mode any more.
    for section in ("runtime", "execution"):
        with pytest.raises(ValueError, match="retired"):
            _validate_agent_metadata({section: {"requested_mode": "sandbox"}})
