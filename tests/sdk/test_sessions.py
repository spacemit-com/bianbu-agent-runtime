from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bianbu_agent import SessionManager
from bianbu_agent.sessions import SessionAgentMismatchError
from bianbu_agent.sessions.stores.sqlite import SQLiteSessionStore


class Plugins:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    async def reload_installed(self, *, force: bool = False, handoff: bool = False):
        del force, handoff
        self.calls += 1
        if self.fail:
            raise RuntimeError("invalid candidate")
        return {"generation_id": self.calls + 1}


@pytest.mark.asyncio
async def test_sqlite_sessions_persist_and_rotate_internal_thread_with_cas(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sessions.sqlite3"
    store = SQLiteSessionStore(path)
    await store.open()
    created = await store.create(
        "stable", agent_id="writer", title="Stable", metadata={"x": 1}
    )
    assert created.agent_id == "writer"
    thread = created.active_thread_id
    assert await store.create("stable", agent_id="writer", title="ignored") == created
    assert await store.rotate_thread("stable", expected=thread, replacement="thread-2") is True
    assert await store.rotate_thread("stable", expected=thread, replacement="thread-3") is False
    await store.close()

    reopened = SQLiteSessionStore(path)
    await reopened.open()
    restored = await reopened.get("stable")
    assert restored is not None
    assert restored.agent_id == "writer"
    assert restored.active_thread_id == "thread-2"
    assert dict(restored.metadata) == {"x": 1}
    assert await reopened.delete("stable") is True
    await reopened.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_session_handle_hides_thread_and_restart_only_reloads_capabilities(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    await store.open()
    plugins = Plugins()
    runs = []

    async def run_factory(session_id, prompt, **options):
        runs.append((session_id, prompt, options))
        return "run-handle"

    manager = SessionManager(
        store=store,
        plugin_manager=plugins,
        run_factory=run_factory,
    )
    with pytest.raises(ValueError, match="agent_id"):
        await manager.create(metadata={"owner": "user"})
    generated = await manager.create(
        agent_id="writer", metadata={"owner": "user"}
    )
    stable = await manager.create(session_id="stable", agent_id="writer")

    assert generated.id != stable.id
    assert generated.agent_id == "writer"
    assert stable.agent_id == "writer"
    renamed = await manager.rename("stable", "**Renamed**")
    assert renamed.title == "Renamed"
    before_clear = await store.get("stable")
    assert before_clear is not None
    cleared = await manager.clear_context("stable")
    after_clear = await store.get("stable")
    assert after_clear is not None
    assert cleared.id == "stable"
    assert after_clear.active_thread_id != before_clear.active_thread_id
    assert not hasattr(stable, "thread_id")
    before = after_clear
    result = await stable.restart()
    after = await store.get("stable")
    assert result.id == stable.id
    assert plugins.calls == 1
    assert before == after
    assert cleared.agent_id == "writer"
    assert await cleared.run("hello") == "run-handle"
    assert await cleared.run("again", agent_id="writer") == "run-handle"
    with pytest.raises(SessionAgentMismatchError, match="bound to agent 'writer'"):
        await cleared.run("wrong", agent_id="reviewer")
    with pytest.raises(SessionAgentMismatchError):
        await manager.get_or_create(session_id="stable", agent_id="reviewer")
    with pytest.raises(ValueError, match="model override"):
        await cleared.run("override", model="other-model")
    assert runs == [
        (
            "stable",
            "hello",
            {
                "agent_id": "writer",
                "thread_id": before.active_thread_id,
            },
        ),
        (
            "stable",
            "again",
            {
                "agent_id": "writer",
                "thread_id": before.active_thread_id,
            },
        ),
    ]

    plugins.fail = True
    with pytest.raises(RuntimeError, match="invalid candidate"):
        await stable.restart()
    assert await store.get("stable") == after
    await store.close()


@pytest.mark.asyncio
async def test_concurrent_session_creates_keep_one_agent_binding(
    tmp_path: Path,
) -> None:
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    await store.open()
    manager = SessionManager(
        store=store,
        plugin_manager=Plugins(),
    )

    results = await asyncio.gather(
        manager.create("race", agent_id="writer"),
        manager.create("race", agent_id="reviewer"),
        return_exceptions=True,
    )

    handles = [item for item in results if not isinstance(item, Exception)]
    conflicts = [
        item for item in results if isinstance(item, SessionAgentMismatchError)
    ]
    assert len(handles) == 1
    assert len(conflicts) == 1
    assert (await manager.get("race")).agent_id == handles[0].agent_id
    await store.close()
