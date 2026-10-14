from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import zipfile
from types import SimpleNamespace

import pytest

from bianbu_agent import AgentHome
from bianbu_agent import ToolCallContext
from bianbu_agent.agents import AgentPackageManager
from bianbu_agent.agents.projects import (
    build_agent_project,
    check_agent_project,
    init_agent_project,
    sync_agent_project,
)
from bianbu_agent.inventory import CapabilityCatalogBuilder, CapabilityKind, InventorySnapshot
from bianbu_agent.agents.manager import AgentManager
from bianbu_agent.agents import AgentManifest, AgentSpec, InMemoryAgentRegistry
from bianbu_agent.models import ModelRegistry


async def test_installed_agent_tools_import_helpers_from_their_published_package(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / "home").prepare()
    manager = AgentPackageManager(home)
    source = (
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    @tool\n"
        "    def greet(name: str) -> str:\n"
        "        '''Greet using a package-owned helper.'''\n"
        "        from .helper import greeting\n"
        "        return greeting(name)\n"
        "    return greet\n"
    )
    helper = (
        "from pathlib import Path\n"
        "def greeting(name):\n"
        "    return f'Hello, {name}! from {Path(__file__).parent}'\n"
    )
    for owner in ("one", "two"):
        artifact = tmp_path / f"{owner}.agent"
        manifest = AgentManifest(
            schema_version=1, version="1.0.0",
            spec=AgentSpec(id=owner, name=owner, tools=(f"{owner}/greet",)),
        )
        # Construct the archive without loading its source: installation first
        # validates the factory in staging, then publishes it by atomic rename.
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("agent.yaml", manifest.to_yaml())
            archive.writestr("tools/greet/tool.py", source)
            archive.writestr("tools/greet/helper.py", helper)
        manager.install(artifact)
    assert not any(home.staging_dir.iterdir())
    catalog = CapabilityCatalogBuilder(workspace_root=home.workspace_dir).build(InventorySnapshot(0))
    for owner in ("one", "two"):
        installed_root = home.agents_dir / owner / "tools/greet"
        assert (installed_root / "helper.py").is_file()
        result = await catalog.tools[f"{owner}/greet"].handler(
            ToolCallContext("run", "session", owner), {"name": "RT24"},
        )
        assert result.content == f"Hello, RT24! from {installed_root}"


@pytest.mark.parametrize("source", [
    "def create_tool(context):\n    return object()\n",
    "def unrelated():\n    return None\n",
    "from bianbu_agent.tools import tool\ndef create_tool(context):\n"
    "    @tool\n    def wrong(name: str) -> str:\n        '''Wrong name.'''\n        return name\n"
    "    return wrong\n",
])
def test_check_rejects_invalid_bundled_tool_factory(tmp_path: Path, source: str) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    tool_root = project / "tools" / "greet"
    tool_root.mkdir(parents=True, exist_ok=True)
    (tool_root / "tool.py").write_text(source)
    with pytest.raises(ValueError, match="Tool"):
        check_agent_project(project)


async def test_bundled_tools_follow_package_lifetime_and_owner_selection(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / "home").prepare()
    manager = AgentPackageManager(home)
    for agent_id in ("one", "two"):
        project = init_agent_project(tmp_path / agent_id, agent_id=agent_id, name=agent_id)
        manager.install(build_agent_project(project.root).path)
    builder = CapabilityCatalogBuilder(workspace_root=home.workspace_dir)
    catalog = builder.build(InventorySnapshot(revision=0))
    assert {"one/greet", "two/greet"} <= set(catalog.tools)
    assert catalog.sources[(CapabilityKind.TOOL, "one/greet")].root == home.agents_dir / "one/tools/greet"
    generation = SimpleNamespace(tools=catalog.tools, mcp_tools={}, mcps={}, skills={}, hooks={})
    agents = AgentManager(
        store=InMemoryAgentRegistry((
            AgentSpec(id="one", name="One", tools=("*",)),
            AgentSpec(id="other", name="Other", tools=("one/greet",)),
        )),
        model_registry=ModelRegistry(default_model=None), generation_provider=lambda: generation,
    )
    one_tools = (await agents.materialize("one", generation, allow_model_repair=True)).tools
    other_tools = (await agents.materialize("other", generation, allow_model_repair=True)).tools
    assert "one/greet" in one_tools
    assert "one/greet" not in other_tools
    assert manager.uninstall("one") is True
    assert manager.uninstall("one") is False
    remaining = builder.build(InventorySnapshot(revision=0))
    assert "one/greet" not in remaining.tools
    assert "two/greet" in remaining.tools
    assert manager.inspect("two") is not None


