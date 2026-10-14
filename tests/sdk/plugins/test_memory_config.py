from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[3] / 'resources/plugins/bianbu.memory'


def _module(path):
    spec = importlib.util.spec_from_file_location('memory_config_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_memory_recall_consumes_plugin_connection_and_budget_config(monkeypatch):
    calls = {}
    class Bridge:
        async def settings(self):
            return {'memory_enabled': True}
        async def recall(self, query, **kwargs):
            calls['recall'] = kwargs
            return ()
    async def connect(**kwargs):
        calls['connect'] = kwargs
        return Bridge()
    module = types.ModuleType('bianbu_agent_sdk.memory')
    module.get_memory_bridge = connect
    monkeypatch.setitem(sys.modules, 'bianbu_agent_sdk', types.ModuleType('bianbu_agent_sdk'))
    monkeypatch.setitem(sys.modules, 'bianbu_agent_sdk.memory', module)
    before = _module(ROOT / 'scripts/before_run.py')
    await before._load_agent_memory('tea', 'account', 'agent', {
        'socket_path': '/tmp/test-memory.sock', 'timeout_seconds': 0.75,
        'recall_top_k': 2, 'recall_max_chars': 300,
    })
    assert calls['connect'] == {'socket_path': '/tmp/test-memory.sock', 'timeout': 0.75, 'account_id': 'account', 'agent_name': 'agent'}
    assert calls['recall'] == {'top_k': 2, 'max_chars': 300}


def _field(name, aliases):
    return types.SimpleNamespace(name=name, aliases=tuple(aliases))


def _fake_agent_memory(monkeypatch, recalled, *, ontology=True):
    """Register a fake AgentMemory SDK and return the recorded bridge calls."""

    calls = {}

    class Bridge:
        async def settings(self):
            return {"memory_enabled": True}

        async def recall(self, query, **kwargs):
            calls["recall"] = kwargs
            return tuple(recalled)

    async def connect(**kwargs):
        calls["connect"] = kwargs
        return Bridge()

    memory = types.ModuleType("bianbu_agent_sdk.memory")
    memory.get_memory_bridge = connect
    monkeypatch.setitem(
        sys.modules, "bianbu_agent_sdk", types.ModuleType("bianbu_agent_sdk")
    )
    monkeypatch.setitem(sys.modules, "bianbu_agent_sdk.memory", memory)
    if ontology:
        module = types.ModuleType("bianbu_agent_sdk.memory.ontology")
        module.L3_FIELD_DICTIONARY = (
            _field("profile.user_name", ("姓名", "名字")),
            _field("development.editor", ("编辑器", "IDE", "editor")),
            _field("appearance.terminal_color_scheme", ("终端配色",)),
        )
        module.is_question_like = lambda text: "?" in text or "？" in text
        monkeypatch.setitem(sys.modules, "bianbu_agent_sdk.memory.ontology", module)
    else:
        monkeypatch.delitem(
            sys.modules, "bianbu_agent_sdk.memory.ontology", raising=False
        )
    return calls


# B4 消费侧的现网样本：测试/崩溃残留（kind=fact 的自由笔记）、自造槽位、
# 疑问句被当偏好，两种真偏好必须留下。
POLLUTED_AND_DURABLE_RECORDS = (
    "[fact rev=1 source=agent score=0.015] RT0190_MEMORY_E2E_cf02ae9ce787490d82b969e840b47473",
    "[fact rev=1 source=agent score=0.032] fact: CRASH-a83263804d",
    "[profile rev=1 source=agent score=0.020 slot=profile.explanation_style] 用户不熟悉年轻人的技术概念。",
    "[preference rev=1 source=agent score=0.030] 我喜欢用什么工具编码？",
    "[preference rev=1 source=agent score=0.040 slot=development.editor] 用户平时用 vim 写代码。",
    "[profile rev=1 source=agent score=0.050] 用户养的猫叫团子。",
)


@pytest.mark.asyncio
async def test_memory_recall_withholds_polluted_records(monkeypatch):
    """B4 消费侧：已落库的低质/伪装记录不得进入模型上下文。"""

    _fake_agent_memory(monkeypatch, POLLUTED_AND_DURABLE_RECORDS)
    before = _module(ROOT / "scripts/before_run.py")
    injected, withheld = await before._load_agent_memory("tea", "account", "agent", {})
    assert withheld == 4
    assert injected == (
        POLLUTED_AND_DURABLE_RECORDS[4],
        POLLUTED_AND_DURABLE_RECORDS[5],
    )


@pytest.mark.asyncio
async def test_memory_injection_guard_can_be_disabled(monkeypatch):
    """运维需要原样注入时可以按插件配置回退。"""

    _fake_agent_memory(monkeypatch, POLLUTED_AND_DURABLE_RECORDS)
    before = _module(ROOT / "scripts/before_run.py")
    injected, withheld = await before._load_agent_memory(
        "tea", "account", "agent", {"injection_guard": False}
    )
    assert withheld == 0
    assert injected == POLLUTED_AND_DURABLE_RECORDS


@pytest.mark.asyncio
async def test_memory_recall_without_sdk_ontology_keeps_what_it_cannot_judge(monkeypatch):
    """SDK 没有 ontology 时只拦能判定的记录，带槽位记录不误伤。"""

    recalled = POLLUTED_AND_DURABLE_RECORDS[:3]
    _fake_agent_memory(monkeypatch, recalled, ontology=False)
    before = _module(ROOT / "scripts/before_run.py")
    injected, withheld = await before._load_agent_memory("tea", "account", "agent", {})
    assert withheld == 2
    assert injected == (recalled[2],)


@pytest.mark.parametrize(
    ("item", "reason"),
    (
        (
            "[fact rev=1 source=agent score=0.015] RT0190_MEMORY_E2E_cf02",
            "not_a_durable_preference",
        ),
        (
            "[profile rev=1 source=agent score=0.020 slot=workflow.bio] 用户名叫 dec",
            "unknown_slot",
        ),
        (
            "[preference rev=1 source=agent score=0.030] 我用什么工具编码？",
            "question_like",
        ),
        ("[profile rev=1 source=agent score=0.040]   ", "empty"),
        (
            "[profile rev=1 source=agent score=0.050] bad\x00text",
            "control_characters",
        ),
        (
            "[profile rev=1 source=agent score=0.060] " + "长" * 2201,
            "oversized",
        ),
        (
            "[decision rev=1 source=agent score=0.070] 选择 SQLite 作为本地存储",
            None,
        ),
        (
            "[preference rev=1 source=agent score=0.080 slot=development.editor] 用户平时用 vim",
            None,
        ),
        (
            "[profile rev=1 source=agent score=0.090] 用户养的猫叫团子。",
            None,
        ),
    ),
)
def test_injection_pollution_reason_classifies_records(monkeypatch, item, reason):
    _fake_agent_memory(monkeypatch, ())
    before = _module(ROOT / "scripts/before_run.py")
    assert before.injection_pollution_reason(item) == reason


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


def test_memory_config_survives_package_install_and_effective_inspection(tmp_path):
    import shutil
    from bianbu_agent.home import AgentHome
    from bianbu_agent.plugins.package_manager import PluginPackageManager
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
