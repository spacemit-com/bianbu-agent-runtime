"""Read-only effective Tool, Skill, and selectable Hook catalog."""

from __future__ import annotations

import logging
from pathlib import Path
import shlex

from fastapi import APIRouter, HTTPException, Request

from bianbu_agent.api.contracts import (
    CapabilityCollectionEnvelope,
    CapabilityView,
    EffectiveCapabilityKind,
)
from bianbu_agent.inventory import CapabilityKind, CapabilityOrigin
from bianbu_agent.plugins.manifest import load_installed_plugin_manifest
from bianbu_agent.plugins import skill_unavailable_reason
from bianbu_agent.skills import DirectorySkillLoader
from bianbu_agent.mcp.config import validate_mcp_config
from bianbu_agent.mcp.lifecycle import MCP_STATE_NOT_LOADED
from bianbu_agent.tools.summaries import tool_summaries


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/capabilities")
_RESTART_REQUIRED = "Restart the session to activate this {kind}."


def control_catalog_snapshot(runtime) -> dict[str, object]:
    """Read the installed, selectable catalog without starting plugin code.

    This is the Control-plane source of truth for expert configuration.  It
    deliberately inspects manifests and package metadata only; a Worker is
    responsible for reporting whether it subsequently loaded the selection.
    """
    # Import lazily so the runtime module can build its own service graph
    # without importing the HTTP API.
    from bianbu_agent.server.runtime import _inventory_snapshot, _file_capability
    from bianbu_agent.agents import AgentPackageManager
    from bianbu_agent.home import AgentHome

    # Tools are built into the Runtime code, so they are listed here but never
    # installed as packages.
    capabilities: dict[str, list[dict[str, object]]] = {
        "tool": [], "skill": [], "hook": [],
    }
    preset_tools = set(runtime.config.presets.tools)
    preset_skills = set(runtime.config.presets.skills)
    mcps: list[str] = []
    mcp_servers: list[dict[str, object]] = []
    snapshot = _inventory_snapshot(runtime.config)
    def add_capability(item, kind: CapabilityKind, root: Path, capability_id: str, *, plugin_id: str | None = None) -> None:
        source = item.source
        display_name = capability_id
        description = str(source.metadata.get("description") or "")
        name_zh = ""
        name_en = ""
        description_zh = ""
        version = source.version
        if kind is CapabilityKind.SKILL:
            metadata, _files = DirectorySkillLoader.inspect(root)
            display_name = metadata.get("display_name", metadata["name"])
            description = metadata.get("description", "")
            name_zh = metadata.get("name_zh", "")
            name_en = metadata.get("name_en", "")
            description_zh = metadata.get("description_zh", "")
            version = metadata.get("version", version)
        capabilities[kind.value].append({
            "id": capability_id,
            "name": capability_id,
            "display_name": display_name,
            "description": description,
            "name_zh": name_zh,
            "name_en": name_en,
            "description_zh": description_zh,
            "summaries": (
                dict(tool_summaries(capability_id))
                if kind is CapabilityKind.TOOL
                else {}
            ),
            "version": version,
            "origin": (
                CapabilityOrigin.PLUGIN.value
                if plugin_id is not None
                else source.origin.value
            ),
            "plugin_id": plugin_id if plugin_id is not None else source.plugin_id,
            "enabled": item.enabled,
            # Hooks are not an Agent-level selection: an Agent activates them by
            # selecting the Plugin that provides them.
            "assignable": kind is not CapabilityKind.HOOK,
            "preset": (
                kind is CapabilityKind.SKILL
                and plugin_id is None
                and capability_id in preset_skills
            ),
            "unavailable_reason": "" if item.enabled else _RESTART_REQUIRED.format(kind=kind.value.title()),
        })

    from bianbu_agent.tools.builtins import BUILTIN_MODULES, builtin_description
    from bianbu_agent.config import RUNTIME_VERSION
    for name in BUILTIN_MODULES:
        capabilities['tool'].append({
            'id': name, 'name': name, 'display_name': name,
            'description': builtin_description(name),
            'summaries': dict(tool_summaries(name)),
            'version': RUNTIME_VERSION,
            'origin': CapabilityOrigin.STANDALONE.value, 'plugin_id': None,
            'enabled': True, 'assignable': True, 'preset': name in preset_tools,
            'unavailable_reason': '',
        })

    for item in snapshot.items:
        source = item.source
        if source.kind is CapabilityKind.TOOL and source.capability_id.casefold() in BUILTIN_MODULES:
            continue
        if source.kind in {CapabilityKind.TOOL, CapabilityKind.SKILL}:
            add_capability(item, source.kind, source.root, source.capability_id)
            continue
        if source.kind is CapabilityKind.PLUGIN:
            manifest = load_installed_plugin_manifest(source.root)
            for kind, directory, members in (
                (CapabilityKind.TOOL, "tools", manifest.contributions.tools),
                (CapabilityKind.SKILL, "skills", manifest.contributions.skills),
                (CapabilityKind.HOOK, "hooks", manifest.contributions.hooks),
            ):
                for member in members:
                    add_capability(
                        item,
                        kind,
                        source.root / directory / member,
                        f"{source.capability_id}/{member}",
                        plugin_id=source.capability_id,
                    )
            continue
        if source.kind is not CapabilityKind.MCP:
            continue
        descriptor = _read_mcp_descriptor(source.manifest_path)
        mcps.append(source.capability_id)
        mcp_servers.append({
            "id": source.capability_id,
            "display_name": descriptor["display_name"],
            "command": descriptor["command"],
            "kind": "mcp",
            # Connection health belongs to the Worker.  This state says the
            # Control catalog has not observed a Worker generation yet.
            "status": "not_loaded",
            "state": MCP_STATE_NOT_LOADED,
            "phase": "",
            "ready": False,
            "tools_count": 0,
            "transport": descriptor["transport"],
            "tools": [],
            "source_kind": "system" if source.metadata.get("system_managed") else source.origin.value,
            "owner_plugin_id": source.plugin_id,
            "removable": source.plugin_id is None and not source.metadata.get("system_managed", False),
            "health_checked_at": None,
        })
    # Worker ToolDirectoryCatalog loads builtins directly, independently of
    # the installed-capability inventory. Mirror that discovery using inspect
    # only; calling its build() here would execute Tool factories in Control.
    # Tool 只能来自内置代码或 Plugin 成员：数据目录下的 tools/ 已不再受支持。
    tool_roots: list[tuple[Path, str | None]] = []
    for package in AgentPackageManager(AgentHome(runtime.config.data_dir)).list():
        tool_roots.append((package.root / "tools", package.manifest.spec.id))
    known_tools = {item["id"] for item in capabilities["tool"]}
    for root, owner in tool_roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not (child / "tool.py").is_file():
                continue
            item = _file_capability(CapabilityKind.TOOL, child)
            identity = f"{owner}/{child.name}" if owner else item.source.capability_id
            if identity not in known_tools:
                add_capability(item, CapabilityKind.TOOL, child, identity)
                known_tools.add(identity)
    for items in capabilities.values():
        items.sort(key=lambda item: str(item["id"]))
    return {
        "generation_id": None,
        "capabilities": capabilities,
        "mcps": sorted(mcps),
        "plugins": sorted(item.source.capability_id for item in snapshot.items
                          if item.source.kind is CapabilityKind.PLUGIN and item.enabled),
        "mcp_servers": sorted(mcp_servers, key=lambda item: str(item["id"])),
    }


