from __future__ import annotations

from typing import Mapping, Sequence

from .security_runtime import SecurityRuntimeClient


class SecurityAgentRegistration:
    """Small lifecycle facade for logical Agent registration/capability sync."""

    def __init__(self, client: SecurityRuntimeClient | None = None):
        self.client = client or SecurityRuntimeClient()

    async def register(self, agent_id: str, metadata: Mapping[str, object] | None = None):
        return await self.client._call("register_agent", str(agent_id), dict(metadata or {}))

    async def initialize_builtin_yolo(self, agent_id: str):
        """Apply the built-in default only when Security records no explicit choice.

        Only the trusted system-starter lifecycle calls this; Agent metadata
        and ordinary declarations cannot request security mode changes.
        """
        mode = await self.client._call("get_agent_security_mode", str(agent_id))
        if not isinstance(mode, Mapping):
            return None
        if (mode.get("mode") == "normal" and mode.get("updated_at") == ""
                and mode.get("updated_by") == ""):
            return await self.client._call("set_agent_security_mode", str(agent_id), "yolo")
        return mode

    async def unregister(self, agent_id: str):
        return await self.client._call("unregister_agent", str(agent_id))

    async def ensure_capabilities(self, agent_id: str,
                                  capabilities: Sequence[Mapping[str, object]]):
        """Register tool rows that securityd does not already know about.

        The tool-policy table is Operator-owned: once a policy exists, the
        console and the securityd WebUI are the only places that change it.
        Registration therefore only *adds* what is missing — an Agent
        declaration must never overwrite a tightening an operator made after
        the fact.
        """
        existing = await self.client._call("get_capabilities", str(agent_id))
        if not isinstance(existing, Sequence) or isinstance(existing, (str, bytes, bytearray)):
            existing = []
        known = {
            str(row.get("tool") or row.get("capability") or "")
            for row in existing if isinstance(row, Mapping)
        }
        rows = [dict(x) for x in capabilities if str(x.get("tool", "")) not in known]
        if not rows:
            return {"agent_id": str(agent_id), "count": 0, "status": "unchanged"}
        result = await self.client._call("register_capabilities", str(agent_id), rows)
        if result is None:
            return None
        return {"agent_id": str(agent_id), "count": len(rows), "status": "registered"}


_VALID_POLICIES = ("allow", "ask", "deny")
_VALID_FRAMEWORKS = ("runtime", "hermes")


def agent_framework(spec) -> str:
    """Infer which framework's tool catalog an Agent is wired to.

    Runtime Agents see runtime tools and hermes Agents see hermes tools; the
    same name (``read_file``) exists in both catalogs, so this is the only
    thing that keeps the console from showing the wrong list. Declared
    frameworks win; otherwise ``com.hermes*`` ids are hermes Agents and
    everything else is a runtime Agent.
    """
    framework = str(getattr(spec, "framework", "") or "").strip().lower()
    if framework in _VALID_FRAMEWORKS:
        return framework
    agent_id = str(getattr(spec, "id", "") or "")
    return "hermes" if agent_id.startswith("com.hermes") else "runtime"


def _policy_of(item: Mapping[str, object], default: str = "allow") -> str:
    raw = item.get("policy")
    if raw is None:
        raw = item.get("default_policy")
    if raw is None:
        raw = default
    value = str(raw).strip().lower()
    return value if value in _VALID_POLICIES else default


def logical_agent_capabilities(spec) -> list[dict[str, object]]:
    """Translate an Agent declaration into securityd tool-policy rows.

    Capability management has exactly one dimension: the tool. Every declared
    tool maps to one ``allow / ask / deny`` policy, and nothing else. The
    selected tools and MCP servers are also represented so an Agent created
    through the API has a complete, replaceable security declaration even when
    it does not carry a hand-written ``permissions`` block; MCP servers use the
    same ``mcp.<server>`` key the runtime gates them by.

    The framework is inferred from the Agent (see :func:`agent_framework`) so
    the console shows runtime tools for runtime Agents and hermes tools for
    hermes Agents.
    """
    framework = agent_framework(spec)

    rows: list[dict[str, object]] = []

    def add(tool: str, policy: str) -> None:
        name = str(tool).strip()
        if not name or name == "*":
            return
        rows.append({"tool": name, "policy": policy, "framework": framework})

    permissions = getattr(spec, "permissions", {})
    declared = permissions.get("capabilities", []) if isinstance(permissions, Mapping) else []
    if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes, bytearray)):
        for item in declared:
            if not isinstance(item, Mapping):
                continue
            tool = str(item.get("tool", item.get("name", item.get("capability", "")))).strip()
            if tool:
                add(tool, _policy_of(item))

    for value in getattr(spec, "tools", ()) or ():
        add(str(value), "allow")

    for value in getattr(spec, "mcps", ()) or ():
        server = str(value).strip()
        if server:
            add(f"mcp.{server}", "allow")

    unique: dict[str, dict[str, object]] = {}
    for row in rows:
        # The explicit permissions declaration wins over the selected-resource
        # default, so an operator's chosen policy is never overwritten.
        unique.setdefault(str(row["tool"]), row)
    return list(unique.values())


__all__ = ["SecurityAgentRegistration", "agent_framework",
           "logical_agent_capabilities"]
