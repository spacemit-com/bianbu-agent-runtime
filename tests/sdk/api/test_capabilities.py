from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import BUILTIN_MODULES
from tests.sdk.api.test_packages import _skill
from tests.sdk.api.test_runs import _graph_api


def _plugin_tool(root: Path) -> Path:
    """Build one Tool directory that belongs to a Plugin package."""
    source = root / "echo"
    source.mkdir(parents=True)
    (source / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    @tool\n"
        "    def echo() -> str:\n"
        "        '''Echo.'''\n"
        "        return 'ok'\n"
        "    return echo\n",
        encoding="utf-8",
    )
    return source


def _plugin(root: Path) -> Path:
    source = root / "vendor.bundle"
    _plugin_tool(source / "tools")
    (source / "tools" / "echo").rename(source / "tools" / "plugin_echo")
    tool_source = source / "tools" / "plugin_echo" / "tool.py"
    tool_source.write_text(
        tool_source.read_text(encoding="utf-8")
        .replace("def echo() -> str", "def plugin_echo() -> str")
        .replace("return echo", "return plugin_echo")
        .replace("def create_tool", "def create_tool"),
        encoding="utf-8",
    )
    _skill(source / "skills", "review")
    hook = source / "hooks" / "audit.policy"
    hook.mkdir(parents=True)
    (hook / "hook.json").write_text('{"schema_version": 1, "hooks": {"BeforeRun": [{"hooks": [{"type": "command", "command": "printf \\"{}\\""}]}]}}', encoding="utf-8")
    (source / "plugin.yaml").write_text(
        """\
schema_version: 1
name: vendor.bundle
version: 1.0.0
description: Capability fixture bundle.
""",
        encoding="utf-8",
    )
    return source


@pytest.mark.asyncio
async def test_effective_capability_catalog_includes_hot_loaded_packages(
    tmp_path: Path,
) -> None:
    """The Agent catalog includes Plugin members and newly installed packages."""

    runtime_root = tmp_path / "runtime"
    _plugin(runtime_root / ".bianbu-agent" / "plugins")

    async with _graph_api(runtime_root, plugins=("*",)) as (client, _, _):
        # Tools are built into the Runtime, so the standalone install route is
        # retired and Tool extension goes through MCP instead.
        retired_tool = await client.post(
            "/api/v1/packages/tool", json={"source": str(tmp_path)}
        )
        assert retired_tool.status_code == 422, retired_tool.text
        # Standalone Hook packages are retired as well: a Plugin contributes
        # Hook members, so the package route no longer accepts this kind.
        retired_hook = await client.post(
            "/api/v1/packages/hook", json={"source": str(tmp_path)}
        )
        assert retired_hook.status_code == 422, retired_hook.text

        tools = await client.get("/api/v1/capabilities/tool")
        skills = await client.get("/api/v1/capabilities/skill")
        hooks = await client.get("/api/v1/capabilities/hook")
        plugins = await client.get("/api/v1/plugins/effective")

    assert tools.status_code == 200
    tool_by_id = {item["id"]: item for item in tools.json()["data"]}
    assert tool_by_id["vendor.bundle/plugin_echo"] == {
        "id": "vendor.bundle/plugin_echo",
        "name": "vendor.bundle/plugin_echo",
        "display_name": "plugin_echo",
        "description": "Echo.",
        "name_zh": "",
        "name_en": "",
        "description_zh": "",
        "summaries": {},
        "version": tool_by_id["vendor.bundle/plugin_echo"]["version"],
        "origin": "plugin",
        "plugin_id": "vendor.bundle",
            "enabled": True,
            "assignable": True,
            "preset": False,
            "unavailable_reason": "",
    }
    assert set(BUILTIN_MODULES) <= set(tool_by_id), "builtin Tools stay listed"
    assert tool_by_id["read_file"]["enabled"] is True
    assert tool_by_id["read_file"]["assignable"] is True
    # Runtime-owned tools carry the UI intro in both languages; the Plugin tool
    # above carries none and keeps its own description.
    assert tool_by_id["read_file"]["summaries"]["zh"].startswith("读取当前 Tool 工作区")
    assert tool_by_id["read_file"]["summaries"]["en"].startswith("Read a UTF-8 text file")

    assert skills.status_code == 200
    # A Plugin Skill is selectable for one message, so the picker needs it.
    # Its id stays plugin-qualified: that is the handle the Run resolves and
    # the prefix the Console and Bibit search by.
    assert skills.json()["data"] == [
        {
            "id": "vendor.bundle/review",
            "name": "vendor.bundle/review",
            "display_name": "review",
            "description": "Review carefully",
            "name_zh": "",
            "name_en": "",
            "description_zh": "",
            "summaries": {},
            "version": "1.0.0",
            "origin": "plugin",
            "plugin_id": "vendor.bundle",
            "enabled": True,
            "assignable": True,
            "preset": False,
            "unavailable_reason": "",
        },
    ]
    assert plugins.status_code == 200
    bundle = next(
        item for item in plugins.json()["data"] if item["id"] == "vendor.bundle"
    )
    assert bundle["contributions"]["skills"] == ["vendor.bundle/review"]
    assert bundle["contributions"]["hooks"] == ["vendor.bundle/audit.policy"]
    assert "vendor.bundle/review" in bundle["members"]
    assert hooks.status_code == 200
    assert hooks.json()["data"] == [
        {
            "id": "vendor.bundle/audit.policy",
            "name": "vendor.bundle/audit.policy",
            "display_name": "vendor.bundle/audit.policy",
            "description": "",
            "name_zh": "",
            "name_en": "",
            "description_zh": "",
            "summaries": {},
            "version": "0.0.0",
            "origin": "plugin",
            "plugin_id": "vendor.bundle",
            "enabled": True,
            "assignable": False,
            "preset": False,
            "unavailable_reason": "",
        },
    ]


def test_effective_capability_catalog_has_a_typed_bounded_openapi_contract() -> None:
    """Generated clients see the exact item schema and tool-or-skill path bound."""

    from tests.sdk.api.test_contracts import _application

    operation = _application().openapi()["paths"][
        "/api/v1/capabilities/{kind}"
    ]["get"]
    kind = next(item for item in operation["parameters"] if item["name"] == "kind")
    assert set(kind["schema"]["enum"]) == {"tool", "skill", "hook"}
    assert operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/CapabilityCollectionEnvelope"}