def test_agent_install_rejects_existing_plugin_tool_owner(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / "home").prepare()
    other = home.plugins_dir / "demo" / "tools" / "greet"
    other.mkdir(parents=True)
    (other / "tool.py").write_text("# owned by another package\n")
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo")
    with pytest.raises(ValueError, match="collision"):
        AgentPackageManager(home).install(build_agent_project(project.root).path)
    assert not (home.agents_dir / "demo").exists()
    assert (other / "tool.py").read_text() == "# owned by another package\n"
    assert not any(home.staging_dir.iterdir())


def test_invalid_bundled_tool_archive_preserves_installed_owner(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / "home").prepare()
    manager = AgentPackageManager(home)
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo")
    original = manager.install(build_agent_project(project.root).path)
    broken = tmp_path / "broken.agent"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr("agent.yaml", project.manifest.to_yaml().replace("0.1.0", "0.2.0"))
        archive.writestr("tools/greet/tool.py", "def create_tool(context):\n    return None\n")
    with pytest.raises(ValueError, match="Tool"):
        manager.install(broken, upgrade=True)
    assert manager.inspect("demo").content_digest == original.content_digest
    assert not any(home.staging_dir.iterdir())


def test_suggested_project_ids_are_random_reverse_domain_ids(tmp_path: Path) -> None:
    import re
    from bianbu_agent.agents.projects import suggested_agent_id
    first, second = suggested_agent_id(tmp_path), suggested_agent_id(tmp_path)
    assert re.fullmatch(r"com\.bianbu\.[0-9a-f]{12}", first)
    assert first != second


def test_init_creates_a_checked_self_contained_project(tmp_path: Path) -> None:
    project = tmp_path / "translator"

    created = init_agent_project(
        project,
        agent_id="translator",
        name="TranslatorExpert",
        name_zh="翻译专家",
    )

    assert created.root == project.resolve()
    assert created.manifest.version == "0.1.0"
    assert created.manifest.spec.name == "TranslatorExpert"
    assert created.manifest.spec.name_zh == "翻译专家"
    assert (project / "runtime.yaml").is_file()
    assert (project / "README.md").is_file()
    assert "dist/" in (project / ".gitignore").read_text(encoding="utf-8")
    assert check_agent_project(project).content_digest == created.content_digest


