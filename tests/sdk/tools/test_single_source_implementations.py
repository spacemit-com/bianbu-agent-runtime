from __future__ import annotations

import ast
from pathlib import Path
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOOL_ROOT = PROJECT_ROOT / ".bianbu-agent" / "tools"


def _production_python_files() -> tuple[Path, ...]:
    roots = (
        PROJECT_ROOT / "agent",
        PROJECT_ROOT / "agent_core",
        PROJECT_ROOT / "bianbu_agent",
        PROJECT_ROOT / "hermes_cli",
        PROJECT_ROOT / "plugins",
        PROJECT_ROOT / "providers",
    )
    files = [
        path
        for root in roots
        if root.is_dir()
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ]
    files.extend(
        path
        for path in (PROJECT_ROOT / "model_tools.py", PROJECT_ROOT / "toolsets.py")
        if path.is_file()
    )
    return tuple(sorted(files))


def _legacy_imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        names: tuple[str, ...] = ()
        if isinstance(node, ast.Import):
            names = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = (node.module,)
        for name in names:
            if (
                name == "tools"
                or name.startswith("tools.")
                or name in {"model_tools", "toolsets"}
            ):
                found.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}:{name}")
    return tuple(found)


def test_root_tool_registry_surface_is_removed() -> None:
    assert not (PROJECT_ROOT / "tools").exists()
    assert not (PROJECT_ROOT / "model_tools.py").exists()
    assert not (PROJECT_ROOT / "toolsets.py").exists()
    for relative in (
        "bianbu_agent/tools/registration.py",
        "bianbu_agent/tools/definition_loader.py",
        "bianbu_agent/tools/kanban.py",
        "bianbu_agent/tools/mutation_guard.py",
        "bianbu_agent/tools/background_workspace",
    ):
        assert not (PROJECT_ROOT / relative).exists()


def test_production_does_not_import_the_removed_tool_registry() -> None:
    violations = [
        violation
        for path in _production_python_files()
        for violation in _legacy_imports(path)
    ]

    assert violations == []


def test_active_tools_use_only_owned_python_sources_and_no_package_artifacts() -> None:
    from bianbu_agent.tools.builtins import BUILTIN_MODULES, builtin_source
    assert not list(TOOL_ROOT.glob("*/tool.py"))
    assert len(BUILTIN_MODULES) == 34
    for name in BUILTIN_MODULES:
        source = builtin_source(name)
        assert source.is_file()
        assert _legacy_imports(source) == ()
    clients = tuple((PROJECT_ROOT / 'bianbu_agent/tools/browser').glob('client.py'))
    assert len(clients) == 1
