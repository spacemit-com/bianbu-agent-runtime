from pathlib import Path
import asyncio
from dataclasses import replace

import pytest

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.plugins.manager import PluginManager
from bianbu_agent.server.runtime import _inventory_snapshot


def write_skill(root, body="first"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(
        f"---\nname: temporary\ndescription: Temporary skill\n---\n{body}\n"
    )


def manager_for(config):
    return PluginManager(
        snapshot_provider=lambda: _inventory_snapshot(config),
        skill_snapshot_provider=lambda: _inventory_snapshot(config, skills_only=True, strict_skills=True),
        workspace_root=config.data_dir / "workspace",
        plugin_data_root=config.data_dir / "workspace" / "plugin-data",
    )


@pytest.mark.asyncio
async def test_run_admission_handoff_walks_capabilities_once(tmp_path, monkeypatch):
    """Admission verifies the Inventory; the lease right after reuses it."""

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    config = RuntimeConfig(data_dir=tmp_path / "runtime")
    manager = manager_for(config)
    walks = 0
    original = manager._snapshot

    async def counted():
        nonlocal walks
        walks += 1
        return await original()

    manager._snapshot = counted
    await manager.start()
    baseline = walks
    try:
        await manager.reload_installed(handoff=True)
        assert walks == baseline + 1
        async with manager.lease():
            pass
        # The lease reused the verified snapshot instead of walking again.
        assert walks == baseline + 1
        # Consumed once: the next lease scans for itself again.
        async with manager.lease():
            pass
        assert walks == baseline + 2
        # A reload without the handoff keeps the previous double scan.
        await manager.reload_installed()
        async with manager.lease():
            pass
        assert walks == baseline + 4
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_handoff_expires_and_still_validates_skills(tmp_path, monkeypatch):
    """A stale handoff is ignored and a half-written Skill still fails admission."""

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    config = RuntimeConfig(data_dir=tmp_path / "runtime")
    manager = manager_for(config)
    await manager.start()
    try:
        skill = Path.home() / ".bianbu-agent" / "skills" / "temporary"
        write_skill(skill)
        await manager.reload_installed(handoff=True)
        assert manager._handoff is not None
        manager._handoff = replace(manager._handoff, taken_at=0.0)
        assert manager._take_handoff() is None
        (skill / "SKILL.md").write_text("---\nname:")
        from bianbu_agent.plugins.manager import CapabilityReloadError
        with pytest.raises(CapabilityReloadError):
            await manager.reload_installed(handoff=True)
        assert manager._handoff is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_skill_watcher_backs_off_and_snaps_back_on_change(tmp_path, monkeypatch):
    """空转时轮询逐次退避，一旦目录有变化立刻回到最小间隔。"""

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    manager = manager_for(RuntimeConfig(data_dir=tmp_path / "runtime"))
    delays: list[float] = []
    outcomes = iter([False, False, False, False, True, False])

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= 6:
            raise asyncio.CancelledError

    async def fake_refresh(*, snapshot=None) -> bool:
        return next(outcomes)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(manager, "refresh_skills", fake_refresh)
    with pytest.raises(asyncio.CancelledError):
        await manager._watch_skills()

    # 1 → 2 → 4 → 8 → 10（封顶）→ 有变化后回到 1
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0, 1.0]