def test_init_refuses_to_overlay_an_existing_directory(tmp_path: Path) -> None:
    project = tmp_path / "occupied"
    project.mkdir()
    (project / "notes.txt").write_text("owned by user", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        init_agent_project(project, agent_id="demo", name="Demo")

    assert (project / "notes.txt").read_text(encoding="utf-8") == "owned by user"


def test_check_rejects_runtime_and_build_state_inside_package(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    (project / "dist").mkdir()
    (project / "dist" / "old.agent").write_bytes(b"old")
    (project / ".bianbu-agent-dev").mkdir()
    (project / ".bianbu-agent-dev" / "runtime.log").write_text("old")

    checked = check_agent_project(project)

    relative = {path.relative_to(project).as_posix() for path in checked.files}
    assert "dist/old.agent" not in relative
    assert ".bianbu-agent-dev/runtime.log" not in relative
    assert "runtime.yaml" not in relative
    assert "README.md" not in relative


def test_check_does_not_traverse_ignored_runtime_state(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    runtime_dir = project / ".bianbu-agent-data" / "runtime"
    runtime_dir.mkdir(parents=True)
    os.mkfifo(runtime_dir / "worker.pipe")

    checked = check_agent_project(project)

    assert {path.relative_to(project).as_posix() for path in checked.files} == {
        "agent.yaml", "tools/greet/tool.py",
    }


def test_build_is_deterministic_and_uses_archive_root_layout(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    (project / "avatar.png").write_bytes(b"avatar")

    first = build_agent_project(project, output=tmp_path / "one.agent")
    second = build_agent_project(project, output=tmp_path / "two.agent")

    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.content_digest == second.content_digest
    with zipfile.ZipFile(first.path) as archive:
        assert archive.namelist() == ["agent.yaml", "avatar.png", "tools/greet/tool.py"]
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)


def test_build_refuses_to_replace_an_artifact(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    output = tmp_path / "demo.agent"
    output.write_bytes(b"owned")

    with pytest.raises(FileExistsError, match="already exists"):
        build_agent_project(project, output=output)

    assert output.read_bytes() == b"owned"


def test_check_rejects_a_file_that_install_would_reject(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    with (project / "oversized.bin").open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)

    with pytest.raises(ValueError, match="too large"):
        check_agent_project(project)


def test_build_streams_files_instead_of_reading_them_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    (project / "payload.bin").write_bytes(b"payload")

    def reject_read_bytes(_path: Path) -> bytes:
        raise AssertionError("build must stream package files")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)

    artifact = build_agent_project(project, output=tmp_path / "demo.agent")

    assert artifact.path.is_file()


def test_agent_package_manager_installs_agent_archive(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "writer", agent_id="writer", name="Writer").root
    artifact = build_agent_project(project, output=tmp_path / "writer.agent")
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    installed = manager.install(artifact.path)

    assert installed.id == "writer"
    assert installed.content_digest == artifact.content_digest
    assert installed.root.name == "writer"


@pytest.mark.parametrize(
    "entry_name",
    ("../escape", "/absolute", "folder\\windows", "agent.yaml/../payload"),
)
def test_agent_archive_rejects_unsafe_paths(tmp_path: Path, entry_name: str) -> None:
    artifact = tmp_path / "malicious.agent"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("agent.yaml", "schema_version: 1\nagent: {}\n")
        archive.writestr(entry_name, "payload")
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    with pytest.raises(ValueError, match="unsafe Agent package archive"):
        manager.install(artifact)

    assert not (tmp_path / "escape").exists()


def test_agent_archive_rejects_symbolic_links(tmp_path: Path) -> None:
    artifact = tmp_path / "linked.agent"
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("agent.yaml", "schema_version: 1\nagent: {}\n")
        archive.writestr(link, "target")
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    with pytest.raises(ValueError, match="symbolic links"):
        manager.install(artifact)


def test_agent_archive_rejects_nul_truncated_names(tmp_path: Path) -> None:
    artifact = tmp_path / "nul.agent"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("agent.yaml", "schema_version: 1\nagent: {}\n")
        archive.writestr("payloadxxxxx", "payload")
    artifact.write_bytes(
        artifact.read_bytes().replace(b"payloadxxxxx", b"payload\x00junk")
    )
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    with pytest.raises(ValueError, match="unsafe Agent package archive"):
        manager.install(artifact)


def test_agent_archive_rejects_casefold_colliding_ancestors(tmp_path: Path) -> None:
    artifact = tmp_path / "collision.agent"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("agent.yaml", "schema_version: 1\nagent: {}\n")
        archive.writestr("A/x", "one")
        archive.writestr("a/y", "two")
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    with pytest.raises(ValueError, match="case-insensitive archive path"):
        manager.install(artifact)


def test_agent_archive_caps_member_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bianbu_agent.agents import packages as package_module

    monkeypatch.setattr(package_module, "MAX_AGENT_PACKAGE_MEMBERS", 2)
    artifact = tmp_path / "many.agent"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr("agent.yaml", "manifest")
        archive.writestr("one", "")
        archive.writestr("two", "")
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    with pytest.raises(ValueError, match="too many entries"):
        manager.install(artifact)


def test_sync_replaces_same_version_content_for_development(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())
    first = sync_agent_project(project, manager)
    (project / "avatar.png").write_bytes(b"changed")

    second = sync_agent_project(project, manager)

    assert first.version == second.version == "0.1.0"
    assert first.content_digest != second.content_digest
    assert (second.root / "avatar.png").read_bytes() == b"changed"


def test_sync_rolls_back_after_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    home = AgentHome(tmp_path / "home").prepare()
    manager = AgentPackageManager(home)
    sync_agent_project(project, manager)
    (project / "avatar.png").write_bytes(b"new")
    from bianbu_agent.agents import packages as package_module

    original_fsync = package_module._fsync_directory
    failed = False

    def fail_once(directory: Path) -> None:
        nonlocal failed
        if directory == home.agents_dir and not failed:
            failed = True
            raise OSError("injected directory fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(package_module, "_fsync_directory", fail_once)

    with pytest.raises(OSError, match="injected"):
        sync_agent_project(project, manager)

    assert not (manager.inspect("demo").root / "avatar.png").exists()
    assert not any(home.staging_dir.iterdir())


def test_project_runtime_template_uses_relative_private_data_dir(tmp_path: Path) -> None:
    project = init_agent_project(tmp_path / "demo", agent_id="demo", name="Demo").root
    document = (project / "runtime.yaml").read_text(encoding="utf-8")

    assert "data_dir: ./.bianbu-agent-data" in document
    assert "default_agent" not in document
    assert "worker:" in document
    assert "host: 127.0.0.1" in document
    assert json.loads(json.dumps(check_agent_project(project).manifest.spec.id)) == "demo"
