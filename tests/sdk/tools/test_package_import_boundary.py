from pathlib import Path

import pytest

from bianbu_agent.tools import DirectoryToolLoader


@pytest.mark.parametrize(
    ("source", "filename"),
    [
        ("from bianbu_agent._legacy.tools import registry\n", "tool.py"),
        ("import bianbu_agent._legacy.tools.runtime\n", "tool.py"),
        ("from bianbu_agent import ToolResult\n", "tool.py"),
        ("from bianbu_agent.tools import ProcessRequest\n", "tool.py"),
        ("from langchain_core.tools import tool\n", "tool.py"),
        ("from agent_core.runtime import service\n", "tool.py"),
        ("from cron.jobs import parse_schedule\n", "helper.py"),
        ("from plugins.example import value\n", "helper.py"),
        ("from providers.example import value\n", "helper.py"),
        ("import hermes_state\n", "helper.py"),
        (
            "from importlib import import_module\n"
            "legacy = import_module('bianbu_agent.tools.web')\n",
            "helper.py",
        ),
    ],
)
def test_inspection_rejects_non_owned_project_imports_before_loading(
    tmp_path: Path,
    source: str,
    filename: str,
) -> None:
    root = tmp_path / "unsafe_tool"
    root.mkdir()
    (root / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n",
        encoding="utf-8",
    )
    (root / filename).write_text(source, encoding="utf-8")

    with pytest.raises(ValueError, match="forbidden Tool dependency"):
        DirectoryToolLoader.inspect(root)


def test_inspection_allows_sdk_decorator_standard_third_party_and_local_imports(
    tmp_path: Path,
) -> None:
    root = tmp_path / "safe_tool"
    root.mkdir()
    (root / "tool.py").write_text(
        "import asyncio\n"
        "import httpx\n"
        "from bianbu_agent.tools import tool\n"
        "from .helper import render\n",
        encoding="utf-8",
    )
    (root / "helper.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "def render(value):\n"
        "    return json.dumps({'path': str(Path(value))})\n",
        encoding="utf-8",
    )

    inspection = DirectoryToolLoader.inspect(root)

    assert inspection.name == "safe_tool"


def test_inspection_rejects_runtime_access_before_loading(tmp_path: Path) -> None:
    root = tmp_path / "runtime_tool"
    root.mkdir()
    (root / "tool.py").write_text(
        "from bianbu_agent.tools import tool\n"
        "def create_tool(context):\n"
        "    async def invoke():\n"
        "        return await context.runtime()\n"
        "    return tool(invoke)\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="runtime access"):
        DirectoryToolLoader.inspect(root)