def _read_mcp_descriptor(path: Path) -> dict[str, str]:
    try:
        config = validate_mcp_config(path, workspace=path.parent)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid MCP manifest: {path}") from exc
    command = (
        shlex.join((config.command, *config.args))
        if config.transport == "stdio"
        else config.url
    )
    return {
        "display_name": config.display_name or path.parent.name,
        "command": command,
        "transport": config.transport,
    }


async def worker_catalog_snapshot(runtime) -> dict[str, object]:
    """Read the active catalog from the isolated Worker.

    The control plane never imports a plugin module.  A missing Worker is an
    explicit service-unavailable response rather than a misleading empty
    catalog, which lets the WebUI show the actual recovery state.
    """
    supervisor = getattr(runtime, "worker_supervisor", None)
    client = getattr(supervisor, "client", None) if supervisor is not None else None
    # Control plane uses AgentWorkerPool (one client per logical Agent), while
    # a direct Worker supervisor exposes ``client`` itself. Select any ready
    # pooled Worker for the global catalog endpoints.
    if supervisor is not None and client is None and hasattr(supervisor, "_supervisors"):
        for worker in supervisor._supervisors.values():
            if worker.ready and worker.client is not None:
                client = worker.client
                break
    if supervisor is None or not getattr(supervisor, "ready", False) or client is None:
        raise HTTPException(503, {
            "code": "worker_unavailable",
            "message": "Agent Worker is not available; capability catalog cannot be read.",
        })
    try:
        response = await client.catalog()
    except Exception as exc:  # noqa: BLE001 - RPC boundary
        # The catalog crosses a Worker RPC boundary, so an empty result and a
        # failed read look identical to the Console.  Keep the reason.
        logger.warning(
            "Worker capability catalog failed: error=%s", exc, exc_info=exc
        )
        raise HTTPException(503, {
            "code": "worker_unavailable",
            "message": (
                "Agent Worker is not available; capability catalog cannot "
                f"be read: {exc}"
            ),
        }) from exc
    raw = response.get("capabilities")
    if not isinstance(raw, dict):
        raise HTTPException(502, {
            "code": "worker_catalog_invalid",
            "message": "Agent Worker returned an invalid capability catalog.",
        })
    capabilities: dict[str, list[dict[str, object]]] = {}
    for kind in ("tool", "skill", "hook"):
        items = raw.get(kind)
        if not isinstance(items, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and bool(item["id"].strip())
            for item in items
        ):
            raise HTTPException(502, {
                "code": "worker_catalog_invalid",
                "message": "Agent Worker returned an invalid capability catalog.",
            })
        capabilities[kind] = [dict(item) for item in items]
    raw_mcps = response.get("mcps", [])
    if not isinstance(raw_mcps, list) or not all(
        isinstance(item, str) and bool(item.strip()) for item in raw_mcps
    ):
        raise HTTPException(502, {
            "code": "worker_catalog_invalid",
            "message": "Agent Worker returned an invalid MCP catalog.",
        })
    raw_mcp_servers = response.get("mcp_servers", [])
    if not isinstance(raw_mcp_servers, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(item["id"].strip())
        for item in raw_mcp_servers
    ):
        raise HTTPException(502, {
            "code": "worker_catalog_invalid",
            "message": "Agent Worker returned invalid MCP server descriptors.",
        })
    raw_mcp_statuses = response.get("mcp_statuses", {})
    if not isinstance(raw_mcp_statuses, dict):
        raise HTTPException(502, {
            "code": "worker_catalog_invalid",
            "message": "Agent Worker returned invalid MCP load statuses.",
        })
    return {
        "generation_id": response.get("generation_id"),
        "capabilities": capabilities,
        "mcps": list(raw_mcps),
        "mcp_servers": [dict(item) for item in raw_mcp_servers],
        "mcp_statuses": {
            str(key): dict(value)
            for key, value in raw_mcp_statuses.items()
            if isinstance(key, str) and isinstance(value, dict)
        },
    }


