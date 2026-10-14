from pathlib import Path
from dataclasses import replace
import os
import tempfile

import pytest

from bianbu_agent.agents import AgentPackageManager, AgentSpec
from bianbu_agent.home import AgentHome


def test_system_agents_are_readable_without_copying_into_user_home(tmp_path: Path):
    system_home = AgentHome(tmp_path / "system")
    spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
    AgentPackageManager(system_home).create(spec)
    user_home = AgentHome(tmp_path / "user")
    packages = AgentPackageManager(user_home, system_agents_dir=system_home.agents_dir)
    assert packages.inspect(spec.id).manifest.spec == spec
    assert [item.id for item in packages.list()] == [spec.id]
    assert packages.is_system(spec.id)
    assert not (user_home.agents_dir / spec.id).exists()
    user = AgentSpec(id="com.bianbu.012345abcdef", name="Writer")
    packages.create(user)
    assert {item.id for item in packages.list()} == {spec.id, user.id}
    assert not packages.is_system(user.id)


@pytest.mark.parametrize("operation", ["update", "sync", "write_avatar", "delete_avatar"])
def test_user_operations_create_overrides_without_modifying_system_agent(tmp_path: Path, operation: str):
    system_home = AgentHome(tmp_path / "system")
    spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
    original = AgentPackageManager(system_home).create(spec)
    user_home = AgentHome(tmp_path / "user")
    packages = AgentPackageManager(user_home, system_agents_dir=system_home.agents_dir)
    calls = {
        "create": lambda: packages.create(spec),
        "update": lambda: packages.update(spec),
        "install": lambda: packages.install(original.root),
        "sync": lambda: packages.sync(original.root),
        "uninstall": lambda: packages.uninstall(spec.id),
        "write_avatar": lambda: packages.write_avatar(spec.id, ".png", b"image"),
        "delete_avatar": lambda: packages.delete_avatar(spec.id),
    }
    original_bytes = (original.root / "agent.yaml").read_bytes()
    calls[operation]()
    assert packages.inspect(spec.id).root == user_home.agents_dir / spec.id
    assert (original.root / "agent.yaml").read_bytes() == original_bytes


def test_deleting_a_packaged_agent_hides_it_without_touching_the_package(tmp_path: Path):
    """A user can remove a factory expert; the signed package keeps its bytes."""

    system_home = AgentHome(tmp_path / "system")
    spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
    original = AgentPackageManager(system_home).create(spec)
    original_bytes = (original.root / "agent.yaml").read_bytes()
    user_home = AgentHome(tmp_path / "user")
    packages = AgentPackageManager(user_home, system_agents_dir=system_home.agents_dir)

    assert packages.uninstall(spec.id) is True
    assert packages.inspect(spec.id) is None
    assert [item.id for item in packages.list()] == []
    assert (original.root / "agent.yaml").read_bytes() == original_bytes
    marker = user_home.agents_dir / f".hidden-{spec.id}"
    assert marker.is_file()
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not (user_home.agents_dir / spec.id).exists()
    # Deleting the same Agent twice reports "nothing left" instead of failing.
    assert packages.uninstall(spec.id) is False
    # Publishing an Agent with that ID is how the removal gets undone.
    restored = packages.create(spec)
    assert restored.root == user_home.agents_dir / spec.id
    assert not marker.exists()
    assert packages.uninstall(spec.id) is True
    assert packages.inspect(spec.id) is None


def test_user_override_wins_over_system_definition(tmp_path: Path):
    system_home, user_home = AgentHome(tmp_path / "system"), AgentHome(tmp_path / "user")
    spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
    AgentPackageManager(system_home).create(spec)
    AgentPackageManager(user_home).create(AgentSpec(id=spec.id, name="Shadow"))
    packages = AgentPackageManager(user_home, system_agents_dir=system_home.agents_dir)
    assert packages.inspect(spec.id).manifest.spec.name == "Shadow"
    assert [item.manifest.spec.name for item in packages.list()] == ["Shadow"]


