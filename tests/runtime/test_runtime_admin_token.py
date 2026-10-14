from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys


HELPER = Path(__file__).parents[2] / "debian" / "runtime-admin-token.py"


def _module():
    spec = importlib.util.spec_from_file_location("runtime_admin_token", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ensure_creates_protected_file_and_env_contract(tmp_path: Path):
    module = _module()
    env = tmp_path / "runtime.env"
    token_file = tmp_path / "runtime-admin-token"

    token = module.ensure_credentials(env, token_file, group=None)

    assert len(token) == 64
    assert token_file.read_text(encoding="utf-8").strip() == token
    assert env.read_text(encoding="utf-8") == f"BIANBU_RUNTIME_ADMIN_TOKEN_FILE={token_file}\n"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o640
    assert stat.S_IMODE(env.stat().st_mode) == 0o640


def test_ensure_is_idempotent_and_upgrade_safe(tmp_path: Path):
    module = _module()
    env = tmp_path / "runtime.env"
    token_file = tmp_path / "runtime-admin-token"

    first = module.ensure_credentials(env, token_file, group=None)
    second = module.ensure_credentials(env, token_file, group=None)

    assert second == first
    assert env.read_text(encoding="utf-8") == f"BIANBU_RUNTIME_ADMIN_TOKEN_FILE={token_file}\n"


def test_rotate_changes_file_without_putting_secret_in_env(tmp_path: Path):
    module = _module()
    env = tmp_path / "runtime.env"
    token_file = tmp_path / "runtime-admin-token"

    first = module.ensure_credentials(env, token_file, group=None)
    rotated = module.rotate_credentials(env, token_file, group=None)

    assert rotated != first
    assert token_file.read_text(encoding="utf-8").strip() == rotated
    assert env.read_text(encoding="utf-8") == f"BIANBU_RUNTIME_ADMIN_TOKEN_FILE={token_file}\n"


def test_explicit_direct_token_is_preserved_until_rotation(tmp_path: Path):
    module = _module()
    env = tmp_path / "runtime.env"
    env.write_text("# operator managed\nBIANBU_RUNTIME_ADMIN_TOKEN=operator-secret\n", encoding="utf-8")

    assert module.ensure_credentials(env, tmp_path / "unused-token", group=None) == "operator-secret"
    rotated = module.rotate_credentials(env, tmp_path / "unused-token", group=None)

    assert rotated != "operator-secret"
    assert f"BIANBU_RUNTIME_ADMIN_TOKEN={rotated}" in env.read_text(encoding="utf-8")


def test_cli_get_token_reads_file_without_echoing_env_contents(tmp_path: Path):
    env = tmp_path / "runtime.env"
    token_file = tmp_path / "runtime-admin-token"
    module = _module()
    token = module.ensure_credentials(env, token_file, group=None)

    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--get-token",
            "--env",
            str(env),
            "--token-file",
            str(token_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == token
    assert result.stderr == ""


def test_restart_targets_legacy_and_per_user_system_services(monkeypatch):
    module = _module()
    calls: list[list[str]] = []

    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command),
    )

    module._restart_runtime()

    assert calls == [
        ["systemctl", "try-restart", "bianbu-agent-runtime.service"],
        ["systemctl", "try-restart", "bianbu-agent-runtime@*.service"],
    ]
