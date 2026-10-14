from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat

import pytest

from bianbu_agent.agents import (
    AgentManifest,
    AgentPackageManager,
    AgentSpec,
)
from bianbu_agent.home import AgentHome


RECURSIVE_ALIAS_MANIFEST = """\
schema_version: 1
agent:
  id: writer
  version: 1.0.0
  name: Writer
  metadata: &metadata
    self: *metadata
"""


def write_source(
    root: Path,
    version: str = "1.0.0",
    *,
    agent_id: str = "writer",
    name: str = "Writer",
) -> Path:
    root.mkdir(parents=True)
    manifest = AgentManifest(
        schema_version=1,
        version=version,
        spec=AgentSpec(id=agent_id, name=name, metadata={"avatar": "✍️"}),
    )
    (root / "agent.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    (root / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    return root


def test_agent_package_install_verify_restart_and_uninstall(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home, protected_ids={"default"})

    installed = manager.install(write_source(tmp_path / "source" / "writer"))

    assert installed.id == "writer"
    assert installed.version == "1.0.0"
    assert len(installed.content_digest) == 64
    assert installed.root == home.agents_dir / "writer"
    assert manager.verify("writer") is True
    assert [item.id for item in AgentPackageManager(home).list()] == ["writer"]
    assert AgentPackageManager(home).inspect("writer") == installed
    assert manager.uninstall("writer") is True
    assert manager.uninstall("writer") is False
    assert not (home.agents_dir / "writer").exists()


def test_agent_package_upgrade_requires_higher_version_and_preserves_old_on_failure(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    manager.install(write_source(tmp_path / "v1" / "writer", "1.0.0"))

    with pytest.raises(FileExistsError, match="already installed"):
        manager.install(write_source(tmp_path / "duplicate" / "writer", "1.0.0"))
    with pytest.raises(ValueError, match="higher"):
        manager.install(write_source(tmp_path / "equal" / "writer", "1.0.0"), upgrade=True)
    with pytest.raises(ValueError, match="higher"):
        manager.install(write_source(tmp_path / "old" / "writer", "0.9.0"), upgrade=True)

    upgraded = manager.install(
        write_source(tmp_path / "v2" / "writer", "1.1.0", name="Senior Writer"),
        upgrade=True,
    )
    assert upgraded.version == "1.1.0"
    assert upgraded.manifest.spec.name == "Senior Writer"
    assert not any(home.staging_dir.iterdir())


def test_agent_package_crud_uses_version_one_and_preserves_package_assets(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    created = manager.create(AgentSpec(id="reviewer", name="Reviewer"))
    assert (created.root / "agent.yaml").is_file()
    assert not (created.root / "agent.toml").exists()
    asset = created.root / "avatar.txt"
    asset.write_text("asset", encoding="utf-8")

    updated = manager.update(AgentSpec(id="reviewer", name="Senior Reviewer"))

    assert created.version == updated.version == "1.0.0"
    assert updated.manifest.spec.name == "Senior Reviewer"
    assert (updated.root / "agent.yaml").is_file()
    assert not (updated.root / "agent.toml").exists()
    assert asset.read_text(encoding="utf-8") == "asset"
    with pytest.raises(FileExistsError, match="already installed"):
        manager.create(AgentSpec(id="reviewer", name="Duplicate"))
    with pytest.raises(KeyError, match="missing"):
        manager.update(AgentSpec(id="missing", name="Missing"))


def test_agent_package_rejects_source_that_only_contains_agent_toml(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    source = tmp_path / "source" / "writer"
    source.mkdir(parents=True)
    manifest = AgentManifest(
        schema_version=1,
        version="1.0.0",
        spec=AgentSpec(id="writer", name="Writer"),
    )
    (source / "agent.toml").write_text(manifest.to_yaml(), encoding="utf-8")

    with pytest.raises(ValueError, match="no readable agent.yaml"):
        manager.install(source)


def test_agent_package_rejects_alias_manifest_as_path_qualified_validation(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    source = tmp_path / "source" / "writer"
    source.mkdir(parents=True)
    (source / "agent.yaml").write_text(
        RECURSIVE_ALIAS_MANIFEST,
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="YAML aliases are not allowed") as exc_info:
        manager.install(source)

    message = str(exc_info.value)
    assert "invalid agent manifest" in message
    assert "agent.yaml" in message


def test_agent_package_inspect_skips_alias_manifest_as_path_qualified_validation(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    package_root = home.agents_dir / "writer"
    package_root.mkdir()
    manifest_path = package_root / "agent.yaml"
    manifest_path.write_text(RECURSIVE_ALIAS_MANIFEST, encoding="utf-8")

    # The loader still validates the manifest by its own path, so the reason is
    # reported with the file it came from; the unloadable record is retired
    # instead of stopping Runtime startup.
    with pytest.raises(ValueError, match="YAML aliases are not allowed") as exc_info:
        AgentManifest.from_path(manifest_path)

    message = str(exc_info.value)
    assert "invalid agent manifest" in message
    assert str(manifest_path) in message
    assert manager.inspect("writer") is None
    assert manager.list() == ()


def test_agent_package_rejects_unsafe_sources_ids_and_casefold_collisions(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    source = write_source(tmp_path / "source" / "writer")
    (source / "linked.txt").symlink_to(source / "README.md")
    with pytest.raises(ValueError, match="symbolic links"):
        manager.install(source)

    mismatch = write_source(tmp_path / "mismatch" / "writer", agent_id="other")
    with pytest.raises(ValueError, match="directory name"):
        manager.install(mismatch)

    manager.create(AgentSpec(id="Writer", name="Writer"))
    with pytest.raises(ValueError, match="case-insensitive"):
        manager.create(AgentSpec(id="writer", name="Second"))
    with pytest.raises(ValueError, match="agent.id"):
        manager.inspect("../Writer")


def test_agent_package_rejects_protected_uninstall_and_installed_symlink(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home, protected_ids={"default"})
    manager.create(AgentSpec(id="default", name="Default"))
    with pytest.raises(ValueError, match="protected"):
        manager.uninstall("default")

    outside = write_source(tmp_path / "outside" / "linked", agent_id="linked")
    (home.agents_dir / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        manager.inspect("linked")


def test_read_retires_a_user_record_the_contract_rejects(tmp_path: Path) -> None:
    """A record from a retired declaration format leaves the user directory."""

    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    system_agents = tmp_path / "system-agents"
    write_source(system_agents / "retired", agent_id="retired", name="Retired")
    manager = AgentPackageManager(home, system_agents_dir=system_agents)
    manager.create(AgentSpec(id="writer", name="Writer"))
    broken = home.agents_dir / "retired"
    broken.mkdir()
    (broken / "agent.yaml").write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  id: retired\n"
        "  version: 1.0.0\n"
        "  name: Retired\n"
        "  hooks: []\n",
        encoding="utf-8",
    )

    # Reading retires the rejected record instead of guessing its format, so
    # the packaged declaration of the same Agent becomes the effective one.
    listed = {item.id: item for item in manager.list()}
    assert sorted(listed) == ["retired", "writer"]
    assert listed["retired"].root == (system_agents / "retired").resolve()
    assert manager.inspect("retired") == listed["retired"]
    assert [path.name for path in home.staging_dir.glob("retired-agent-retired-*")]

    orphan = home.agents_dir / "orphan"
    orphan.mkdir()
    (orphan / "agent.yaml").write_text(
        "schema_version: 1\nagent:\n  id: orphan\n  version: 1.0.0\n"
        "  name: Orphan\n  model: legacy-model-name\n",
        encoding="utf-8",
    )
    assert manager.inspect("orphan") is None
    assert sorted(item.id for item in manager.list()) == ["retired", "writer"]
    assert [path.name for path in home.staging_dir.glob("retired-agent-orphan-*")]

    assert manager.is_system("retired") is True
    assert manager.avatar_path("retired") is None
    updated = manager.update(AgentSpec(id="retired", name="Edited"))
    assert updated.manifest.spec.name == "Edited"
    assert updated.root == (home.agents_dir / "retired").resolve()
    assert manager.is_system("retired") is False
    assert manager.avatar_path("retired") is None


def test_edit_retires_an_unloadable_record_without_a_packaged_copy(tmp_path: Path) -> None:
    """An unreadable record is dropped, never preserved as a dead format."""

    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    broken = home.agents_dir / "orphan"
    broken.mkdir()
    (broken / "agent.yaml").write_text(
        "schema_version: 1\nagent:\n  id: orphan\n  version: 1.0.0\n"
        "  name: Orphan\n  model: legacy-model-name\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unloadable"):
        manager.update(AgentSpec(id="orphan", name="Orphan"))


def test_packaged_declaration_must_load(tmp_path: Path) -> None:
    """The publisher reconciles the profile, so a stale declaration is an error."""

    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    system_agents = tmp_path / "system-agents"
    write_source(system_agents / "current", agent_id="current", name="Current")
    stale = system_agents / "stale"
    stale.mkdir()
    (stale / "agent.yaml").write_text(
        "schema_version: 1\n"
        "agent:\n"
        "  id: stale\n"
        "  version: 1.0.0\n"
        "  name: Stale\n"
        "  hooks: []\n",
        encoding="utf-8",
    )
    manager = AgentPackageManager(home, system_agents_dir=system_agents)

    # Until the publisher reconciles the directory, every read names the file
    # instead of guessing: no path counts a stale declaration as absent.
    with pytest.raises(ValueError, match="unsupported agent fields"):
        manager.list()
    with pytest.raises(ValueError, match="unsupported agent fields"):
        manager.inspect("stale")
    with pytest.raises(ValueError, match="unsupported agent fields"):
        manager.inspect("current")

    shutil.rmtree(stale)
    assert manager.inspect("current") is not None


def test_control_startup_retires_unloadable_user_records(tmp_path: Path) -> None:
    """A record from a retired declaration format leaves the user directory."""

    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    system_agents = tmp_path / "system-agents"
    write_source(system_agents / "retired", agent_id="retired", name="Retired")
    manager = AgentPackageManager(home, system_agents_dir=system_agents)
    manager.create(AgentSpec(id="writer", name="Writer"))
    for agent_id in ("retired", "orphan"):
        broken = home.agents_dir / agent_id
        broken.mkdir()
        (broken / "agent.yaml").write_text(
            "schema_version: 1\n"
            "agent:\n"
            f"  id: {agent_id}\n"
            "  version: 1.0.0\n"
            f"  name: {agent_id}\n"
            "  hooks: []\n",
            encoding="utf-8",
        )

    assert manager.retire_unloadable_records() == ("orphan", "retired")
    assert manager.retire_unloadable_records() == ()

    assert sorted(path.name for path in home.agents_dir.iterdir()) == ["writer"]
    assert sorted(
        path.name.rsplit("-", 1)[0]
        for path in home.staging_dir.glob("retired-agent-*")
    ) == ["retired-agent-orphan", "retired-agent-retired"]
    # The packaged declaration of the same Agent is effective again.
    assert manager.is_system("retired") is True
    assert manager.inspect("retired").root == (system_agents / "retired").resolve()


def test_agent_package_allows_protected_declaration_edit_but_rejects_lifecycle_mutation(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home, protected_ids={"default"})
    manager.create(AgentSpec(id="default", name="Default"))

    updated = manager.update(AgentSpec(id="default", name="Changed"))
    assert updated.manifest.spec.name == "Changed"
    with pytest.raises(ValueError, match="protected"):
        manager.install(
            write_source(
                tmp_path / "upgrade" / "default",
                "2.0.0",
                agent_id="default",
            ),
            upgrade=True,
        )
    with pytest.raises(ValueError, match="protected"):
        manager.uninstall("default")

    assert manager.inspect("default").manifest.spec.name == "Changed"


def test_agent_package_uses_staged_snapshot_as_version_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    manager.install(write_source(tmp_path / "v1" / "writer", "1.0.0"))
    source = write_source(tmp_path / "v2" / "writer", "2.0.0")
    original_copy = AgentPackageManager._copy_source

    def mutate_before_copy(source_root: Path, destination: Path) -> None:
        downgraded = AgentManifest(
            schema_version=1,
            version="0.5.0",
            spec=AgentSpec(id="writer", name="Downgraded"),
        )
        (source_root / "agent.yaml").write_text(
            downgraded.to_yaml(), encoding="utf-8"
        )
        original_copy(source_root, destination)

    monkeypatch.setattr(
        AgentPackageManager,
        "_copy_source",
        staticmethod(mutate_before_copy),
    )

    with pytest.raises(ValueError, match="higher"):
        manager.install(source, upgrade=True)

    installed = manager.inspect("writer")
    assert installed.version == "1.0.0"
    assert installed.manifest.spec.name == "Writer"


def test_agent_package_upgrade_uses_atomic_directory_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    manager.install(write_source(tmp_path / "v1" / "writer", "1.0.0"))
    exchanges: list[tuple[Path, Path]] = []
    from bianbu_agent.agents import packages as package_module

    original_exchange = package_module._rename_exchange

    def record_exchange(source: Path, destination: Path) -> None:
        exchanges.append((source, destination))
        original_exchange(source, destination)

    monkeypatch.setattr(package_module, "_rename_exchange", record_exchange)

    manager.install(
        write_source(tmp_path / "v2" / "writer", "2.0.0"),
        upgrade=True,
    )

    assert len(exchanges) == 1
    assert exchanges[0][1] == home.agents_dir / "writer"
    assert manager.inspect("writer").version == "2.0.0"


def test_agent_package_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    source = write_source(tmp_path / "source" / "writer")
    os.mkfifo(source / "blocking.fifo")

    with pytest.raises(ValueError, match="unsupported"):
        manager.install(source)


def test_agent_package_installs_read_only_asset_directory(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    source = write_source(tmp_path / "source" / "writer")
    assets = source / "assets"
    assets.mkdir()
    (assets / "avatar.txt").write_text("avatar", encoding="utf-8")
    assets.chmod(0o555)

    installed = manager.install(source)

    assert (installed.root / "assets" / "avatar.txt").read_text() == "avatar"
    assert stat.S_IMODE((installed.root / "assets").stat().st_mode) == 0o555


def test_agent_package_upgrade_rolls_back_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    manager.install(write_source(tmp_path / "v1" / "writer", "1.0.0"))
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
        manager.install(
            write_source(tmp_path / "v2" / "writer", "2.0.0"),
            upgrade=True,
        )

    assert manager.inspect("writer").version == "1.0.0"
    assert not any(home.staging_dir.iterdir())


def test_agent_package_retains_old_tree_when_exchange_rollback_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
    manager.install(write_source(tmp_path / "v1" / "writer", "1.0.0"))
    from bianbu_agent.agents import packages as package_module

    original_exchange = package_module._rename_exchange
    exchange_calls = 0

    def fail_rollback(source: Path, destination: Path) -> None:
        nonlocal exchange_calls
        exchange_calls += 1
        if exchange_calls == 2:
            raise OSError("injected exchange rollback failure")
        original_exchange(source, destination)

    original_fsync = package_module._fsync_directory
    fsync_failed = False

    def fail_fsync_once(directory: Path) -> None:
        nonlocal fsync_failed
        if directory == home.agents_dir and not fsync_failed:
            fsync_failed = True
            raise OSError("injected directory fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(package_module, "_rename_exchange", fail_rollback)
    monkeypatch.setattr(package_module, "_fsync_directory", fail_fsync_once)

    with pytest.raises(OSError, match="rollback"):
        manager.install(
            write_source(tmp_path / "v2" / "writer", "2.0.0"),
            upgrade=True,
        )

    assert manager.inspect("writer").version == "2.0.0"
    retained = list(home.staging_dir.iterdir())
    assert len(retained) == 1
    assert AgentManifest.from_path(retained[0] / "agent.yaml").version == "1.0.0"


@pytest.mark.parametrize("operation", ["install", "create"])
def test_agent_package_new_publication_rolls_back_after_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent").prepare()
    manager = AgentPackageManager(home)
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
        if operation == "install":
            manager.install(write_source(tmp_path / "source" / "writer"))
        else:
            manager.create(AgentSpec(id="writer", name="Writer"))

    assert manager.inspect("writer") is None
    assert not any(home.staging_dir.iterdir())
