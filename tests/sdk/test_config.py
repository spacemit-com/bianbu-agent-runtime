from __future__ import annotations

import math
from pathlib import Path

import pytest
import yaml

import bianbu_agent


RuntimeConfig = getattr(bianbu_agent, "RuntimeConfig", None)
RuntimeConfigError = getattr(bianbu_agent, "RuntimeConfigError", RuntimeError)
init_runtime_config = getattr(bianbu_agent, "init_runtime_config", None)
load_runtime_config = getattr(bianbu_agent, "load_runtime_config", None)
update_runtime_config = getattr(bianbu_agent, "update_runtime_config", None)


def test_runtime_configuration_public_api_exists() -> None:
    assert RuntimeConfig is not None
    assert RuntimeConfigError is not RuntimeError
    assert callable(init_runtime_config)
    assert callable(load_runtime_config)
    assert callable(update_runtime_config)


def test_worker_policy_admits_independent_sessions_without_global_run_cap() -> None:
    config = RuntimeConfig(data_dir=Path("."))

    assert config.runtime.worker.max_concurrent_runs is None


def _document(*, data_dir: str = ".") -> dict[str, object]:
    return {
        "data_dir": data_dir,
        "service": {
            "title": "Bianbu Agent",
            "host": "0.0.0.0",
            "port": 9899,
        },
        "language": "zh",
        "require_file_backed_capabilities": True,
        "plugin_stop_timeout_seconds": 5.0,
        "allow_unmanaged_capabilities": False,
    }


def _write_config(path: Path, document: dict[str, object] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(document or _document(), sort_keys=False),
        encoding="utf-8",
    )


def test_load_runtime_config_resolves_relative_data_dir_from_yaml_parent(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "etc" / "runtime.yaml"
    _write_config(config_path, _document(data_dir="../state"))

    config = load_runtime_config(config_path)

    assert config.config_path == config_path.resolve()
    assert config.data_dir == (tmp_path / "state").resolve()
    assert config.service.title == "Bianbu Agent"
    assert config.service.host == "0.0.0.0"
    assert config.service.port == 9899
    assert config.plugin_dirs == ((tmp_path / "state" / "plugins").resolve(),)


def test_load_runtime_config_expands_user_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_home = tmp_path / "user"
    config_path = tmp_path / "runtime.yaml"
    monkeypatch.setenv("HOME", str(user_home))
    _write_config(config_path, _document(data_dir="~/.bianbu-agent"))

    config = load_runtime_config(config_path)

    assert config.data_dir == (user_home / ".bianbu-agent").resolve()


def test_load_runtime_config_preserves_base_plugin_configuration(tmp_path: Path) -> None:
    document = _document()
    document["plugins"] = {
        "vendor.bundle": {"mode": "runtime", "nested": {"limit": 20}}
    }
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    config = load_runtime_config(config_path)

    assert dict(config.plugin_configs["vendor.bundle"]) == {
        "mode": "runtime",
        "nested": {"limit": 20},
    }


def test_load_runtime_config_does_not_create_missing_file(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.yaml"

    with pytest.raises(RuntimeConfigError, match="does not exist"):
        load_runtime_config(config_path)

    assert not config_path.exists()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda value: value.pop("data_dir"), "data_dir"),
        (lambda value: value.__setitem__("unknown", True), "unknown"),
        (lambda value: value.__setitem__("service", {"port": 0}), "port"),
        (
            lambda value: value.__setitem__("service", {"host": "", "port": 9899}),
            "host",
        ),
    ],
)
def test_load_runtime_config_rejects_invalid_documents(
    tmp_path: Path, mutate, match: str
) -> None:
    document = _document()
    mutate(document)
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    with pytest.raises(RuntimeConfigError, match=match):
        load_runtime_config(config_path)


def test_load_runtime_config_rejects_unknown_service_key(tmp_path: Path) -> None:
    document = _document()
    document["service"] = {"host": "0.0.0.0", "port": 9899, "workers": 2}
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    with pytest.raises(RuntimeConfigError, match="workers"):
        load_runtime_config(config_path)


