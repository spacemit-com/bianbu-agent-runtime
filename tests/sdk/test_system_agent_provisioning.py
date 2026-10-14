import os
from pathlib import Path

import pytest
import yaml

from bianbu_agent.agents import system_paths
from bianbu_agent import provisioning


def test_system_publisher_requires_administrator(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioning.os, "geteuid", lambda: 1001)
    with pytest.raises(PermissionError):
        provisioning.publish_system_starter(tmp_path, tmp_path / "template", "com.bianbu.012345abcdef")


def test_system_profile_key_cannot_be_redirected_by_user_symlink(tmp_path):
    own_profile = tmp_path / "own-profile"
    other_profile = tmp_path / "other-profile"
    before = system_paths.system_agent_directory(own_profile)
    other_profile.mkdir()
    own_profile.symlink_to(other_profile, target_is_directory=True)
    assert system_paths.system_agent_directory(own_profile) == before
    assert system_paths.system_agent_directory(own_profile) != system_paths.system_agent_directory(other_profile)


def test_system_publisher_preserves_id_and_leaves_sessions_untouched(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioning, "SYSTEM_OWNER_UID", os.geteuid())
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    data = tmp_path / "user-data"
    sessions = data / "sessions"
    sessions.mkdir(parents=True)
    session = sessions / "keep.txt"
    session.write_text("keep", encoding="utf-8")
    template = tmp_path / "starter.yaml"
    template.write_text("schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n  name: BibitEdgeAssistant\n", encoding="utf-8")
    agent_id = "com.bianbu.012345abcdef"
    assert provisioning.publish_system_starter(data, template, agent_id) == agent_id
    manifest = system_paths.system_agent_directory(data) / agent_id / "agent.yaml"
    assert yaml.safe_load(manifest.read_text())["agent"]["id"] == agent_id
    assert manifest.stat().st_mode & 0o777 == 0o644
    assert manifest.parent.stat().st_mode & 0o777 == 0o755
    assert manifest.stat().st_uid == data.stat().st_uid
    assert manifest.parent.parent.stat().st_mode & 0o777 == 0o755
    assert session.read_text() == "keep"
    assert not (data / "agents").exists()

    # Package publication always replaces the system template; user edits live elsewhere.
    edited = yaml.safe_load(manifest.read_text())
    edited["agent"]["description"] = "administrator customization"
    manifest.write_text(yaml.safe_dump(edited))
    provisioning.publish_system_starter(data, template, agent_id)
    assert "description" not in yaml.safe_load(manifest.read_text())["agent"]
    recommendation = yaml.safe_load(template.read_text())
    recommendation["agent"]["description"] = "new recommendation"
    template.write_text(yaml.safe_dump(recommendation))
    provisioning.publish_system_starter(data, template, agent_id)
    assert yaml.safe_load(manifest.read_text())["agent"]["description"] == "new recommendation"
    assert provisioning.system_starter_id(data) == agent_id
    assert session.read_text() == "keep"


