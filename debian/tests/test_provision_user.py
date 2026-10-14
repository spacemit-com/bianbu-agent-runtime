from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "debian" / "provision-user.py"
MANAGED_KINDS = ("agents", "skills", "plugins", "mcps", "models")


def _load_module():
    assert MODULE_PATH.is_file(), "the package user provisioner is not implemented"
    spec = importlib.util.spec_from_file_location("provision_user", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "default-profile"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        """data_dir: ~/.bianbu-agent
service:
  title: Bianbu Agent
  host: 0.0.0.0
  port: 9899
language: zh
require_file_backed_capabilities: true
plugin_stop_timeout_seconds: 5.0
allow_unmanaged_capabilities: false
security:
  required: true
  securityd_uds: /run/bianbu/securityd.sock
  client_dir: /usr/lib/bianbu-securityd/client
runtime:
  control:
    execution: {requested_mode: host}
  worker:
    execution: {requested_mode: host}
    entrypoint: [/usr/bin/bianbu-agent, --role, agent-worker, --config, /home/agent/config.yaml, worker]
""",
        encoding="utf-8",
    )
    (profile / "starter-agent.yaml").write_text(
        """schema_version: 1
agent:
  id: starter-template
  version: 1.0.0
  name: BianbuAgent
  name_zh: Bianbu智能助手
  description: Starter local Bianbu agent
  soul: Help the user complete practical work safely.
  model:
    provider: deepseek
    name: deepseek-flash
  tools: ['*']
  skills: ['*']
  mcps: ['*']
""",
        encoding="utf-8",
    )
    for kind in MANAGED_KINDS:
        component = profile / kind / f"package-{kind}"
        component.mkdir(parents=True)
        (component / "payload.txt").write_text(f"{kind}-v1\n", encoding="utf-8")
    return profile