def test_load_runtime_config_rejects_retired_searxng_url(tmp_path: Path) -> None:
    document = _document()
    document["searxng_url"] = "http://remote.invalid:8888"
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    with pytest.raises(RuntimeConfigError, match="searxng_url"):
        load_runtime_config(config_path)


def test_init_runtime_config_creates_once_without_data_tree(tmp_path: Path) -> None:
    config_path = tmp_path / "deployment" / "runtime.yaml"

    created = init_runtime_config(config_path)

    assert created == config_path.resolve()
    assert config_path.is_file()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["data_dir"] == "."
    assert "searxng_url" not in yaml.safe_load(
        config_path.read_text(encoding="utf-8")
    )
    assert not (config_path.parent / "agents").exists()
    with pytest.raises(RuntimeConfigError, match="already exists"):
        init_runtime_config(config_path)


def test_update_runtime_config_atomically_persists_valid_values(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path)

    updated = update_runtime_config(
        config_path,
        {"language": "en", "service": {"title": "My Agent", "port": 18765}},
    )

    assert updated.language == "en"
    assert updated.service.title == "My Agent"
    assert updated.service.port == 18765
    assert load_runtime_config(config_path) == updated
    assert list(tmp_path.glob(".runtime.yaml.*.tmp")) == []


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan])
def test_runtime_config_rejects_invalid_timeout(tmp_path: Path, value: float) -> None:
    with pytest.raises(RuntimeConfigError, match="timeout"):
        RuntimeConfig(
            data_dir=tmp_path,
            plugin_stop_timeout_seconds=value,
        )


@pytest.mark.parametrize(
    "legacy",
    [
        {"default_agent_id": "bianbu-agent-default"},
        {"runtime": {"default_agent": {"id": "bianbu-agent-default"}}},
        {"security": {"agent_id": "bianbu-agent-default"}},
    ],
)
def test_runtime_config_rejects_global_default_agent_fields(
    tmp_path: Path, legacy: dict[str, object]
) -> None:
    document = _document()
    document.update(legacy)
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    with pytest.raises(RuntimeConfigError):
        load_runtime_config(config_path)


def test_runtime_config_reads_presets_from_configuration(tmp_path: Path) -> None:
    document = _document()
    document["presets"] = {
        "tools": ["read_file", "terminal"],
        "skills": ["using-apm"],
    }
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    config = load_runtime_config(config_path)

    assert config.presets.tools == ("read_file", "terminal")
    assert config.presets.skills == ("using-apm",)


def test_runtime_config_defaults_to_no_presets(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path)

    config = load_runtime_config(config_path)

    assert config.presets.tools == ()
    assert config.presets.skills == ()


def test_init_runtime_config_template_declares_the_always_on_capabilities(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "runtime.yaml"

    init_runtime_config(config_path)

    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert document["presets"] == {
        "tools": [
            "skills_list",
            "skill_view",
            "skill_manage",
            "terminal",
            "process",
            "read_file",
            "write_file",
            "search_files",
            "patch",
        ],
        "skills": ["using-apm"],
    }


@pytest.mark.parametrize(
    "presets",
    [
        {"tools": "read_file"},
        {"tools": ["*"]},
        {"tools": ["read_file", "read_file"]},
        {"skills": [""]},
        {"unknown": ["read_file"]},
    ],
)
def test_runtime_config_rejects_invalid_presets(
    tmp_path: Path, presets: dict[str, object]
) -> None:
    document = _document()
    document["presets"] = presets
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    with pytest.raises(RuntimeConfigError):
        load_runtime_config(config_path)


def test_runtime_config_rejects_the_retired_automatic_execution_mode(
    tmp_path: Path,
) -> None:
    document = _document()
    document["runtime"] = {"worker": {"execution": {"requested_mode": "auto"}}}
    config_path = tmp_path / "runtime.yaml"
    _write_config(config_path, document)

    with pytest.raises(RuntimeConfigError, match="requested_mode must be one of"):
        load_runtime_config(config_path)
