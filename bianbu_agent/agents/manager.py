from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from bianbu_agent.agents import AgentDeclaration, AgentModel, AgentRegistry, AgentSpec
from bianbu_agent.agents.presets import with_presets
from bianbu_agent.config import RuntimePresetsConfig
from bianbu_agent.errors import AgentValidationError, ModelResolutionError
from bianbu_agent.mcp import resolve_mcp_selectors
from bianbu_agent.models import ModelRegistry


@dataclass(frozen=True, slots=True)
class AgentToolInfo:
    name: str
    description: str
    input_schema: Mapping[str, object]
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class AgentSkillInfo:
    name: str
    display_name: str
    description: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class AgentManager:
    def __init__(
        self,
        *,
        store: AgentRegistry,
        model_registry: ModelRegistry,
        generation_provider: Callable[[], object],
        presets: RuntimePresetsConfig | None = None,
    ) -> None:
        self._store = store
        self._models = model_registry
        self._generation = generation_provider
        # Presets come from ``config.yaml``; an unset section simply means this
        # device declares no always-on capabilities.
        self._presets = presets if presets is not None else RuntimePresetsConfig()

    async def create(
        self,
        spec: AgentSpec,
        *,
        generation: object | None = None,
        allow_model_repair: bool = False,
        allow_capability_gap: bool = False,
    ) -> AgentSpec:
        self.validate(
            spec,
            generation=generation,
            allow_model_repair=allow_model_repair,
            allow_capability_gap=allow_capability_gap,
        )
        return await self._store.create(spec)

    async def get(self, agent_id: str) -> AgentSpec:
        normalized = str(agent_id).strip()
        if not normalized:
            raise ValueError("agent_id must not be empty")
        found = await self._store.get(normalized)
        if found is None:
            raise KeyError(normalized)
        return found

    async def list(self) -> list[AgentSpec]:
        stored = await self._store.list()
        return sorted(stored, key=lambda item: (item.id.casefold(), item.id))

    async def update(
        self,
        spec: AgentSpec,
        *,
        generation: object | None = None,
        allow_model_repair: bool = False,
        allow_capability_gap: bool = False,
    ) -> AgentSpec:
        self.validate(
            spec,
            generation=generation,
            allow_model_repair=allow_model_repair,
            allow_capability_gap=allow_capability_gap,
        )
        return await self._store.update(spec)

    async def delete(self, agent_id: str) -> bool:
        return await self._store.delete(agent_id)

    async def materialize(
        self,
        agent_id: str,
        generation: object,
        *,
        allow_model_repair: bool = False,
        allow_capability_gap: bool = False,
    ) -> AgentSpec:
        """Resolve one declaration against the exact leased Generation."""

        spec = await self.get(agent_id)
        self.validate(
            spec,
            generation=generation,
            allow_model_repair=allow_model_repair,
            allow_capability_gap=allow_capability_gap,
        )
        plugins = tuple(dict.fromkeys(
            (*_materialize(spec.plugins, _plugin_catalog(generation)),
             *_plugin_owners(spec, generation))
        ))
        mcps = _select_members(
            resolve_mcp_selectors(spec.mcps, generation.mcps),
            generation.mcps,
            generation,
            plugins,
            mcp=True,
        )
        selected_skills = _select_members(
            spec.skills, generation.skills, generation, plugins
        )
        return replace(
            spec,
            tools=_materialize_agent_tools(
                spec, generation, mcps, plugins, self._presets.tools
            ),
            plugins=plugins,
            skills=with_presets(
                selected_skills, self._presets.skills, generation.skills
            ),
            mcps=mcps,
            hooks=_plugin_hooks(getattr(generation, "hooks", {}), plugins),
            declared=AgentDeclaration.from_spec(spec),
        )

    async def list_tools(self, agent_id: str) -> list[AgentToolInfo]:
        generation = self._generation()
        agent = await self.materialize(
            agent_id, generation, allow_capability_gap=True
        )
        tools = generation.tools
        return [
            AgentToolInfo(
                name=tools[name].spec.name,
                description=tools[name].spec.description,
                input_schema=tools[name].spec.input_schema,
                metadata=tools[name].spec.metadata,
            )
            for name in agent.tools
            if name in tools
        ]

    async def list_skills(self, agent_id: str) -> list[AgentSkillInfo]:
        generation = self._generation()
        agent = await self.materialize(
            agent_id, generation, allow_capability_gap=True
        )
        skills = generation.skills
        return [
            AgentSkillInfo(
                name=skills[name].spec.name,
                display_name=skills[name].spec.display_name,
                description=skills[name].spec.description,
                metadata=skills[name].spec.metadata,
            )
            for name in agent.skills
            if name in skills
        ]

    def validate(
        self,
        spec: AgentSpec,
        *,
        generation: object | None = None,
        allow_model_repair: bool = False,
        allow_capability_gap: bool = False,
    ) -> None:
        """Validate one declaration against the current immutable generation.

        ``allow_capability_gap`` keeps a declared-but-uninstalled Skill, MCP
        server or Plugin from blocking the Agent.  An expert installed from
        AgentHub declares Powers the device does not have yet; it has to run
        with a reported gap so it can provision them through APM.  Tool
        selectors stay strict because they are the boundary of what the model
        may call at all.
        """
        if (
            not isinstance(spec.model, AgentModel)
            and not self._models.has_default_model
            and not allow_model_repair
        ):
            raise AgentValidationError("Agent model is required: agent_model_required")
        if spec.model:
            try:
                self._models.resolve(spec.model)
            except ModelResolutionError as exc:
                raise AgentValidationError(f"Agent model is unavailable: {spec.model}") from exc
        selected_generation = self._generation() if generation is None else generation
        _validate_selector(spec.tools, "tools")
        _validate_selector(spec.skills, "skills")
        _validate_selector(spec.mcps, "mcps")
        _validate_selector(spec.plugins, "plugins")
        ordinary_tools = {
            name: contribution
            for name, contribution in selected_generation.tools.items()
            if not is_mcp_tool(contribution)
        }
        mcp_tool_names = {
            name
            for name, contribution in selected_generation.tools.items()
            if is_mcp_tool(contribution)
        }
        selected_mcp_tools = tuple(
            name for name in spec.tools if name != "*" and name in mcp_tool_names
        )
        if selected_mcp_tools:
            raise AgentValidationError(
                "Agent tools must name built-in tools; select the MCP server "
                "that provides them instead: " + ", ".join(selected_mcp_tools)
            )
        missing_tools = _missing(spec.tools, ordinary_tools)
        missing_skills = _missing(spec.skills, selected_generation.skills)
        installed_mcps = dict(selected_generation.mcps)
        catalog = getattr(selected_generation, "capability_catalog", None)
        if catalog is not None:
            installed_mcps.update({source.capability_id: source
                                   for source in catalog.mcp_sources.values()})
        # A connection failure is a runtime state of an installed MCP, not a
        # missing expert reference. Healthy selected servers must still work.
        # The declaration may use the APM name (``nws``) of an MCP this device
        # installed as ``mcp.nws``, so resolve before comparing.
        missing_mcps = _missing(
            resolve_mcp_selectors(spec.mcps, installed_mcps), installed_mcps
        )
        missing_plugins = _missing(spec.plugins, _plugin_catalog(selected_generation))
        details = []
        if missing_plugins:
            details.append("missing plugins: " + ", ".join(missing_plugins))
        if missing_tools:
            details.append("missing tools: " + ", ".join(missing_tools))
        if missing_skills:
            details.append("missing skills: " + ", ".join(missing_skills))
        if missing_mcps:
            details.append("missing MCP servers: " + ", ".join(missing_mcps))
        if missing_tools:
            raise AgentValidationError("; ".join(details))
        if details and not allow_capability_gap:
            raise AgentValidationError("; ".join(details))