def _prepared(tmp_path):
    provisioner = _load_module()
    profile = _profile(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    result = provisioner.provision(profile, home, uid=1001)
    return provisioner, profile, home, home / ".bianbu-agent", result


def test_first_use_keeps_preinstalled_resources_out_of_user_home(tmp_path):
    provisioner, profile, home, root, first = _prepared(tmp_path)
    assert first.updated
    config = yaml.safe_load((root / "config.yaml").read_text())
    assert config["service"]["port"] == 9900
    assert (root / "config.yaml").stat().st_mode & 0o777 == 0o600
    state = json.loads((root / ".managed-profile.json").read_text())
    assert state["schema_version"] == 1
    assert state["starter_agent_id"].startswith("com.bianbu.")
    assert all(not ids for ids in state["managed"].values())
    for kind in MANAGED_KINDS:
        # Extension roots belong to the manager that writes them: a profile
        # that ships nothing for a kind keeps no empty directory behind.
        target = root / kind
        assert not target.exists() or not list(target.iterdir())
    assert not provisioner.provision(profile, home, uid=1001).updated


def test_upgrade_preserves_user_config_agents_and_resources(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    (root / "config.yaml").write_text("user-config: true\n")
    for kind in MANAGED_KINDS:
        target = root / kind / "user-created"
        target.mkdir(parents=True)
        (target / "payload.txt").write_text("keep")
    (profile / "config.yaml").write_text((profile / "config.yaml").read_text() + "# upgrade\n")
    assert provisioner.provision(profile, home, uid=1001).updated
    assert (root / "config.yaml").read_text() == "user-config: true\n"
    for kind in MANAGED_KINDS:
        assert (root / kind / "user-created/payload.txt").read_text() == "keep"


def test_upgrade_retires_the_automatic_execution_mode(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    config = yaml.safe_load((root / "config.yaml").read_text())
    config["runtime"]["control"]["execution"]["requested_mode"] = "auto"
    config["runtime"]["worker"]["execution"]["requested_mode"] = "auto"
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (profile / "config.yaml").write_text((profile / "config.yaml").read_text() + "# upgrade\n")
    assert provisioner.provision(profile, home, uid=1001).updated
    upgraded = yaml.safe_load((root / "config.yaml").read_text())
    assert upgraded["runtime"]["control"]["execution"]["requested_mode"] == "host"
    assert upgraded["runtime"]["worker"]["execution"]["requested_mode"] == "host"
    # The migration touches the execution default only.
    assert upgraded["service"]["port"] == 9900
    assert upgraded["language"] == "zh"
    assert not provisioner.provision(profile, home, uid=1001).updated


def test_upgrade_keeps_an_explicit_execution_mode(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    config = yaml.safe_load((root / "config.yaml").read_text())
    config["runtime"]["worker"]["execution"]["requested_mode"] = "sandbox"
    (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    (profile / "config.yaml").write_text((profile / "config.yaml").read_text() + "# upgrade\n")
    assert provisioner.provision(profile, home, uid=1001).updated
    upgraded = yaml.safe_load((root / "config.yaml").read_text())
    assert upgraded["runtime"]["worker"]["execution"]["requested_mode"] == "sandbox"


def test_legacy_managed_resources_are_backed_up_and_not_reseeded(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    state_path = root / ".managed-profile.json"
    state = json.loads(state_path.read_text())
    state["profile_digest"] = "old"
    for kind in MANAGED_KINDS:
        target = root / kind / "old-default"
        target.mkdir(parents=True)
        (target / "payload.txt").write_text("modified")
        state["managed"][kind] = ["old-default"]
    state_path.write_text(json.dumps(state))
    result = provisioner.provision(profile, home, uid=1001)
    for kind in MANAGED_KINDS:
        assert not (root / kind / "old-default").exists()
        assert (root / "backups/package-upgrade" / result.profile_digest[:12] / kind / "old-default/payload.txt").read_text() == "modified"
        assert not (root / kind / f"package-{kind}").exists()


def _publish_system_starter(provisioner, home, agent_id, monkeypatch, declaration="provider: deepseek\n"):
    """Stand in for the root-side publisher: a system starter plus its marker."""

    agents = home / "system-agents"
    directory = agents / agent_id
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / "agent.yaml"
    manifest.write_text(declaration, encoding="utf-8")
    monkeypatch.setattr(provisioner, "system_starter_id", lambda _: agent_id)
    monkeypatch.setattr(provisioner, "system_agent_directory", lambda _: agents)
    return manifest


def test_legacy_starter_waits_for_system_publication_then_yields_to_it(tmp_path, monkeypatch):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    state_path = root / ".managed-profile.json"
    state = json.loads(state_path.read_text())
    state["profile_digest"] = "old"
    state_path.write_text(json.dumps(state))
    agent_id = state["starter_agent_id"]
    package = root / "agents" / agent_id
    package.mkdir(parents=True)
    (package / "agent.yaml").write_text("personal old declaration")
    session = root / "sessions/history.json"
    session.write_text(json.dumps({"agent_id": agent_id, "messages": ["keep"]}))
    before = session.read_bytes()
    provisioner.provision(profile, home, uid=1001)
    assert package.exists(), "prepare must leave the usable starter until privileged publication succeeds"
    _publish_system_starter(provisioner, home, agent_id, monkeypatch)
    assert provisioner.provision(profile, home, uid=1001).updated
    assert not package.exists()
    assert len(list((root / "backups/system-agent-migration").glob(f"*/{agent_id}/agent.yaml"))) == 1
    assert session.read_bytes() == before


def test_a_user_edit_survives_until_the_next_published_default(tmp_path, monkeypatch):
    """One overwrite per shipped default, not one per start.

    The preset expert's declaration is ours, and the copy a user edits is the
    override the Agent store reads first -- so a stale copy would keep every
    later publication from reaching the device, while replacing it on each
    start would throw the user's edit away on every restart.
    """

    provisioner, profile, home, root, _ = _prepared(tmp_path)
    agent_id = json.loads((root / ".managed-profile.json").read_text())["starter_agent_id"]
    published = _publish_system_starter(provisioner, home, agent_id, monkeypatch)
    assert provisioner.provision(profile, home, uid=1001).updated, "the first publication is recorded"

    package = root / "agents" / agent_id
    package.mkdir(parents=True)
    (package / "agent.yaml").write_text("my edit\n")
    other = root / "agents" / "user-made"
    other.mkdir(parents=True)
    (other / "agent.yaml").write_text("mine\n")

    for _ in range(3):
        assert not provisioner.provision(profile, home, uid=1001).updated
    assert (package / "agent.yaml").read_text() == "my edit\n"

    published.write_text("provider: deepseek-v4-pro\n", encoding="utf-8")

    assert provisioner.provision(profile, home, uid=1001).updated
    assert not package.exists()
    assert len(list((root / "backups/system-agent-migration").glob(f"*/{agent_id}/agent.yaml"))) == 1
    # Only the starter's ID is ours to replace; another Agent the user made is
    # not part of this.
    assert (other / "agent.yaml").read_text() == "mine\n"


@pytest.mark.parametrize("legacy_id", ["custom-bibit", "com.bianbu.111111111111"])
def test_unmanaged_bibit_and_sessions_are_preserved(tmp_path, monkeypatch, legacy_id):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    package = root / "agents" / legacy_id
    package.mkdir(parents=True)
    (package / "agent.yaml").write_text(yaml.safe_dump({"agent": {"id": legacy_id, "name": "BibitEdgeAssistant"}}))
    session = root / "sessions/history.json"
    session.write_text(json.dumps({"agent_id": legacy_id}))
    before = session.read_bytes()
    monkeypatch.setattr(provisioner, "system_starter_id", lambda _: "com.bianbu.222222222222")
    provisioner.provision(profile, home, uid=1001)
    assert package.exists()
    assert session.read_bytes() == before


def test_retired_intent_mcp_is_removed_and_backed_up(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    old = root / "mcps/bibit-intent"
    old.mkdir(parents=True)
    (old / "mcp.json").write_text(json.dumps({"transport": "stdio", "command": "custom"}))
    state_path = root / ".managed-profile.json"
    state = json.loads(state_path.read_text())
    state["managed"]["mcps"] = ["bibit-intent"]
    state["profile_digest"] = "old"
    state_path.write_text(json.dumps(state))
    result = provisioner.provision(profile, home, uid=1001)
    assert not old.exists()
    backed_up = (
        root / "backups/package-upgrade" / result.profile_digest[:12]
        / "mcps" / "bibit-intent" / "mcp.json"
    )
    assert json.loads(backed_up.read_text()) == {"transport": "stdio", "command": "custom"}
    assert not (root / "plugins/bianbu.bibit.intent").exists()


def test_managed_symlink_rejected_without_touching_external_target(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (root / "skills").mkdir(parents=True)
    (root / "skills/old").symlink_to(outside)
    state_path = root / ".managed-profile.json"
    state = json.loads(state_path.read_text())
    state["profile_digest"] = "old"
    state["managed"]["skills"] = ["old"]
    state_path.write_text(json.dumps(state))
    with pytest.raises(provisioner.ProvisionError, match="symbolic link"):
        provisioner.provision(profile, home, uid=1001)
    assert (outside / "keep").read_text() == "keep"


def test_tampered_managed_state_cannot_delete_outside_profile(tmp_path: Path) -> None:
    provisioner = _load_module()
    profile = _profile(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    result = provisioner.provision(profile, home, uid=1000)
    root = home / ".bianbu-agent"
    outside = home / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep\n", encoding="utf-8")
    state_path = root / ".managed-profile.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["profile_digest"] = result.profile_digest
    state["managed"]["tools"].append("../../outside")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(provisioner.ProvisionError, match="component ID"):
        provisioner.provision(profile, home, uid=1000)

    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_provisioning_does_not_import_obsolete_profile_data(tmp_path: Path) -> None:
    provisioner = _load_module()
    profile = _profile(tmp_path)
    home = tmp_path / "home"
    legacy = home / ".local" / "share" / "bianbu-agent"
    legacy.mkdir(parents=True)
    (legacy / "private.txt").write_text("leave me here\n", encoding="utf-8")

    provisioner.provision(profile, home, uid=1001)

    assert not (home / ".bianbu-agent" / "private.txt").exists()
    assert (legacy / "private.txt").read_text(encoding="utf-8") == "leave me here\n"


def test_obsolete_empty_directories_are_dropped(tmp_path: Path) -> None:
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    for name in ("runs", "tools", "session-logs", "cache", "hooks"):
        (root / name).mkdir()
    (profile / "config.yaml").write_text(
        (profile / "config.yaml").read_text(encoding="utf-8") + "# upgrade\n",
        encoding="utf-8",
    )

    provisioner.provision(profile, home, uid=1001)

    for name in ("runs", "tools", "session-logs", "cache", "hooks"):
        assert not (root / name).exists()


def test_obsolete_directories_with_content_are_preserved(tmp_path: Path) -> None:
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    (root / "session-logs").mkdir()
    (root / "session-logs" / "old.jsonl.zstd").write_text("keep", encoding="utf-8")

    provisioner.provision(profile, home, uid=1001)

    assert (root / "session-logs" / "old.jsonl.zstd").read_text(encoding="utf-8") == "keep"


def test_retired_migration_lock_file_is_dropped(tmp_path: Path) -> None:
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    worker = root / "runtime" / "worker"
    worker.mkdir(parents=True)
    (root / ".agent-json-migration.lock").write_text("", encoding="utf-8")
    (worker / ".agent-json-migration.lock").write_text("", encoding="utf-8")
    (root / ".agent-json-migration.lock.keep").write_text("notes\n", encoding="utf-8")

    provisioner.provision(profile, home, uid=1001)

    assert not (root / ".agent-json-migration.lock").exists()
    assert not (worker / ".agent-json-migration.lock").exists()
    assert (root / ".agent-json-migration.lock.keep").read_text(encoding="utf-8") == "notes\n"


def test_retired_lock_file_with_content_is_preserved(tmp_path: Path) -> None:
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    (root / ".agent-json-migration.lock").write_text("busy", encoding="utf-8")

    provisioner.provision(profile, home, uid=1001)

    assert (root / ".agent-json-migration.lock").read_text(encoding="utf-8") == "busy"


def test_retired_root_of_empty_directories_is_dropped(tmp_path: Path) -> None:
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    (root / "cache" / "agent-json-migration").mkdir(parents=True)
    (root / "runs" / "empty").mkdir(parents=True)
    (root / "hooks" / "example.check").mkdir(parents=True)

    provisioner.provision(profile, home, uid=1001)

    assert not (root / "cache").exists()
    assert not (root / "runs").exists()
    assert not (root / "hooks").exists()


def test_retired_root_keeps_the_branch_that_holds_content(tmp_path: Path) -> None:
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    (root / "cache" / "agent-store").mkdir(parents=True)
    (root / "cache" / "agent-store" / "blob").write_text("x", encoding="utf-8")
    (root / "cache" / "empty").mkdir()

    provisioner.provision(profile, home, uid=1001)

    assert (root / "cache" / "agent-store" / "blob").read_text(encoding="utf-8") == "x"
    assert not (root / "cache" / "empty").exists()


def test_empty_extension_roots_are_dropped_even_without_a_profile_change(tmp_path: Path) -> None:
    """An unchanged packaged profile must not keep empty extension roots alive."""

    provisioner, profile, home, root, _ = _prepared(tmp_path)
    provisioner.provision(profile, home, uid=1001)
    for name in ("mcps", "plugins", "skills", "tools"):
        (root / name).mkdir()
    (root / "mcps" / "keep").mkdir()
    (root / "mcps" / "keep" / "mcp.json").write_text("{}\n", encoding="utf-8")

    result = provisioner.provision(profile, home, uid=1001)

    assert result.updated is False
    for name in ("plugins", "skills", "tools"):
        assert not (root / name).exists()
    assert (root / "mcps" / "keep" / "mcp.json").is_file()


def test_builtin_tool_upgrade_archives_only_previously_managed_copies(tmp_path):
    provisioner = _load_module()
    profile = _profile(tmp_path)
    home = tmp_path / 'home'
    home.mkdir()
    provisioner.provision(profile, home, uid=1000)
    root = home / '.bianbu-agent'
    legacy = root / 'tools/read_file'
    legacy.mkdir(parents=True)
    (legacy / 'tool.py').write_text('old modified builtin')
    custom = root / 'tools/my_tool'
    custom.mkdir()
    (custom / 'tool.py').write_text('user extension')
    state_path = root / '.managed-profile.json'
    state = json.loads(state_path.read_text())
    state['managed']['tools'] = ['read_file']
    state['profile_digest'] = 'old-profile'
    state_path.write_text(json.dumps(state))
    # Even a stale profile's tool payload must never seed code again.
    stale = profile / 'tools/read_file'
    stale.mkdir(parents=True)
    (stale / 'tool.py').write_text('stale package code')
    result = provisioner.provision(profile, home, uid=1000)
    assert not legacy.exists()
    assert (custom / 'tool.py').read_text() == 'user extension'
    backup = root / 'backups/package-upgrade' / result.profile_digest[:12] / 'tools/read_file/tool.py'
    assert backup.read_text() == 'old modified builtin'
    assert json.loads(state_path.read_text())['managed']['tools'] == []


def _write_presets(profile: Path, presets: dict) -> None:
    path = profile / "config.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["presets"] = presets
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def test_profile_presets_reach_an_existing_user_config(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    config_path = root / "config.yaml"
    document = yaml.safe_load(config_path.read_text())
    assert "presets" not in document
    document["language"] = "en"
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    presets = {"tools": ["terminal", "patch"], "skills": ["using-apm"]}
    _write_presets(profile, presets)

    assert provisioner.provision(profile, home, uid=1001).updated

    updated = yaml.safe_load(config_path.read_text())
    assert updated["presets"] == presets
    # Everything the user owned stays exactly as they left it.
    assert updated["language"] == "en"
    assert updated["service"]["port"] == document["service"]["port"]


def test_existing_presets_are_never_overwritten_by_the_profile(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    config_path = root / "config.yaml"
    document = yaml.safe_load(config_path.read_text())
    document["presets"] = {"tools": ["terminal"]}
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    _write_presets(profile, {"tools": ["patch"], "skills": ["using-apm"]})

    provisioner.provision(profile, home, uid=1001)

    assert yaml.safe_load(config_path.read_text())["presets"] == {"tools": ["terminal"]}


def test_profile_without_presets_leaves_user_config_untouched(tmp_path):
    provisioner, profile, home, root, _ = _prepared(tmp_path)
    config_path = root / "config.yaml"
    config_path.write_text("language: en\n", encoding="utf-8")
    (profile / "config.yaml").write_text(
        (profile / "config.yaml").read_text(encoding="utf-8") + "# upgrade\n",
        encoding="utf-8",
    )

    provisioner.provision(profile, home, uid=1001)

    assert config_path.read_text(encoding="utf-8") == "language: en\n"


def test_presets_return_to_a_config_older_than_the_backfill(tmp_path):
    """同一 profile 再跑一次也要补齐：包已经把 presets 写进 profile，但这台设备的
    用户配置比补齐代码更早，digest 相同，只有按次检查才补得上。"""

    provisioner = _load_module()
    profile = _profile(tmp_path)
    presets = {"tools": ["terminal"], "skills": ["using-apm"]}
    _write_presets(profile, presets)
    home = tmp_path / "home"
    home.mkdir()
    provisioner.provision(profile, home, uid=1001)
    root = home / ".bianbu-agent"
    config_path = root / "config.yaml"
    document = yaml.safe_load(config_path.read_text())
    document.pop("presets")
    document["language"] = "en"
    config_path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = provisioner.provision(profile, home, uid=1001)

    assert result.updated
    updated = yaml.safe_load(config_path.read_text())
    assert updated["presets"] == presets
    assert updated["language"] == "en"
    assert updated["service"]["port"] == document["service"]["port"]
