import ast
from pathlib import Path


def test_runtime_does_not_import_product_or_removed_runtime_roots() -> None:
    offenders: list[Path] = []
    for path in Path("bianbu_agent").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ] + [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        if any(name.partition(".")[0] in {
            "agent", "agent_core", "cron", "hermes_cli", "plugins", "providers"
        } for name in imports):
            offenders.append(path)

    assert offenders == []


def test_removed_generic_and_specialized_public_packages_stay_private() -> None:
    root = Path("bianbu_agent")

    assert not (root / "capabilities").exists()
    assert not (root / "memory").exists()
    assert (root / "security").is_dir()
    assert not (root / "audit").exists()
    assert not (root / "managers.py").exists()


def test_console_does_not_import_legacy_runtime() -> None:
    offenders: list[tuple[Path, str]] = []
    roots = (Path("bianbu_agent/console"),)
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ] + [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ]
            offenders.extend(
                (path, name)
                for name in imports
                if name == "bianbu_agent._legacy"
                or name.startswith("bianbu_agent._legacy.")
            )

    assert offenders == []


def test_runtime_control_paths_do_not_import_securityd_internals() -> None:
    roots = (
        Path("bianbu_agent"),
        Path(".bianbu-agent/plugins/bianbu.security/lib"),
    )
    offenders = []
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            ] + [
                node.module or ""
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
            ]
            if any(
                name == "securityd" or name.startswith("securityd.")
                for name in imports
            ):
                offenders.append(str(path))
    allowed_transition = {
        ".bianbu-agent/plugins/bianbu.security/lib/uds_transport.py",
        ".bianbu-agent/plugins/bianbu.security/lib/bridge.py",
        ".bianbu-agent/plugins/bianbu.security/lib/mapping.py",
        "bianbu_agent/security/security_skill_dlp.py",
    }
    assert set(offenders) <= allowed_transition