async def _worker_catalog(runtime) -> dict[str, list[dict[str, object]]]:
    snapshot = await worker_catalog_snapshot(runtime)
    return snapshot["capabilities"]


def _tool_views(runtime) -> list[CapabilityView]:
    generation = runtime.plugin_runtime.current_generation
    catalog = generation.capability_catalog
    catalog_tools = {} if catalog is None else catalog.tools
    preset_tools = set(runtime.config.presets.tools)
    items: list[CapabilityView] = []
    for capability_id, contribution in catalog_tools.items():
        metadata = contribution.spec.metadata
        if metadata.get("origin") == "mcp":
            continue
        source = catalog.sources[(CapabilityKind.TOOL, capability_id)]
        unavailable_reason = str(metadata.get("unavailable_reason") or "").strip()
        items.append(
            CapabilityView(
                id=capability_id,
                name=capability_id,
                display_name=contribution.spec.name,
                description=contribution.spec.description,
                summaries=dict(tool_summaries(capability_id)),
                version=source.version,
                origin=source.origin.value,
                plugin_id=source.plugin_id,
                enabled=not unavailable_reason,
                preset=capability_id in preset_tools,
                unavailable_reason=unavailable_reason,
            )
        )
    active_ids = set(catalog_tools)
    for capability_id, contribution in generation.tools.items():
        if capability_id in active_ids:
            continue
        metadata = contribution.spec.metadata
        if metadata.get("origin") == "mcp":
            continue
        items.append(
            CapabilityView(
                id=capability_id,
                name=capability_id,
                display_name=str(metadata.get("display_name") or contribution.spec.name),
                description=contribution.spec.description,
                summaries=dict(tool_summaries(capability_id)),
                version=str(metadata.get("version") or "0.0.0"),
                origin=str(metadata.get("origin") or "plugin"),
                plugin_id=contribution.plugin_id,
                enabled=True,
                preset=capability_id in preset_tools,
                unavailable_reason="",
            )
        )
        active_ids.add(capability_id)
    return sorted(items, key=lambda item: item.id)


