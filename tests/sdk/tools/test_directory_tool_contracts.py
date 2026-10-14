from __future__ import annotations

from pathlib import Path
import subprocess

from bianbu_agent.tools import ToolLoadContext
from bianbu_agent.tools.builtins import BUILTIN_MODULES, builtin_source, inspect_builtin, load_builtin

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = PROJECT_ROOT / ".bianbu-agent" / "tools"


def test_every_checked_in_tool_has_one_valid_directory_contract() -> None:
    assert len(BUILTIN_MODULES) == 34
    assert {"read_file", "terminal", "write_file"}.issubset(BUILTIN_MODULES)
    assert {"browser_vision", "delegate_task", "execute_code"}.isdisjoint(BUILTIN_MODULES)
    for name in BUILTIN_MODULES:
        assert inspect_builtin(name).source_path == builtin_source(name)
    assert not list(TOOL_ROOT.glob("*/tool.py"))


def test_active_tool_schemas_come_from_the_decorated_functions(tmp_path: Path) -> None:
    expected_properties = {
        "read_file": {"path", "offset", "limit"},
        "search_files": {
            "pattern",
            "file_glob",
            "limit",
            "offset",
            "output_mode",
            "path",
            "target",
        },
        "terminal": {"command", "background", "timeout", "workdir"},
        "write_file": {"path", "content"},
    }

    for name, properties in expected_properties.items():
        contribution = load_builtin(
            name,
            context=ToolLoadContext(workspace_root=tmp_path),
        )
        assert set(contribution.spec.input_schema["properties"]) == properties


def test_no_legacy_tool_tree_remains() -> None:
    legacy_root = PROJECT_ROOT / ".bianbu-agent" / "legacy-tools"
    tracked = subprocess.run(
        ["git", "ls-files", "--", str(legacy_root.relative_to(PROJECT_ROOT))],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert tracked.stdout == ""
