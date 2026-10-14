"""Security approval identity must survive the real graph runtime boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from bianbu_agent.security.security_middleware import _runtime_context


def test_security_context_reads_each_active_graph_invocation():
    """after_model receives Runtime without config; never lose or mix Run IDs."""

    async def inspect(state: dict, runtime: Runtime) -> dict:
        await asyncio.sleep(0)
        return {"identity": _runtime_context(SimpleNamespace(runtime=runtime))}

    builder = StateGraph(dict)
    builder.add_node("inspect", inspect)
    builder.add_edge(START, "inspect")
    builder.add_edge("inspect", END)
    graph = builder.compile()

    async def invoke():
        return await asyncio.gather(
            graph.ainvoke({}, {"configurable": {
                "session_id": "session-a", "run_id": "run-a",
            }}),
            graph.ainvoke({}, {"configurable": {
                "session_id": "session-b", "run_id": "run-b",
            }}),
        )

    assert asyncio.run(invoke()) == [
        {"identity": ("session-a", "run-a")},
        {"identity": ("session-b", "run-b")},
    ]
    # A finished invocation must not lend its identity to unrelated calls.
    assert _runtime_context(SimpleNamespace(runtime=Runtime())) == ("", "")