def _plugin_catalog(generation: object) -> Mapping[str, object]:
    catalog = getattr(generation, "capability_catalog", None)
    if catalog is not None:
        return {source.capability_id: source for source in catalog.plugin_sources.values()}
    return getattr(generation, "plugins", {})


def _select_members(values, available, generation, plugins, *, mcp=False):
    installed = _plugin_catalog(generation)
    selected_plugins = set(plugins)
    result = []
    for name, contribution in available.items():
        owner = getattr(contribution, "owner_plugin_id" if mcp else "plugin_id", None)
        if owner in installed:
            enabled = owner in selected_plugins
        else:
            enabled = values == ("*",) or name in values
        if enabled:
            result.append(name)
    return tuple(sorted(result))


def _plugin_hooks(available: Mapping[str, object], plugins) -> tuple[str, ...]:
    """Hooks belong to Plugins: selecting a Plugin activates its Hooks.

    An Agent that declares no Plugin selection keeps no Hook selection either,
    exactly like its other Plugin-owned members.
    """

    selected = set(plugins)
    return tuple(
        sorted(
            name
            for name, contribution in available.items()
            if getattr(contribution, "plugin_id", None) in selected
        )
    )


def _validate_selector(values: tuple[str, ...], kind: str) -> None:
    if "*" in values and values != ("*",):
        raise AgentValidationError(
            f"Agent {kind} wildcard must be the only selector"
        )