def test_system_package_upgrade_rollback_preserves_location(tmp_path):
    system_home, user_home = AgentHome(tmp_path / "system"), AgentHome(tmp_path / "user")
    spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
    original = AgentPackageManager(system_home).create(spec)
    candidate = AgentPackageManager(AgentHome(tmp_path / "candidate")).create(
        replace(spec, description="upgrade"), version="2.0.0",
    )
    packages = AgentPackageManager(user_home, system_agents_dir=system_home.agents_dir)
    with pytest.raises(RuntimeError, match="registration unavailable"):
        packages.install_with_security(candidate.root, upgrade=True, register=lambda _: None, required=True)
    restored = packages.inspect(spec.id)
    assert restored.root == original.root
    assert restored.content_digest == original.content_digest
    assert not (user_home.agents_dir / spec.id).exists()
    updated = packages.install(candidate.root, upgrade=True)
    assert updated.root == user_home.agents_dir / spec.id
    assert updated.manifest.spec.description == "upgrade"
    with pytest.raises(FileExistsError):
        packages.create(spec)
    with pytest.raises(ValueError, match="case-insensitively"):
        packages.create(replace(spec, id=spec.id.upper()))


def test_system_package_on_another_filesystem_can_sync_and_delete(tmp_path):
    if not os.access("/dev/shm", os.W_OK) or Path("/dev/shm").stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("requires a writable second filesystem")
    with tempfile.TemporaryDirectory(prefix="bianbu-package-test-", dir="/dev/shm") as directory:
        system_home = AgentHome(Path(directory))
        spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
        original = AgentPackageManager(system_home).create(spec)
        user_home = AgentHome(tmp_path / "user")
        packages = AgentPackageManager(user_home, system_agents_dir=system_home.agents_dir)
        candidate = AgentPackageManager(AgentHome(tmp_path / "source")).create(
            replace(spec, description="cross filesystem"), version="2.0.0",
        )
        assert packages.sync(candidate.root).root == user_home.agents_dir / spec.id
        assert packages.inspect(spec.id).manifest.spec.description == "cross filesystem"
        assert packages.uninstall(spec.id) is True
        assert packages.inspect(spec.id) is None
        assert original.root.is_dir()


@pytest.mark.asyncio
async def test_system_agent_api_edits_user_override_and_deletes_it(tmp_path: Path, monkeypatch):
    from tests.sdk.api.test_agents import _file_backed_api
    from bianbu_agent.agents import system_paths

    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "system-profiles")
    async with _file_backed_api(tmp_path) as (client, runtime):
        system_dir = system_paths.system_agent_directory(runtime.home.root)
        spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant")
        AgentPackageManager(AgentHome(system_dir.parent)).create(spec)
        listed = (await client.get("/api/v1/agents")).json()["data"]
        system = next(item for item in listed if item["id"] == spec.id)
        assert system["origin"] == "system"
        assert not system.get("read_only", False)
        mutations = []

        async def unregister(agent_id):
            mutations.append(agent_id)

        monkeypatch.setattr(runtime, "unregister_logical_agent", unregister)
        response = await client.patch(f"/api/v1/agents/{spec.id}", json={"description": "My Bibit"})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["origin"] == "user"
        assert runtime.agent_packages.inspect(spec.id).manifest.spec.description == "My Bibit"
        assert runtime.agent_packages.inspect(spec.id).root == runtime.home.agents_dir / spec.id
        response = await client.delete(f"/api/v1/agents/{spec.id}")
        assert response.status_code == 204, response.text
        assert runtime.agent_packages.inspect(spec.id) is None
        assert not (runtime.home.agents_dir / spec.id).exists()
        assert (runtime.home.agents_dir / f".hidden-{spec.id}").is_file()
        assert mutations == [spec.id], "Deleting an Agent revokes its security identity"
        assert AgentPackageManager(AgentHome(system_dir.parent)).inspect(spec.id) is not None


