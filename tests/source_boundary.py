from __future__ import annotations

import ast
from pathlib import Path


REMOVED_ROOTS = frozenset(
    {"agent", "cron", "hermes_cli", "plugins", "providers"}
)


def source_violations(path: Path, *, source_root: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module]
        for name in names:
            if name.partition(".")[0] in REMOVED_ROOTS:
                violations.append(f"{path.relative_to(source_root)}:{node.lineno}: {name}")
    return violations


def wheel_package_paths(project_root: Path) -> tuple[Path, ...]:
    return (project_root / "bianbu_agent",)
