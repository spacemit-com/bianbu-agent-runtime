from __future__ import annotations

from typing import Mapping, Sequence

from .capability_declarations import DeclarationBaseline, plan_replay
from .security_runtime import SecurityRuntimeClient


class SecurityAgentRegistration:
    """Small lifecycle facade for logical Agent registration/capability sync."""

    def __init__(self, client: SecurityRuntimeClient | None = None):
        self.client = client or SecurityRuntimeClient()

    async def register(self, agent_id: str, metadata: Mapping[str, object] | None = None):
        return await self.client._call("register_agent", str(agent_id), dict(metadata or {}))

    async def initialize_builtin_yolo(self, agent_id: str, *, force: bool = False):
        """Apply the built-in ``yolo`` default to a packaged system Agent.

        Without ``force`` this only replaces the untouched default an older
        Security release wrote (``safe`` with no ``updated_at``/``updated_by``).
        The first install passes ``force`` so the shipped Agent starts from the
        product default ``yolo`` -> ``host``.  Reinstalls and upgrades never
        pass ``force``.  Only the trusted package/startup lifecycle calls this;
        Agent metadata and ordinary declarations cannot change a security mode.
        """
        mode = await self.client._call("get_agent_security_mode", str(agent_id))
        if not isinstance(mode, Mapping):
            return None
        if force:
            if mode.get("mode") == "yolo":
                return mode
            return await self.client._call("set_agent_security_mode", str(agent_id), "yolo")
        if (mode.get("mode") == "safe" and mode.get("updated_at") == ""
                and mode.get("updated_by") == ""):
            return await self.client._call("set_agent_security_mode", str(agent_id), "yolo")
        return mode

    async def unregister(self, agent_id: str):
        return await self.client._call("unregister_agent", str(agent_id))

    async def ensure_capabilities(self, agent_id: str,
                                  capabilities: Sequence[Mapping[str, object]],
                                  *,
                                  baseline: DeclarationBaseline | None = None):
        """Replay this Agent's declared tool policies over securityd.

        The tool-policy table is shared with the security console: the Runtime
        writes the rows it declares, the console writes the operator's edits.
        An upgrade has to make a new declaration effective, and it must never
        roll back a tightening an operator made in the console.  The baseline
        is what tells the two apart — a stored row that no longer matches the
        value this Runtime declared last time belongs to the operator and is
        left alone (``capability_declarations.plan_replay``).
        """
        declared_rows = [dict(row) for row in capabilities
                         if str(row.get("tool", "")).strip()]
        declared = {str(row["tool"]): str(row.get("policy", "allow"))
                    for row in declared_rows}
        existing = await self.client._call("get_capabilities", str(agent_id))
        current: dict[str, str] = {}
        if isinstance(existing, Sequence) and not isinstance(existing, (str, bytes, bytearray)):
            for item in existing:
                if not isinstance(item, Mapping):
                    continue
                tool = str(item.get("tool") or item.get("capability") or "")
                if tool:
                    current[tool] = str(item.get("policy", "allow"))
        previous = baseline.declared_for(agent_id) if baseline is not None else None
        plan = plan_replay(declared=declared, current=current, previous=previous)
        if plan.writes:
            payload = [row for row in declared_rows
                       if str(row["tool"]) in plan.writes]
            result = await self.client._call("register_capabilities",
                                             str(agent_id), payload)
            if result is None:
                return None
        if baseline is not None:
            baseline.record(agent_id, declared)
        return {"agent_id": str(agent_id), "count": len(plan.writes),
                "status": "replayed" if plan.writes else "unchanged",
                "preserved": sorted(plan.preserved)}


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