def test_worker_mounts_system_profile_read_only_at_sandbox_profile_path(tmp_path, monkeypatch):
    import json
    from bianbu_agent.agents import system_paths
    from bianbu_agent.security.execution_runtime import _with_runtime_worker_policy
    from bianbu_agent.security.security_runtime import RuntimeStartDecision

    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    user_data = tmp_path / "user" / ".bianbu-agent"
    source = system_paths.system_agent_directory(user_data)
    source.mkdir(parents=True)
    policy = {"readonly_bind_mounts": [{"host": str(user_data), "target": "/home/agent"}]}
    decision = RuntimeStartDecision("lease", json.dumps(policy), "sandbox", "bwrap", True, 2**31, "test")
    resolved = _with_runtime_worker_policy(
        decision, memory_uds=None, memory_registry_dir=None, bibit_intent_root=None,
    ).policy
    assert {"host": str(source), "target": str(system_paths.system_agent_directory(Path("/home/agent")))} in resolved["readonly_bind_mounts"]
    assert all(item.get("host") != str(source) for item in resolved.get("writable_mounts", []))


@pytest.mark.asyncio
async def test_system_bibit_can_open_editor_without_developer_mode(tmp_path, monkeypatch):
    import asyncio
    from bianbu_agent.agents import system_paths, AgentModel
    from playwright.async_api import async_playwright, expect
    from tests.sdk.console.test_browser_contract import _running_console

    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    source = system_paths.system_agent_directory(tmp_path / ".bianbu-agent")
    spec = AgentSpec(id="com.bianbu.abcdef012345", name="BibitEdgeAssistant", model=AgentModel("browser", "browser-contract"))
    AgentPackageManager(AgentHome(source.parent)).create(spec)
    async with _running_console(tmp_path) as (base_url, _):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            release_settings = asyncio.Event()
            try:
                page = await browser.new_page()

                async def delayed_settings(route):
                    response = await route.fetch()
                    await release_settings.wait()
                    await route.fulfill(response=response)

                await page.route("**/api/v1/settings", delayed_settings)
                await page.goto(f"{base_url}/chat", wait_until="domcontentloaded")
                await page.evaluate("navigateTo('agents')")
                card = page.locator(f'.agent-card[data-agent-id="{spec.id}"]')
                await expect(card).to_be_visible()
                await expect(card).not_to_contain_text("只读")
                await expect(card.locator('.agent-card-delete')).to_be_enabled()
                await expect(card.locator('.agent-card-use')).to_be_enabled()
                release_settings.set()
                await page.wait_for_function("() => isNewChatDraft()")
                assert await page.evaluate("currentView") == "agents"
                await card.click()
                await expect(page.locator('#agent-editor-overlay')).to_be_visible()
            finally:
                release_settings.set()
                await browser.close()


def test_package_agent_layer_is_available_and_profile_starter_overrides_it(tmp_path, monkeypatch):
    package_home = AgentHome(tmp_path / 'package')
    profile_home = AgentHome(tmp_path / 'profile')
    spec = AgentSpec(id='com.bianbu.abcdef012345', name='Packaged default')
    package = AgentPackageManager(package_home).create(spec)
    AgentPackageManager(profile_home).create(replace(spec, name='Profile starter'))
    monkeypatch.setenv('BIANBU_SYSTEM_RESOURCE_ROOT', str(package_home.root))
    manager = AgentPackageManager(AgentHome(tmp_path / 'user'), system_agents_dir=profile_home.agents_dir)
    assert manager.inspect(spec.id).manifest.spec.name == 'Profile starter'
    override = manager.update(replace(spec, name='User choice'))
    assert override.root == manager.home.agents_dir / spec.id
    assert manager.inspect(spec.id).manifest.spec.name == 'User choice'
    assert not manager.is_system(spec.id)
    assert package.manifest.spec.name == 'Packaged default'
