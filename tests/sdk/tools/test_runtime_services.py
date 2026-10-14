from pathlib import Path
import pytest

from bianbu_agent import RunEvent, RunResult, ToolCallContext
from bianbu_agent.home import AgentHome
from bianbu_agent.sessions import JSONSessionStore, SessionLogStore
from bianbu_agent.tools import ToolPackageContext, ToolRuntimeServices


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _package(root: Path, name: str) -> ToolPackageContext:
    package_root = root / name
    package_root.mkdir(parents=True)
    return ToolPackageContext(
        name=name,
        package_root=package_root,
        source_digest="0" * 64,
        workspace_root=root,
    )


@pytest.mark.asyncio
async def test_sessions_facet_reads_current_json_records(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    sessions = JSONSessionStore(home.sessions_dir)
    await sessions.open()
    await sessions.create("other", agent_id="agent-a", title="Other")
    await sessions.update_title("other", "Other")
    await sessions.close()
    store = SessionLogStore(home.sessions_dir)
    await store.open()
    await store.start("run", "other", {"prompt": "needle prompt"})
    await store.append("run", RunEvent("token", {"text": "needle result"}))
    await store.finish("run", RunResult("run", "completed", "needle result"))
    await store.close()
    factory = ToolRuntimeServices(home=home, model=None)
    factory._scope_resolver = None
    runtime = await factory.for_call(
        _package(tmp_path / "packages", "session_search"),
        ToolCallContext("run", "current", "agent"),
    )

    sessions, runs = await runtime.sessions.records()

    assert sessions[0]["id"] == "other"
    assert sessions[0]["title"] == "Other"
    assert sessions[0]["agent_id"] == "agent-a"
    assert runs[0]["session_id"] == "other"
    assert runs[0]["request"]["prompt"] == "needle prompt"


def test_public_runtime_has_no_tool_named_business_methods() -> None:
    forbidden = {
        "todo",
        "memory",
        "delegate",
        "session_search",
        "terminal",
    }

    assert forbidden.isdisjoint(dir(ToolRuntimeServices))


@pytest.mark.asyncio
async def test_process_facet_accepts_primitive_foreground_and_background_calls(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    factory = ToolRuntimeServices(home=home, model=None)
    factory._scope_resolver = None
    runtime = await factory.for_call(
        _package(tmp_path / "packages", "terminal"),
        ToolCallContext("run", "session", "agent"),
    )

    try:
        foreground = await runtime.processes.run(
            command="printf primitive-foreground",
            timeout_seconds=10,
        )
        handle = await runtime.processes.start_background(
            command="printf primitive-background",
            timeout_seconds=10,
        )
        background = await runtime.processes.wait(handle.session_id, 10)
    finally:
        await factory.close()

    assert foreground.output == "primitive-foreground"
    assert foreground.exit_code == 0
    assert background.output == "primitive-background"
    assert background.exit_code == 0
