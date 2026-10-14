from __future__ import annotations

import argparse

from bianbu_agent.agents import (
    AgentModel,
    AgentPackageManager,
    AgentSpec,
    ManifestAgentStore,
)
from bianbu_agent.cli.output import emit, plain
from bianbu_agent.provisioning import new_agent_id


# The declared Agent contract.  ``hooks`` is deliberately absent: an Agent
# declares Plugins and the leased Generation resolves the Hooks they own, so the
# stored declaration never carries a Hook selection of its own.
_AGENT_OUTPUT_FIELDS = (
    "id",
    "name",
    "name_zh",
    "name_en",
    "description",
    "description_zh",
    "soul",
    "model",
    "tools",
    "skills",
    "mcps",
    "plugins",
    "permissions",
    "metadata",
)


def _agent_output(value: object) -> object:
    if isinstance(value, AgentSpec):
        return {
            name: plain(getattr(value, name))
            for name in _AGENT_OUTPUT_FIELDS
        }
    if isinstance(value, list):
        return [_agent_output(item) for item in value]
    return value


async def _resolve_agent_id(store: ManifestAgentStore, value: str) -> str:
    """Accept the generated Agent ID or the name ``agent create`` was given.

    ``create`` takes a display name and generates the ID, so reporting an
    Agent must not require the caller to have kept the generated value.  An
    exact ID always wins; a display name only resolves while it is unique.
    """

    if await store.get(value) is not None:
        return value
    matches = sorted(
        spec.id
        for spec in await store.list()
        if spec.name == value or value in {
            spec.name_zh, spec.name_en,
        } - {""}
    )
    if len(matches) > 1:
        raise ValueError(
            f"Agent name {value} is ambiguous: {', '.join(matches)}"
        )
    return matches[0] if matches else value


async def agent_command(
    store: ManifestAgentStore,
    manager: AgentPackageManager,
    args: argparse.Namespace,
) -> int:
    if args.action == "list":
        value = await store.list()
    elif args.action == "create":
        value = await store.create(
            AgentSpec(
                id=new_agent_id(),
                name=args.name,
                model=AgentModel(provider=args.provider, name=args.model),
                tools=tuple(args.tool),
                skills=tuple(args.skill),
                mcps=tuple(args.mcp),
                plugins=tuple(args.plugin),
            )
        )
    else:
        agent_id = await _resolve_agent_id(store, args.agent_id)
        if args.action == "show":
            value = await store.get(agent_id)
        elif args.action == "verify":
            value = {"valid": manager.verify(agent_id)}
        else:
            value = {"deleted": await store.delete(agent_id)}
    emit(_agent_output(value))
    return 0
