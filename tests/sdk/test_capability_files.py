"""能力目录读取器：文件枚举的确定性、跳过项与拒绝项，以及摘要不变。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from bianbu_agent._internal.filesystem import (
    UnsafeFilesystemEntry,
    digest_directory,
    regular_files,
)


def _write(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_regular_files_returns_sorted_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write(root, "plugin.yaml", "name: demo")
    _write(root, "hooks/AfterRun/hook.json", "{}")
    _write(root, "scripts/after_run.py", "print('x')")
    _write(root, "SKILL.md", "---\nname: demo\n---\n")

    files = regular_files(root)

    assert [path.relative_to(root).as_posix() for path in files] == [
        "SKILL.md",
        "hooks/AfterRun/hook.json",
        "plugin.yaml",
        "scripts/after_run.py",
    ]
    assert all(path.is_absolute() for path in files)


def test_regular_files_skips_pycache_at_any_depth(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write(root, "plugin.yaml", "name: demo")
    _write(root, "scripts/after_run.py", "print('x')")
    _write(root, "scripts/__pycache__/after_run.cpython-314.pyc", "bytecode")
    _write(root, "__pycache__/top.pyc", "bytecode")

    files = regular_files(root)

    assert [path.relative_to(root).as_posix() for path in files] == [
        "plugin.yaml",
        "scripts/after_run.py",
    ]


def test_regular_files_rejects_symlinks_instead_of_following(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write(root, "plugin.yaml", "name: demo")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)
    (root / "dangling.txt").symlink_to(tmp_path / "missing.txt")

    with pytest.raises(UnsafeFilesystemEntry, match="symbolic links are not allowed"):
        regular_files(root)


def test_regular_files_rejects_non_regular_entries(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write(root, "plugin.yaml", "name: demo")
    if not hasattr(os, "mkfifo"):  # pragma: no cover - platform guard
        pytest.skip("platform has no mkfifo")
    os.mkfifo(root / "pipe")

    with pytest.raises(UnsafeFilesystemEntry, match="unsupported capability"):
        regular_files(root)


def test_regular_files_rejects_case_insensitive_duplicates(tmp_path: Path) -> None:
    root = tmp_path / "plugin"
    _write(root, "Readme.md", "# a")
    _write(root, "README.md", "# b")

    with pytest.raises(ValueError, match="duplicate case-insensitive resource path"):
        regular_files(root)


def test_regular_files_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="capability root is not a directory"):
        regular_files(tmp_path / "absent")


def test_digest_directory_matches_reference_order(tmp_path: Path) -> None:
    """摘要必须仍然按 (路径, 大小, 内容) 的排序结果累积。"""

    root = tmp_path / "plugin"
    _write(root, "plugin.yaml", "name: demo")
    _write(root, "scripts/after_run.py", "print('x')")
    _write(root, "scripts/__pycache__/after_run.cpython-314.pyc", "bytecode")

    expected = hashlib.sha256()
    for relative in ("plugin.yaml", "scripts/after_run.py"):
        path = root / relative
        encoded = relative.encode("utf-8")
        expected.update(len(encoded).to_bytes(8, "big"))
        expected.update(encoded)
        expected.update(path.stat().st_size.to_bytes(8, "big"))
        expected.update(path.read_bytes())

    assert digest_directory(root, regular_files(root)) == expected.hexdigest()
