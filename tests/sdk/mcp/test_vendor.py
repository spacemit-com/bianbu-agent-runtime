from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from bianbu_agent.mcp import vendor as vendor_module
from bianbu_agent.mcp.vendor import (
    NpmUnavailableError,
    _resolve_executable,
    _run_npm_install,
    remove_vendored_server,
    vendor_npm_server,
)


def _write_package(root: Path, manifest: dict[str, object], *executables: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name in executables:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return root


def _fake_npm(
    monkeypatch: pytest.MonkeyPatch,
    manifest: dict[str, object],
    *executables: str,
    package: str = "bilibili-mcp-js",
) -> None:
    monkeypatch.setattr(
        vendor_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}",
    )

    def install(_npm: str, _spec: str, staging: Path) -> None:
        _write_package(
            staging / "node_modules" / package,
            manifest,
            *executables,
        )

    monkeypatch.setattr(vendor_module, "_run_npm_install", install)


def test_vendor_publishes_the_executable_npm_would_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_npm(
        monkeypatch,
        {"name": "bilibili-mcp-js", "bin": "dist/index.js"},
        "dist/index.js",
    )

    vendored = vendor_npm_server("bilibili-mcp-js", tmp_path / "mcp.bilibili")

    assert vendored.interpreter == Path("/usr/bin/node")
    assert vendored.script == (
        tmp_path / "mcp.bilibili" / "node_modules" / "bilibili-mcp-js" / "dist" / "index.js"
    )
    assert vendored.script.is_file()
    assert list(tmp_path.iterdir()) == [tmp_path / "mcp.bilibili"]


def test_vendor_replaces_an_existing_tree_only_after_npm_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "mcp.bilibili"
    (destination / "node_modules").mkdir(parents=True)
    (destination / "stale").write_text("old", encoding="utf-8")
    _fake_npm(
        monkeypatch,
        {"name": "bilibili-mcp-js", "bin": "index.js"},
        "index.js",
    )

    vendor_npm_server("bilibili-mcp-js", destination)

    assert not (destination / "stale").exists()
    assert (destination / "node_modules" / "bilibili-mcp-js" / "index.js").is_file()


def test_vendor_leaves_no_tree_behind_when_npm_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(vendor_module.shutil, "which", lambda name: f"/usr/bin/{name}")

    def failing_install(_npm: str, spec: str, staging: Path) -> None:
        (staging / "node_modules").mkdir(parents=True)
        raise RuntimeError(f"npm install {spec!r} failed (exit 1): boom")

    monkeypatch.setattr(vendor_module, "_run_npm_install", failing_install)

    with pytest.raises(RuntimeError, match="exit 1"):
        vendor_npm_server("bilibili-mcp-js", tmp_path / "mcp.bilibili")

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("missing", ("npm", "node"))
def test_vendor_reports_a_missing_package_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    monkeypatch.setattr(
        vendor_module.shutil,
        "which",
        lambda name: None if name == missing else f"/usr/bin/{name}",
    )

    with pytest.raises(NpmUnavailableError, match=missing):
        vendor_npm_server("bilibili-mcp-js", tmp_path / "mcp.bilibili")


def test_vendor_rejects_a_spec_that_names_no_package(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported npm package spec"):
        vendor_npm_server("   ", tmp_path / "mcp.broken")


def test_object_bin_is_selected_by_the_package_base_name(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "node_modules" / "@scope" / "pkg",
        {"name": "@scope/pkg", "bin": {"pkg": "cli.js", "other": "other.js"}},
        "cli.js",
        "other.js",
    )

    assert _resolve_executable(root, "@scope/pkg") == root / "cli.js"


def test_single_entry_object_bin_needs_no_name_match(tmp_path: Path) -> None:
    root = _write_package(
        tmp_path / "node_modules" / "pkg",
        {"name": "pkg", "bin": {"renamed-cli": "cli.js"}},
        "cli.js",
    )

    assert _resolve_executable(root, "pkg") == root / "cli.js"


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ({"name": "pkg"}, "declares no executable"),
        ({"name": "pkg", "bin": {"a": "a.js", "b": "b.js"}}, "several executables"),
        ({"name": "pkg", "bin": "missing.js"}, "is missing"),
        ({"name": "pkg", "bin": "../outside.js"}, "declares an executable outside"),
    ],
)
def test_resolve_executable_refuses_what_it_cannot_launch(
    tmp_path: Path,
    manifest: dict[str, object],
    message: str,
) -> None:
    root = tmp_path / "node_modules" / "pkg"
    root.mkdir(parents=True)
    (root / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
    if manifest.get("bin") == "missing.js":
        pass
    elif "a.js" in str(manifest.get("bin")):
        (root / "a.js").write_text("", encoding="utf-8")
        (root / "b.js").write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        _resolve_executable(root, "pkg")


def test_resolve_executable_reports_a_manifest_without_a_package(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="no package directory"):
        _resolve_executable(tmp_path / "node_modules" / "pkg", "pkg")

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "package.json").write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="invalid package.json"):
        _resolve_executable(broken, "pkg")


def test_removing_a_vendored_server_ignores_a_missing_tree(tmp_path: Path) -> None:
    assert remove_vendored_server(tmp_path / "absent") is False

    existing = tmp_path / "present"
    existing.mkdir()
    assert remove_vendored_server(existing) is True
    assert not existing.exists()


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is not installed")
def test_real_npm_install_produces_the_entry_point(tmp_path: Path) -> None:
    """npm itself must agree with our reading of its bin rules."""

    source = _write_package(
        tmp_path / "source" / "bilibili-mcp-js",
        {
            "name": "bilibili-mcp-js",
            "version": "1.0.0",
            "bin": {"bilibili-mcp": "dist/index.js"},
        },
        "dist/index.js",
    )
    staging = tmp_path / "staging"

    _run_npm_install(shutil.which("npm"), str(source), staging)

    script = _resolve_executable(
        staging / "node_modules" / "bilibili-mcp-js",
        "bilibili-mcp-js",
    )
    assert script.is_file()
    assert script.read_text(encoding="utf-8")
