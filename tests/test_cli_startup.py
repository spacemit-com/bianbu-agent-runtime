"""Help must not initialize server, model, or Worker dependencies."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("arguments", [[], ["--help"], ["agent", "--help"], ["serve", "--help"], ["worker", "--help"]])
def test_cli_help_does_not_import_runtime_dependencies(arguments):
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc
import runpy
import sys
class BlockRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'langchain', 'langchain_core', 'langgraph', 'fastapi'}:
            raise AssertionError('help loaded runtime dependency: ' + fullname)
sys.meta_path.insert(0, BlockRuntime())
sys.argv = ['bianbu-agent', *sys.argv[1:]]
runpy.run_module('bianbu_agent.cli', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *arguments], cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout


def test_public_exports_keep_original_identity():
    from bianbu_agent import AgentSpec
    from bianbu_agent.agents import AgentSpec as DirectAgentSpec
    assert AgentSpec is DirectAgentSpec


def test_expected_lookup_failure_is_concise(monkeypatch, capsys):
    import importlib
    cli = importlib.import_module("bianbu_agent.cli.main")

    async def missing(args):
        raise KeyError("Agent is not installed")

    monkeypatch.setattr(cli, "_main_with_args", missing)
    monkeypatch.setattr(sys, "argv", ["bianbu-agent", "agent", "list"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert "Agent is not installed" in capsys.readouterr().err


def test_version_does_not_load_runtime():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-X", "importtime", "-m", "bianbu_agent.cli", "--version"],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root)},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("bianbu-agent ")
    assert "langchain" not in result.stderr
    assert "bianbu_agent.server.runtime" not in result.stderr


def test_packaged_wrapper_keeps_arguments_and_default_config_separate(tmp_path):
    root = Path(__file__).resolve().parents[1]
    wrapper = tmp_path / "bin" / "bianbu-agent"
    wrapper.parent.mkdir()
    wrapper.write_bytes((root / "debian/bianbu-agent-wrapper").read_bytes())
    wrapper.chmod(0o755)
    executable = tmp_path / "lib/bianbu-agent-runtime/venv/bin/bianbu-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!{sys.executable}\nimport sys, os, json\nprint(json.dumps([sys.argv[1:],os.environ.get('BIANBU_AGENT_CONFIG')]))\n")
    executable.chmod(0o755)
    for arguments in ([], ["--help"], ["run", "project"], ["agent", "list"]):
        result = subprocess.run([str(wrapper), *arguments], capture_output=True, text=True,
                                env={**os.environ, "BIANBU_AGENT_CONFIG": str(tmp_path / "user.yaml")}, check=True)
        args, config = json.loads(result.stdout)
        assert args == arguments
        assert config == str(tmp_path / "user.yaml")


def _packaged_wrapper(tmp_path: Path) -> tuple[Path, Path]:
    """Materialise the packaged wrapper plus its venv entry point."""
    root = Path(__file__).resolve().parents[1]
    wrapper = tmp_path / "bin" / "bianbu-agent"
    wrapper.parent.mkdir()
    wrapper.write_bytes((root / "debian/bianbu-agent-wrapper").read_bytes())
    wrapper.chmod(0o755)
    executable = tmp_path / "lib/bianbu-agent-runtime/venv/bin/bianbu-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys, os, json\n"
        "print(json.dumps([sys.argv[1:], os.environ.get('BIANBU_AGENT_CONFIG')]))\n"
    )
    executable.chmod(0o755)
    return wrapper, executable


def _run_wrapper(wrapper: Path, tmp_path: Path, *arguments: str, **environment: str):
    return subprocess.run(
        [str(wrapper), *arguments],
        capture_output=True,
        text=True,
        env={**os.environ, **environment},
        check=True,
    )


def test_packaged_wrapper_resolves_the_runtime_service_user(tmp_path):
    """One device, one Runtime: the CLI must not read the caller's own $HOME.

    ``sudo bianbu-agent`` and any second login user would otherwise talk to a
    directory the system service never opens.
    """
    wrapper, _ = _packaged_wrapper(tmp_path)
    home = tmp_path / "home" / "bianbu"
    (home / ".bianbu-agent").mkdir(parents=True)
    record = tmp_path / "service-user"
    record.write_text("bianbu\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    (fake_bin / "getent").write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' 'bianbu:x:1000:1000::{home}:/bin/bash'\n"
    )
    (fake_bin / "getent").chmod(0o755)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "BIANBU_SERVICE_USER_FILE": str(record),
        "HOME": str(tmp_path / "root-home"),
        "BIANBU_AGENT_CONFIG": "",
    }

    result = _run_wrapper(wrapper, tmp_path, "model", "list", **environment)

    arguments, config = json.loads(result.stdout)
    assert arguments == ["model", "list"]
    assert config == str(home / ".bianbu-agent" / "config.yaml")
    # The caller is not the service user and the directory is unreadable, so the
    # wrapper has to name the owner instead of failing on a path nobody chose.
    assert "bianbu" in result.stderr
    assert "BIANBU_AGENT_CONFIG" in result.stderr


def test_packaged_wrapper_keeps_the_login_home_without_a_runtime_record(tmp_path):
    """A source checkout has no service-user record and keeps plain $HOME."""
    wrapper, _ = _packaged_wrapper(tmp_path)
    home = tmp_path / "home" / "alice"
    home.mkdir(parents=True)

    result = _run_wrapper(
        wrapper,
        tmp_path,
        "agent",
        "list",
        HOME=str(home),
        BIANBU_SERVICE_USER_FILE=str(tmp_path / "missing-service-user"),
        BIANBU_AGENT_CONFIG="",
    )

    arguments, config = json.loads(result.stdout)
    assert arguments == ["agent", "list"]
    assert config == str(home / ".bianbu-agent" / "config.yaml")
    assert result.stderr == ""


def test_worker_factory_does_not_import_http_application():
    """A Worker never serves HTTP; importing its factory must not load routes."""
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib.abc
import sys
class BlockHTTP(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'bianbu_agent.server.app', 'bianbu_agent.api'}:
            raise AssertionError('Worker loaded HTTP application: ' + fullname)
sys.meta_path.insert(0, BlockHTTP())
from bianbu_agent.server.factory import create_runtime
assert callable(create_runtime)
"""
    result = subprocess.run(
        [sys.executable, '-c', script], cwd=root,
        env={**os.environ, 'PYTHONPATH': str(root)}, capture_output=True,
        text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
