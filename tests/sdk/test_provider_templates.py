from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import yaml

from bianbu_agent.models.providers import PROVIDER_TEMPLATES


ROOT = Path(__file__).resolve().parents[2]


def test_minimax_provider_template_uses_openai_compatible_endpoint() -> None:
    template = next(item for item in PROVIDER_TEMPLATES if item["id"] == "minimax")

    assert template == {
        "id": "minimax",
        "name": "MiniMax",
        "protocol": "openai-compatible",
        "default_base_url": "https://api.minimaxi.com/v1",
    }


def test_packaged_debian_defaults_bind_bibit_provider_without_exposing_key_in_tests(
    tmp_path: Path,
) -> None:
    provider_path = ROOT / "resources" / "models" / "custom-bibit" / "provider.yaml"
    renderer_path = ROOT / "debian" / "render-starter-agent.py"
    source_path = tmp_path / "source-agent.yaml"
    output_path = tmp_path / "packaged-agent.yaml"

    source = yaml.safe_load(
        (ROOT / "resources/default-profile/starter-agent.yaml").read_text(encoding="utf-8")
    )
    source["agent"]["model"] = {"provider": "custom:test", "name": "other"}
    source_path.write_text(
        yaml.safe_dump(source, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )

    spec = importlib.util.spec_from_file_location("render_starter_agent", renderer_path)
    assert spec is not None and spec.loader is not None
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    renderer.render_starter_agent(source_path, output_path)

    provider = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    agent = yaml.safe_load(output_path.read_text(encoding="utf-8"))

    assert provider["provider"] == "custom:bibit"
    assert provider["id"] == "custom-provider-bibit"
    assert provider["parameters"]["record_kind"] == "custom_provider"
    assert provider["parameters"]["custom_id"] == "bibit"
    assert provider["parameters"]["name"] == "Bibit Built-in"
    assert provider["base_url"] == "https://api.deepseek.com/v1"
    assert provider["parameters"]["api_key"]
    source["agent"]["model"] = {"provider": "custom:bibit", "name": "deepseek-flash"}
    assert agent == source
    assert agent["agent"]["model"] == {"provider": "custom:bibit", "name": "deepseek-flash"}


def test_packaged_provider_is_a_custom_record_owned_by_bibit() -> None:
    """The factory credential is a Custom Provider record, not a vendor template.

    Presenting it as ``custom:bibit`` keeps the Models page from offering it as
    a vendor template the user is expected to fill in with their own key, while
    the display name ("Bibit Built-in") still says the credential ships with
    Bibit, in English so it reads the same on every locale.
    """

    provider = yaml.safe_load(
        (ROOT / "resources" / "models" / "custom-bibit" / "provider.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert provider["provider"] == "custom:bibit"
    assert provider["parameters"]["record_kind"] == "custom_provider"
    assert provider["parameters"]["name"] == "Bibit Built-in"
    assert all(item["id"] != provider["provider"] for item in PROVIDER_TEMPLATES)
    template = next(item for item in PROVIDER_TEMPLATES if item["id"] == "deepseek")
    assert template["default_base_url"] == provider["base_url"]


def test_builtin_bibit_expert_names_every_capability_kind():
    """出厂专家逐个点名工具/技能/插件，不写通配符，也不重复点名插件名下的成员。

    通配符把含义交给了「这台机器上装了什么」，于是能力计数、编辑器勾选和
    「临时使用技能」菜单都得各自特判一次；改成本包发布的那份清单之后，四处读的
    是同一个列表。选中插件就已经选中了它名下的 MCP 服务器、技能和 Hook，再逐个
    点名等于把同一件事写两遍，所以 `mcps:` 只列不属于任何已选插件的服务器。
    清单随包更新，`tests/sdk/test_project_definitions.py` 会检查每一项都真实
    存在于本仓。
    """
    document = yaml.safe_load((ROOT / 'resources/default-profile/starter-agent.yaml').read_text())
    agent = document['agent']
    assert agent['name'] == 'BibitEdgeAssistant'
    for kind in ('tools', 'skills', 'mcps', 'plugins'):
        assert '*' not in agent[kind]
    for kind in ('tools', 'skills', 'plugins'):
        assert agent[kind]
    selected_plugins = set(agent['plugins'])
    for kind in ('mcps', 'skills', 'tools'):
        for selector in agent[kind]:
            owner = selector.split('/', 1)[0] if '/' in selector else selector
            assert owner not in selected_plugins, (
                f"{kind} entry {selector} belongs to the selected Plugin {owner}"
            )


def test_console_provider_templates_match_the_runtime_catalog() -> None:
    """The Models page must offer exactly the providers the Runtime defines."""

    source = (
        ROOT / "bianbu_agent" / "console" / "web" / "static" / "js" / "console.js"
    ).read_text(encoding="utf-8")
    start = source.index("const PROVIDER_TEMPLATES = [")
    block = source[start : source.index("];", start)]
    rows = re.findall(
        r"\{id: '([^']*)', name: '([^']*)', protocol: '([^']*)',"
        r" default_base_url: '([^']*)'\}",
        block,
    )
    assert rows, "console.js no longer declares a flat PROVIDER_TEMPLATES array"
    assert [
        {
            "id": identifier,
            "name": name,
            "protocol": protocol,
            "default_base_url": base_url,
        }
        for identifier, name, protocol, base_url in rows
    ] == [dict(template) for template in PROVIDER_TEMPLATES]