@pytest.mark.asyncio
async def test_user_skills_add_edit_delete_preserve_leases_and_host(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    config = RuntimeConfig(data_dir=tmp_path / "runtime")
    manager = manager_for(config)
    await manager.start()
    host = manager.host
    skill = Path.home() / ".bianbu-agent" / "skills" / "temporary"
    try:
        write_skill(skill)
        async with manager.lease() as first:
            assert first.skills["temporary"].spec.instructions == "first"
            write_skill(skill, "second")
            async with manager.lease() as second:
                assert second.id > first.id
                assert second.skills["temporary"].spec.instructions == "second"
                assert first.skills["temporary"].spec.instructions == "first"
                assert manager.host is host
            (skill / "SKILL.md").write_text("---\nname:")
            async with manager.lease() as invalid:
                assert invalid is second
            from bianbu_agent.plugins.manager import CapabilityReloadError
            with pytest.raises(CapabilityReloadError):
                await manager.reload_installed()
            assert manager.current_generation is second
            write_skill(skill, "recovered")
            async with manager.lease() as recovered:
                assert recovered.skills["temporary"].spec.instructions == "recovered"
            (skill / "SKILL.md").unlink()
            async with manager.lease() as removed:
                assert "temporary" not in removed.skills
                assert "temporary" in first.skills
    finally:
        await manager.close()
    assert manager._skill_watch_task is None


@pytest.mark.asyncio
async def test_skill_watcher_publishes_without_runs_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    config = RuntimeConfig(data_dir=tmp_path / "runtime")
    manager = manager_for(config)
    await manager.start()
    task = manager._skill_watch_task
    try:
        write_skill(config.data_dir / "skills" / "temporary")
        async with asyncio.timeout(3):
            while "temporary" not in manager.current_generation.skills:
                await asyncio.sleep(0.02)
    finally:
        await manager.close()
    assert task.done()


def test_sandbox_mounts_user_skill_directory_readonly(tmp_path):
    import json
    from bianbu_agent.security.execution_runtime import _with_runtime_worker_policy
    from bianbu_agent.security.security_runtime import RuntimeStartDecision

    root = tmp_path / "user-skills"
    root.mkdir()
    decision = RuntimeStartDecision(
        "lease", json.dumps({}), "sandbox", "bwrap", True, 2**31, "test"
    )
    result = _with_runtime_worker_policy(
        decision, memory_uds=None, memory_registry_dir=None,
        bibit_intent_root=None, user_skills_root=root,
    )
    mount = {"host": str(root), "target": "/run/bianbu-agent/user-skills"}
    assert mount in result.policy["readonly_bind_mounts"]
    assert mount not in result.policy["writable_mounts"]
    from bianbu_agent.security.execution_runtime import ExecutionRequest, _safe_env, _host_to_target
    env = _safe_env(result.policy, ExecutionRequest("test", ("/bin/true",)))
    assert env["BIANBU_USER_SKILLS_DIR"] == str(root)
    assert _host_to_target(result.policy, env["BIANBU_USER_SKILLS_DIR"]) == mount["target"]


@pytest.mark.asyncio
async def test_overlay_leases_keep_plugins_alive_across_full_reload(tmp_path, monkeypatch):
    from bianbu_agent.plugins import PluginCandidate, PluginManifest, PluginHealth

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    config = RuntimeConfig(data_dir=tmp_path / "runtime")
    instances = []
    manifest = PluginManifest(id="test.observer", version="1.0.0")

    class Observer:
        async def setup(self, context):
            self.stopped = False
            instances.append(self)

        async def start(self):
            pass

        async def health(self):
            return PluginHealth.healthy()

        async def stop(self):
            assert not self.stopped
            self.stopped = True

    Observer.manifest = manifest
    manager = PluginManager(
        snapshot_provider=lambda: _inventory_snapshot(config),
        skill_snapshot_provider=lambda: _inventory_snapshot(config, skills_only=True, strict_skills=True),
        workspace_root=config.data_dir / "workspace",
        plugin_data_root=config.data_dir / "workspace" / "plugin-data",
        explicit_candidates=(PluginCandidate(factory=lambda context: Observer(), manifest=manifest),),
    )
    await manager.start()
    try:
        write_skill(config.data_dir / "skills" / "temporary")
        async with manager.lease() as old:
            assert old.skills["temporary"].spec.instructions == "first"
            assert len(instances) == 1
            write_skill(config.data_dir / "skills" / "temporary", "second")
            async with manager.lease() as overlay:
                assert overlay.id > old.id
                assert len(instances) == 1
                result = await manager.reload_installed()
                assert result.generation_id > overlay.id
                assert len(instances) == 2
                assert not instances[0].stopped
                assert overlay.skills["temporary"].spec.instructions == "second"
            assert not instances[0].stopped
        assert instances[0].stopped
        assert not instances[1].stopped
    finally:
        await manager.close()
    assert instances[1].stopped


@pytest.mark.asyncio
async def test_failed_watcher_does_not_prevent_host_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    manager = manager_for(RuntimeConfig(data_dir=tmp_path / "runtime"))
    await manager.start()
    original = manager._skill_watch_task
    original.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original

    async def failed():
        raise RuntimeError("watcher failed")

    manager._skill_watch_task = asyncio.create_task(failed())
    await asyncio.sleep(0)
    host = manager.host
    await manager.close()
    assert manager._host is None
    assert manager._skill_watch_task is None
    assert host._current is None


@pytest.mark.asyncio
async def test_reconfigure_retains_last_valid_skill_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    config = RuntimeConfig(data_dir=tmp_path / "runtime")
    manager = manager_for(config)
    await manager.start()
    root = config.data_dir / "skills" / "temporary"
    try:
        write_skill(root)
        async with manager.lease() as previous:
            assert previous.skills["temporary"].spec.instructions == "first"
            (root / "SKILL.md").write_text("---\nname:")
            assert await manager.reconfigure({}) == ()
            current = manager.current_generation
            assert current.id > previous.id
            assert current.skills["temporary"].spec.instructions == "first"
            assert current.skills["temporary"].spec.instructions == previous.skills["temporary"].spec.instructions
            assert Path(current.skills["temporary"].spec.metadata["manifest_path"]).read_text().endswith("first\n")
            assert manager.host.snapshot() is current
            async with manager.lease() as retained:
                assert retained is current
            write_skill(root, "recovered")
            async with manager.lease() as recovered:
                assert recovered.skills["temporary"].spec.instructions == "recovered"
                assert previous.skills["temporary"].spec.instructions == "first"
    finally:
        await manager.close()
