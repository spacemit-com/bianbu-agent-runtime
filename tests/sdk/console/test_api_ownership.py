from __future__ import annotations

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[3]


def test_console_package_retains_no_http_business_adapter() -> None:
    console_root = PROJECT_ROOT / "bianbu_agent" / "console"

    assert not (console_root / "router.py").exists()
    assert not (console_root / "facade.py").exists()


def test_application_installs_only_the_resource_api() -> None:
    app_source = PROJECT_ROOT / "bianbu_agent" / "server" / "app.py"
    tree = ast.parse(app_source.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "install_api_routes" in imported_names
    assert "install_console_routes" not in imported_names
