from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import tomllib

import yaml

from bianbu_agent.config import load_runtime_config


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "prepare-development-security-manifest.py"


def test_prepare_manifest_matches_source_agent_and_runtime_config(tmp_path: Path) -> None:
    config_path = PROJECT_ROOT / "config" / "development.yaml"
    runtime_config = tmp_path / "config-all.yaml"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project-root",
            str(PROJECT_ROOT),
            "--config",
            str(config_path),
            "--output",
            str(tmp_path),
            "--runtime-config-output",
            str(runtime_config),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    package_dir = Path(result.stdout.strip())
    manifest = tomllib.loads(
        (package_dir / "manifest.toml").read_text(encoding="utf-8")
    )
    config = load_runtime_config(runtime_config)
    assert config.security.required is True
    assert config.runtime.control.startup_timeout_seconds == 60
    assert config.runtime.control.handshake_timeout_seconds == 60
    assert config.runtime.worker.startup_timeout_seconds == 60
    assert manifest["agent"]["id"] == "com.bianbu.runtime.development"
    # The development manifest binds the runtime catalog too, so a dev
    # registration lists runtime tools only.
    assert manifest["agent"]["framework"] == "runtime"
    template = yaml.safe_load(
        (PROJECT_ROOT / "resources" / "default-profile" / "starter-agent.yaml").read_text(encoding="utf-8")
    )
    assert manifest["agent"]["version"] == template["agent"]["version"].removeprefix("v")
    assert set(manifest["runtime"]) == {"entrypoint"}
    assert manifest["runtime"]["entrypoint"] == list(
        config.runtime.worker.entrypoint
    )
    assert manifest["runtime"]["entrypoint"] == [
        sys.executable,
        "-m",
        "bianbu_agent.cli",
        "--role",
        "agent-worker",
        "--config",
        str(runtime_config),
        "worker",
    ]
    assert (tmp_path / "DEVELOPMENT-SELF-SIGNED").is_file()


def test_prepare_manifest_names_the_worker_config_inside_the_data_root(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    config_path = tmp_path / "dev.yaml"
    config_path.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    runtime_config = data_dir / "config-all.yaml"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--project-root",
            str(PROJECT_ROOT),
            "--config",
            str(config_path),
            "--output",
            str(tmp_path / "manifest"),
            "--runtime-config-output",
            str(runtime_config),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    manifest = tomllib.loads(
        (Path(result.stdout.strip()) / "manifest.toml").read_text(encoding="utf-8")
    )
    # Security Runtime binds the data root at /home/agent and rewrites this
    # argument back to the host config for host-mode Workers, so the manifest
    # must not embed the checkout path.
    assert manifest["runtime"]["entrypoint"] == [
        sys.executable,
        "-m",
        "bianbu_agent.cli",
        "--role",
        "agent-worker",
        "--config",
        "/home/agent/config-all.yaml",
        "worker",
    ]
    assert load_runtime_config(runtime_config).runtime.worker.entrypoint[-3:] == (
        "--config",
        "/home/agent/config-all.yaml",
        "worker",
    )