def _missing(
    values: tuple[str, ...],
    available: Mapping[str, object],
) -> list[str]:
    if values == ("*",):
        return []
    return sorted(set(values) - available.keys())


_MEMBER_OWNER_FIELDS = (
    # (declaration field, Generation catalog, owner attribute)
    ("tools", "tools", "plugin_id"),
    ("skills", "skills", "plugin_id"),
    ("mcps", "mcps", "owner_plugin_id"),
)


def _plugin_owners(spec: AgentSpec, generation: object) -> tuple[str, ...]:
    """Return the Plugins that own the members this declaration names.

    ``_select_members`` exposes a Plugin-owned Tool, Skill or MCP server only
    while its Plugin is selected.  An expert that named the member and not the
    Plugin therefore lost the capability *silently*: the entry was neither
    exposed nor reported as missing, so the Skill it declares looked like it was
    never recognized.  Naming a member is an explicit request for that
    capability, so materialize the owning Plugin instead of discarding it.
    Plugins this device does not have stay declared gaps, like any other.
    """

    installed = _plugin_catalog(generation)
    if not installed:
        return ()
    owners: list[str] = []
    for field_name, catalog_name, owner_attribute in _MEMBER_OWNER_FIELDS:
        selectors = getattr(spec, field_name, ())
        if not selectors or selectors == ("*",):
            continue
        available = getattr(generation, catalog_name, None)
        if not isinstance(available, Mapping):
            continue
        if field_name == "mcps":
            # The declaration may use the APM name (``nws``) of a server this
            # device installed as ``mcp.nws``; the catalog is keyed by the
            # installed ID.
            selectors = resolve_mcp_selectors(selectors, available)
        for name in selectors:
            owner = getattr(available.get(name), owner_attribute, None)
            if isinstance(owner, str) and owner in installed:
                owners.append(owner)
    return tuple(dict.fromkeys(owners))


def _materialize(
    values: tuple[str, ...],
    available: Mapping[str, object],
) -> tuple[str, ...]:
    if values == ("*",):
        return tuple(sorted(available))
    return values


def is_mcp_tool(contribution: object) -> bool:
    """Report whether a Tool contribution comes from an MCP server."""

    spec = getattr(contribution, "spec", None)
    metadata = getattr(spec, "metadata", {})
    return metadata.get("origin") == "mcp"


def _materialize_agent_tools(
    spec: AgentSpec,
    generation: object,
    mcps: tuple[str, ...],
    plugins: tuple[str, ...],
    preset_tools: tuple[str, ...] = (),
) -> tuple[str, ...]:
    ordinary = {
        name: contribution
        for name, contribution in generation.tools.items()
        if not is_mcp_tool(contribution)
        and contribution.spec.metadata.get("owner_agent_id", spec.id) == spec.id
    }
    selected = list(
        with_presets(
            _select_members(spec.tools, ordinary, generation, plugins),
            preset_tools,
            ordinary,
        )
    )
    for mcp_id in mcps:
        selected.extend(generation.mcp_tools.get(mcp_id, ()))
    return tuple(dict.fromkeys(selected))


__all__ = ["AgentManager", "AgentSkillInfo", "AgentToolInfo", "is_mcp_tool"]