def test_source_launcher_republishes_and_reconciles_the_profile(tmp_path, monkeypatch):
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "provision-starter-agent.py"
    spec = importlib.util.spec_from_file_location("source_starter", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(provisioning.os, "geteuid", lambda: 1001)
    data = tmp_path / "user-data"
    template = tmp_path / "starter.yaml"
    template.write_text("schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n  name: BibitEdgeAssistant\n")
    monkeypatch.setattr(sys, "argv", [str(script), "--data-dir", str(data), "--template", str(template)])
    stale_id = "com.bianbu.012345abcdef"

    def publish_at_privilege_boundary(command, **kwargs):
        assert command[:2] == ["sudo", "--"]
        assert kwargs["check"] is True
        agent_id = command[command.index("--agent-id") + 1]
        directory = system_paths.system_agent_directory(data)
        # A declaration a retired Runtime published here must not survive a start.
        (directory / stale_id).mkdir(parents=True, mode=0o755, exist_ok=True)
        for path in (directory, directory.parent, directory.parent.parent):
            os.chmod(path, 0o755)
        (directory / stale_id / "agent.yaml").write_text(
            "schema_version: 1\nagent:\n  id: " + stale_id + "\n  version: 1.0.0\n"
            "  name: Stale\n  hooks: []\n",
            encoding="utf-8",
        )
        provisioning.publish_system_starter(data, template, agent_id)

    # Match real ownership validation while simulating sudo as an external boundary.
    monkeypatch.setattr(provisioning, "SYSTEM_OWNER_UID", Path(tmp_path).stat().st_uid)
    actual_uid = Path(tmp_path).stat().st_uid

    def privileged_publish(command, **kwargs):
        with monkeypatch.context() as privileged:
            privileged.setattr(provisioning.os, "geteuid", lambda: actual_uid)
            publish_at_privilege_boundary(command, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", privileged_publish)
    assert module.main() == 0
    agent_id = provisioning.system_starter_id(data)
    assert agent_id is not None
    directory = system_paths.system_agent_directory(data)
    assert sorted(item.name for item in directory.iterdir()) == [agent_id]
    assert not (data / "backups" / "system-agent-migration").exists()

    # Every start republishes as root, so a stale declaration never has to be
    # read past: the profile always holds exactly the shipped declaration.
    assert module.main() == 0
    assert provisioning.system_starter_id(data) == agent_id
    assert sorted(item.name for item in directory.iterdir()) == [agent_id]


def test_system_owner_migration_rejects_links_without_changing_target(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioning, "SYSTEM_OWNER_UID", os.geteuid())
    directory = tmp_path / "agents"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    outside.chmod(0o644)
    (directory / "linked").symlink_to(outside)
    with pytest.raises(ValueError, match="links"):
        provisioning._secure_system_agents(directory, os.getuid())
    assert outside.read_text() == "untouched"
    assert outside.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(os.geteuid() != 0, reason="requires root to verify real ownership handoff")
def test_root_publisher_keeps_system_owned_and_user_can_save_override(tmp_path, monkeypatch):
    import pwd
    from dataclasses import replace
    from bianbu_agent.agents import AgentPackageManager
    from bianbu_agent.home import AgentHome

    account = pwd.getpwnam(os.environ.get("BIANBU_TEST_ACCOUNT", "nobody"))
    # pytest ancestors need traversal by the test account, not write access.
    for path in (tmp_path, tmp_path.parent, tmp_path.parent.parent):
        path.chmod(0o755)
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    data = tmp_path / "data"
    data.mkdir()
    os.chown(data, account.pw_uid, account.pw_gid)
    template = tmp_path / "starter.yaml"
    template.write_text("schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n  name: BibitEdgeAssistant\n")
    agent_id = "com.bianbu.012345abcdef"
    directory = system_paths.system_agent_directory(data)
    legacy = directory / agent_id
    legacy.mkdir(parents=True, mode=0o755)
    note = legacy / "personal-note.txt"
    note.write_text("keep existing resource")
    other = directory.parent.parent / "another-profile"
    other.mkdir(mode=0o700)
    provisioning.publish_system_starter(data, template, agent_id)
    manifest = directory / agent_id / "agent.yaml"
    assert directory.parent.stat().st_uid == 0
    assert directory.stat().st_uid == 0
    assert manifest.stat().st_uid == 0
    assert directory.stat().st_mode & 0o777 == 0o755
    assert note.stat().st_uid == 0
    assert note.read_text() == "keep existing resource"
    assert other.stat().st_uid == 0
    assert other.stat().st_mode & 0o777 == 0o700
    pid = os.fork()
    if pid == 0:
        try:
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            packages = AgentPackageManager(AgentHome(data))
            spec = packages.inspect(agent_id).manifest.spec
            packages.update(replace(spec, description="saved by profile user"))
        except BaseException:
            os._exit(1)
        os._exit(0)
    assert os.waitpid(pid, 0)[1] == 0
    provisioning.publish_system_starter(data, template, agent_id)
    assert "description" not in yaml.safe_load(manifest.read_text())["agent"]
    assert yaml.safe_load((data / "agents" / agent_id / "agent.yaml").read_text())["agent"]["description"] == "saved by profile user"


def test_source_launcher_fresh_install_does_not_create_user_agent(tmp_path, monkeypatch):
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "provision-starter-agent.py"
    spec = importlib.util.spec_from_file_location("fresh_source_starter", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    data = tmp_path / "user-data"
    template = tmp_path / "starter.yaml"
    monkeypatch.setattr(sys, "argv", [str(script), "--data-dir", str(data), "--template", str(template)])
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: calls.append(command))
    assert module.main() == 0
    assert len(calls) == 1
    assert provisioning.AGENT_ID_PATTERN.fullmatch(calls[0][calls[0].index("--agent-id") + 1])
    assert not (data / "agents").exists()
    assert not (data / "backups").exists()


def test_source_launcher_uses_config_data_dir(tmp_path, monkeypatch):
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "provision-starter-agent.py"
    spec = importlib.util.spec_from_file_location("configured_source_starter", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    data = tmp_path / "configured-data"
    config = tmp_path / "development.yaml"
    config.write_text(f"data_dir: {data}\n", encoding="utf-8")
    template = tmp_path / "starter.yaml"
    template.write_text("schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n  name: BibitEdgeAssistant\n")
    monkeypatch.setattr(sys, "argv", [str(script), "--config", str(config), "--template", str(template)])
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: calls.append(command))

    assert module.main() == 0

    assert data.is_dir()
    assert calls[0][calls[0].index("--data-dir") + 1] == str(data)
    assert any(
        argument.endswith("scripts/provision-system-agent.py") for argument in calls[0]
    )
    assert not (data / "agents").exists()


def test_source_launcher_requires_one_data_location(tmp_path, monkeypatch):
    import importlib.util
    import sys

    script = Path(__file__).resolve().parents[2] / "scripts" / "provision-starter-agent.py"
    spec = importlib.util.spec_from_file_location("invalid_source_starter", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    template = tmp_path / "starter.yaml"
    monkeypatch.setattr(sys, "argv", [str(script), "--template", str(template)])

    with pytest.raises(SystemExit) as missing:
        module.main()
    assert missing.value.code == 2

    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(script),
            "--config",
            str(tmp_path / "development.yaml"),
            "--data-dir",
            str(tmp_path / "data"),
            "--template",
            str(template),
        ],
    )
    with pytest.raises(SystemExit) as both:
        module.main()
    assert both.value.code == 2


def test_publish_removes_a_declaration_a_retired_runtime_left_behind(tmp_path, monkeypatch):
    """The profile root holds exactly what the current Runtime ships.

    A profile directory outlives the Runtime that published it, and a record
    written by a retired generation used to stop startup.  The publisher now
    reconciles the directory instead of leaving a read path to guess.
    """

    monkeypatch.setattr(provisioning, 'SYSTEM_OWNER_UID', os.geteuid())
    monkeypatch.setattr(system_paths, 'SYSTEM_PROFILE_ROOT', tmp_path / 'profiles')
    data = tmp_path / 'user-data'
    data.mkdir()
    template = tmp_path / 'starter.yaml'
    template.write_text('schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n  name: BibitEdgeAssistant\n')
    agent_id = 'com.bianbu.012345abcdef'
    system = system_paths.system_agent_directory(data)
    stale = system / 'com.bianbu.fedcba543210'
    stale.mkdir(parents=True, mode=0o755)
    for path in (system, system.parent, system.parent.parent):
        os.chmod(path, 0o755)
    (stale / 'agent.yaml').write_text(
        'schema_version: 1\nagent:\n  id: com.bianbu.fedcba543210\n'
        '  version: 1.0.0\n  name: Stale\n  hooks: []\n',
        encoding='utf-8',
    )
    (stale / 'personal-note.txt').write_text('left by an older generation')

    provisioning.publish_system_starter(data, template, agent_id)

    assert sorted(item.name for item in system.iterdir()) == [agent_id]
    assert not (system.parent / 'legacy-user-agents').exists()
    assert not (data / 'agents').exists()


def test_system_publication_root_stays_readable_for_the_runtime_user():
    """The publication root cannot live below the private Runtime data dir.

    A regression here is invisible to root-run tests and only shows up as a
    Runtime that cannot start: /var/lib/bianbu-agent-runtime is 0750
    bianbu-runtime:bianbu-runtime, so nothing below it is readable by the
    Runtime user no matter how the declaration itself is chmodded.
    """

    root = system_paths.SYSTEM_PROFILE_ROOT
    assert root == Path("/var/lib/bianbu-agent-profiles")
    assert not root.is_relative_to(Path("/var/lib/bianbu-agent-runtime"))
    # /var/lib itself is root-owned 0755; the package creates the root below it.
    assert root.parent == Path("/var/lib")


def test_publisher_reconciles_a_restricted_publication_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provisioning, "SYSTEM_OWNER_UID", os.geteuid())
    root = tmp_path / "bianbu-agent-profiles"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", root)
    data = tmp_path / "user-data"
    data.mkdir()
    template = tmp_path / "starter.yaml"
    template.write_text(
        "schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n"
        "  name: BibitEdgeAssistant\n",
        encoding="utf-8",
    )
    agent_id = "com.bianbu.012345abcdef"

    assert provisioning.publish_system_starter(data, template, agent_id) == agent_id

    assert root.stat().st_mode & 0o777 == 0o755
    assert provisioning.system_starter_id(data) == agent_id

