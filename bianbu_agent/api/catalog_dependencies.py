"""Cross-catalog Agent reference checks for destructive mutations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from fastapi import HTTPException

from bianbu_agent.errors import AgentReference


_SELECTOR_FIELDS = {
    "tool": "tools",
    "skill": "skills",
    "mcp": "mcps",
    "plugin": "plugins",
}

# Kinds differ in how the user gets rid of the selection, so the guidance has to
# name the right page and the right resource noun.
_IN_USE_HELP = {
    "plugin": ("插件", "请先在“专家”页给这些专家换用其它插件或删除这些专家，再删除该插件。"),
    "mcp": ("MCP Server", "请先在“专家”页给这些专家换用其它 MCP Server 或删除这些专家，再删除该 MCP Server。"),
    "tool": ("工具", "请先在“专家”页给这些专家换用其它工具或删除这些专家，再删除该工具。"),
    "skill": ("技能", "请先在“专家”页给这些专家换用其它技能或删除这些专家，再删除该技能。"),
}


async def agent_references(
    runtime: object,
    selections: Mapping[str, Iterable[str]],
) -> tuple[AgentReference, ...]:
    """Return Agents explicitly selecting any named capability."""

    normalized = {
        kind: {str(item) for item in identifiers}
        for kind, identifiers in selections.items()
        if kind in _SELECTOR_FIELDS
    }
    references: set[AgentReference] = set()
    manager = getattr(runtime, "agents", None)
    if manager is None:
        return ()
    agents = await manager.list()
    for agent in agents:
        for kind, identifiers in normalized.items():
            selected = tuple(getattr(agent, _SELECTOR_FIELDS[kind], ()) or ())
            if selected != ("*",) and identifiers.intersection(selected):
                references.add(
                    AgentReference(
                        id=str(agent.id),
                        name=str(getattr(agent, "name", "") or ""),
                        name_zh=str(getattr(agent, "name_zh", "") or ""),
                        name_en=str(getattr(agent, "name_en", "") or ""),
                    )
                )
                break
    return tuple(sorted(references, key=lambda reference: reference.id))


def capability_in_use(
    kind: str,
    capability_id: str,
    references: tuple[AgentReference, ...],
) -> HTTPException:
    """409 naming the Experts that still select the capability.

    The message used to be English and quoted the content-hash Agent ID
    (``com.bianbu.278200e1617c``) only: a Chinese console showed a refusal the
    user could neither read nor act on.  The ID stays in ``details.agent_ids``
    for clients that want it; the message names the Experts.
    """

    noun, help_text = _IN_USE_HELP.get(
        kind,
        (kind, f"请先在“专家”页给这些专家换用其它{kind}或删除这些专家，再删除它。"),
    )
    labels = "、".join(reference.label for reference in references)
    return HTTPException(
        409,
        {
            "code": f"{kind}_in_use",
            "message": (
                f"{noun} {capability_id} 正在被专家使用：{labels}。{help_text}"
            ),
            "details": {
                "kind": kind,
                "capability_id": capability_id,
                "agent_ids": [reference.id for reference in references],
            },
        },
    )


__all__ = ["agent_references", "capability_in_use"]