def _skill_views(runtime) -> list[CapabilityView]:
    generation = runtime.plugin_runtime.current_generation
    catalog = generation.capability_catalog
    catalog_skills = {} if catalog is None else catalog.skills
    preset_skills = set(runtime.config.presets.skills)
    items: list[CapabilityView] = []
    for capability_id, contribution in catalog_skills.items():
        source = catalog.sources[(CapabilityKind.SKILL, capability_id)]
        unavailable_reason = skill_unavailable_reason(contribution)
        items.append(
            CapabilityView(
                id=capability_id,
                name=capability_id,
                display_name=contribution.spec.display_name,
                description=contribution.spec.description,
                name_zh=contribution.spec.name_zh,
                name_en=contribution.spec.name_en,
                description_zh=contribution.spec.description_zh,
                version=source.version,
                origin=source.origin.value,
                plugin_id=source.plugin_id,
                enabled=not unavailable_reason,
                preset=capability_id in preset_skills,
                unavailable_reason=unavailable_reason,
            )
        )
    active_ids = set(catalog_skills)
    for capability_id, contribution in generation.skills.items():
        if capability_id in active_ids:
            continue
        metadata = contribution.spec.metadata
        unavailable_reason = skill_unavailable_reason(contribution)
        items.append(
            CapabilityView(
                id=capability_id,
                name=capability_id,
                display_name=contribution.spec.display_name,
                description=contribution.spec.description,
                name_zh=contribution.spec.name_zh,
                name_en=contribution.spec.name_en,
                description_zh=contribution.spec.description_zh,
                version=contribution.spec.version,
                origin=str(metadata.get("origin") or "plugin"),
                plugin_id=contribution.plugin_id,
                enabled=not unavailable_reason,
                preset=capability_id in preset_skills,
                unavailable_reason=unavailable_reason,
            )
        )
        active_ids.add(capability_id)
    for package in runtime.skills.list():
        if package.capability_id in active_ids:
            continue
        items.append(
            CapabilityView(
                id=package.capability_id,
                name=package.capability_id,
                display_name=package.capability_id,
                description="",
                version=package.version,
                origin="standalone",
                plugin_id=None,
                enabled=False,
                unavailable_reason=_RESTART_REQUIRED.format(kind="Skill"),
            )
        )
    return sorted(items, key=lambda item: item.id)


def _hook_views(runtime) -> list[CapabilityView]:
    generation = runtime.plugin_runtime.current_generation
    items: list[CapabilityView] = []
    for capability_id, contribution in generation.hooks.items():
        metadata = contribution.metadata
        unavailable_reason = str(metadata.get("unavailable_reason") or "").strip()
        items.append(
            CapabilityView(
                id=capability_id,
                name=capability_id,
                display_name=str(metadata.get("display_name") or capability_id),
                description=str(metadata.get("description") or ""),
                version=str(metadata.get("version") or "0.0.0"),
                origin=str(metadata.get("origin") or "plugin"),
                plugin_id=contribution.plugin_id,
                enabled=not unavailable_reason,
                # Hooks are not an Agent-level selection: an Agent activates
                # them by selecting the Plugin that provides them.
                assignable=False,
                unavailable_reason=unavailable_reason,
            )
        )
    return sorted(items, key=lambda item: item.id)


@router.get("/{kind}", response_model=CapabilityCollectionEnvelope)
async def list_capabilities(
    kind: EffectiveCapabilityKind,
    request: Request,
) -> CapabilityCollectionEnvelope:
    """Project the immutable active generation plus pending standalone packages."""

    runtime = request.app.state.runtime
    if getattr(runtime, "role", "worker") == "control":
        catalog = control_catalog_snapshot(runtime)["capabilities"]
        items = [CapabilityView.model_validate(item) for item in catalog[kind]]
        return CapabilityCollectionEnvelope(data=sorted(items, key=lambda item: item.id))
    items = {
        "tool": _tool_views,
        "skill": _skill_views,
        "hook": _hook_views,
    }[kind](runtime)
    return CapabilityCollectionEnvelope(data=items)


__all__ = ["control_catalog_snapshot", "router", "worker_catalog_snapshot"]