def test_publisher_leaves_a_readable_package_despite_a_restrictive_umask(tmp_path, monkeypatch):
    """The packaged unit runs with UMask=0077, which masks mkdir() modes.

    Only root publishes here, but the Runtime user reads the declaration, so a
    package directory left at 0700 by the umask shows up as a missing
    agent.yaml at Runtime startup.
    """

    monkeypatch.setattr(provisioning, "SYSTEM_OWNER_UID", os.geteuid())
    monkeypatch.setattr(system_paths, "SYSTEM_PROFILE_ROOT", tmp_path / "profiles")
    data = tmp_path / "user-data"
    data.mkdir()
    template = tmp_path / "starter.yaml"
    template.write_text(
        "schema_version: 1\nagent:\n  id: starter-template\n  version: 1.0.0\n"
        "  name: BibitEdgeAssistant\n",
        encoding="utf-8",
    )
    agent_id = "com.bianbu.012345abcdef"

    previous_umask = os.umask(0o077)
    try:
        assert provisioning.publish_system_starter(data, template, agent_id) == agent_id
    finally:
        os.umask(previous_umask)

    directory = system_paths.system_agent_directory(data)
    package = directory / agent_id
    assert package.stat().st_mode & 0o777 == 0o755
    assert (package / "agent.yaml").stat().st_mode & 0o777 == 0o644
    assert (directory.parent / "starter-agent-id").stat().st_mode & 0o777 == 0o644

