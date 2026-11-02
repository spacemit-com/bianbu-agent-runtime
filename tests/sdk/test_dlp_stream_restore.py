from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest

from bianbu_agent import (
    AgentSpec,
    ModelRegistry,
    RunEvent,
    RunManager,
)
from bianbu_agent.plugins import (
    ContextPatch,
    HookContribution,
    HookPoint,
    PluginContributions,
    PluginGeneration,
    PluginRuntime,
)
from bianbu_agent.runs.stores.sqlite import SQLiteRunStore
from bianbu_agent.runs.stream_restore import StandInStreamRestorer

STAND_IN = "13879455773"
REAL = "13812345678"


class Agents:
    async def materialize(self, agent_id, generation, **kwargs):
        del generation, kwargs
        return AgentSpec(
            id=agent_id or "default",
            name="Agent",
            model="fast",
            hooks=("bianbu.security/security.after-context",),
        )


class Plugins:
    def __init__(self, generation):
        self.generation = generation

    @asynccontextmanager
    async def lease(self):
        yield self.generation


@dataclass
class Model:
    model: str


class SplitEngine:
    """Emit a stand-in one character at a time, as a provider may."""

    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        for character in f"号码 {STAND_IN}":
            yield RunEvent("token", {"text": character})
        yield RunEvent("done", {})


class AfterModelEngine:
    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        yield RunEvent("token", {"text": f"号码 {STAND_IN}"})
        yield RunEvent("done", {})


class StopEngine:
    async def stream(self, prompt, **kwargs):
        del prompt, kwargs
        yield RunEvent("token", {"text": "号码 1387"})
        raise RuntimeError("upstream reset")


def _generation(patch):
    async def hook(_context):
        return patch

    return PluginGeneration(
        id=1,
        plugins={
            "bianbu.security": PluginRuntime(
                plugin_id="bianbu.security",
                instance=object(),
                config={},
                contributions=PluginContributions(
                    hooks=(
                        HookContribution(
                            "bianbu.security",
                            HookPoint.AFTER_CONTEXT,
                            hook,
                            id="security.after-context",
                            metadata={
                                "qualified_id": (
                                    "bianbu.security/security.after-context"
                                ),
                            },
                        ),
                    ),
                ),
            ),
        },
        statuses={},
    )


def _manager(tmp_path, *, engine, patch):
    store = SQLiteRunStore(tmp_path / "runs.sqlite3")
    return store, RunManager(
        plugin_manager=Plugins(_generation(patch)),
        agent_manager=Agents(),
        model_registry=ModelRegistry(default_model=Model("fast"),
                                     models={"fast": Model("fast")}),
        engine_builder=lambda *_args: engine,
        store=store,
    )


def test_restorer_handles_every_split_without_cascade():
    mapping = {STAND_IN: REAL, REAL: "13800000000"}
    original = f"联系 {STAND_IN}，备用 {REAL}。"
    for cut in range(len(original) + 1):
        restorer = StandInStreamRestorer()
        restorer.update(mapping)
        output = restorer.feed(original[:cut]) + restorer.feed(original[cut:])
        output += restorer.flush()
        assert output == f"联系 {REAL}，备用 13800000000。"


@pytest.mark.asyncio
async def test_stream_tokens_are_restored_before_publication(tmp_path):
    store, manager = _manager(
        tmp_path,
        engine=SplitEngine(),
        patch=ContextPatch(stream_stand_ins={STAND_IN: REAL}),
    )
    await store.open()
    try:
        handle = await manager.run("stream-restore", "phone?")
        tokens = []
        async for event in handle:
            if event.kind == "token":
                tokens.append(event.payload["text"])
        result = await handle.wait()
    finally:
        await store.close()
    assert "".join(tokens) == f"号码 {REAL}"
    assert result.content == f"号码 {REAL}"
    assert STAND_IN not in "".join(tokens)


@pytest.mark.asyncio
async def test_after_model_mapping_is_applied_and_terminal_text_matches(tmp_path):
    store, manager = _manager(
        tmp_path,
        engine=AfterModelEngine(),
        patch=ContextPatch(stream_stand_ins={STAND_IN: REAL}),
    )
    await store.open()
    try:
        handle = await manager.run("after-model-restore", "phone?")
        async for _event in handle:
            pass
        result = await handle.wait()
    finally:
        await store.close()
    assert result.content == f"号码 {REAL}"


@pytest.mark.asyncio
async def test_failed_run_releases_a_partial_stand_in(tmp_path):
    store, manager = _manager(
        tmp_path,
        engine=StopEngine(),
        patch=ContextPatch(stream_stand_ins={STAND_IN: REAL}),
    )
    await store.open()
    try:
        handle = await manager.run("failed-partial", "phone?")
        tokens = []
        async for event in handle:
            if event.kind == "token":
                tokens.append(event.payload["text"])
        result = await handle.wait()
    finally:
        await store.close()
    # The provider failed, but text already published must not swallow the
    # user's request: a held-back suffix is released as well, even though it
    # still looks like the start of a stand-in.
    assert "".join(tokens) == "号码 1387"
    assert result.status == "failed"
