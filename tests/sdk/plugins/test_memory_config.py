from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3] / 'resources/plugins/bianbu.memory'


def test_memory_manifest_retains_hooks_and_one_mcp():
    from bianbu_agent.plugins.manifest import load_plugin_manifest
    from bianbu_agent.plugins.composition import compose_plugin_config
    manifest = load_plugin_manifest(ROOT)
    assert not (ROOT / 'console.py').exists()
    # Memory Tools moved behind the MCP server so the child process can carry
    # its own memoryd registration.
    assert not manifest.contributions.tools
    assert manifest.contributions.mcps == ('memory',)
    assert len(manifest.contributions.hooks) == 2
    config = compose_plugin_config(manifest).config
    assert config['socket_path'] is None
    assert config['timeout_seconds'] == 2


def test_memory_mcp_launches_the_runtime_interpreter_without_pythonpath(
    tmp_path, monkeypatch
):
    """memoryd authenticates the peer executable, so the command must not drift."""

    from bianbu_agent.mcp.config import load_mcp_config
    # The definition names the interpreter symbolically: the Debian package and
    # a source checkout run different venvs, and only the interpreter that
    # expands the definition is guaranteed to be the Worker's own executable.
    monkeypatch.delenv('RUNTIME_PYTHON', raising=False)
    monkeypatch.delenv('RUNTIME_PYTHON_BIN_DIR', raising=False)
    config = load_mcp_config(ROOT / 'mcps/memory/mcp.json', workspace=tmp_path)
    assert config.transport == 'stdio'
    # A Worker-started MCP child must reuse the Runtime venv so /proc/<pid>/exe
    # still matches the registered executable_path.
    assert config.command == sys.executable
    assert config.args == ('-m', 'bianbu_agent_sdk.memory.mcp_server')
    assert config.env == {
        'PATH': f'{Path(sys.executable).parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin'
    }


def test_memory_mcp_honours_an_explicit_runtime_interpreter(tmp_path, monkeypatch):
    from bianbu_agent.mcp.config import load_mcp_config
    monkeypatch.setenv('RUNTIME_PYTHON', '/opt/runtime/bin/python3')
    monkeypatch.setenv('RUNTIME_PYTHON_BIN_DIR', '/opt/runtime/bin')
    config = load_mcp_config(ROOT / 'mcps/memory/mcp.json', workspace=tmp_path)
    assert config.command == '/opt/runtime/bin/python3'
    assert config.env['PATH'].startswith('/opt/runtime/bin:')


def test_mcp_credentials_publish_the_loading_interpreter(tmp_path, monkeypatch):
    from bianbu_agent.mcp.credentials import load_environment
    monkeypatch.delenv('RUNTIME_PYTHON', raising=False)
    monkeypatch.delenv('RUNTIME_PYTHON_BIN_DIR', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    values = load_environment()
    assert values['RUNTIME_PYTHON'] == sys.executable
    assert values['RUNTIME_PYTHON_BIN_DIR'] == str(Path(sys.executable).parent)


def test_memory_config_survives_package_install_and_effective_inspection(tmp_path, monkeypatch):
    import shutil
    from bianbu_agent.home import AgentHome
    from bianbu_agent.plugins.package_manager import PluginPackageManager
    monkeypatch.setattr("bianbu_agent.system_resources.system_resource_dir",
                        lambda _kind: tmp_path / "empty-system-resources")
    source = tmp_path / 'source'
    shutil.copytree(ROOT, source, ignore=shutil.ignore_patterns('__pycache__'))
    home = AgentHome(tmp_path / 'home').prepare()
    manager = PluginPackageManager(home)
    manager.install(source)
    assert [item.id for item in manager.list()] == ['bianbu.memory']
    effective = manager.effective({'bianbu.memory': {'recall_top_k': 2}})[0]
    assert effective['config']['recall_top_k'] == 2
    assert effective['config']['timeout_seconds'] == 2
    assert manager.verify('bianbu.memory') is True
