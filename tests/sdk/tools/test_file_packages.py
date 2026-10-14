from __future__ import annotations

import json
from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent import ToolCallContext
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext, ToolRuntimeFactory


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, workspace: Path):
    root = name
    return load_builtin(
        root,
        context=ToolLoadContext(
            workspace_root=workspace,
            services={"runtime_factory": ToolRuntimeFactory(workspace_root=workspace)},
        ),
    )


@pytest.mark.asyncio
async def test_write_and_read_file_are_owned_by_their_single_sources(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    call = ToolCallContext("run", "session", "agent")

    written = await _load("write_file", workspace).handler(
        call,
        {"path": "nested/example.txt", "content": "alpha\nbeta\ngamma\n"},
    )
    read = await _load("read_file", workspace).handler(
        call,
        {"path": "nested/example.txt", "offset": 2, "limit": 1},
    )

    written_payload = json.loads(written.content)
    assert written_payload["bytes_written"] == 17
    assert written_payload["files_modified"] == [
        str(workspace / "nested" / "example.txt")
    ]
    assert json.loads(read.content) == {
        "content": "2|beta",
        "total_lines": 3,
        "file_size": 17,
        "truncated": True,
        "hint": "Use offset=3 to continue reading (showing 2-2 of 3 lines)",
    }


@pytest.mark.asyncio
async def test_file_packages_reject_paths_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")

    result = await _load("read_file", workspace).handler(
        ToolCallContext("run", "session", "agent"),
        {"path": str(outside)},
    )

    payload = json.loads(result.content)
    assert payload["success"] is False
    assert payload["code"] == "path_outside_workspace"


@pytest.mark.asyncio
async def test_read_file_reports_binary_content(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "binary.bin").write_bytes(b"abc\0def")

    result = await _load("read_file", workspace).handler(
        ToolCallContext("run", "session", "agent"),
        {"path": "binary.bin"},
    )

    assert json.loads(result.content) == {
        "is_binary": True,
        "file_size": 7,
        "error": "Binary file - cannot display as text.",
    }


@pytest.mark.asyncio
async def test_search_files_does_not_follow_file_symlinks_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("outside-secret", encoding="utf-8")
    (workspace / "linked.txt").symlink_to(outside)

    result = await _load("search_files", workspace).handler(
        ToolCallContext("run", "session", "agent"),
        {"pattern": "outside-secret", "path": "."},
    )

    assert json.loads(result.content) == {"total_count": 0, "matches": []}


@pytest.mark.asyncio
async def test_search_files_paginates_unique_file_results(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
    (workspace / "b.txt").write_text("needle\n", encoding="utf-8")

    result = await _load("search_files", workspace).handler(
        ToolCallContext("run", "session", "agent"),
        {
            "pattern": "needle",
            "path": ".",
            "output_mode": "files_only",
            "offset": 1,
            "limit": 1,
        },
    )

    assert json.loads(result.content) == {
        "total_count": 2,
        "files": ["b.txt"],
        "truncated": False,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "target"),
    (("victim.txt", "victim.txt"), ("source.txt", "existing.txt")),
)
async def test_patch_move_rejects_data_losing_destinations(
    tmp_path: Path,
    source: str,
    target: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / source).write_text("source-content\n", encoding="utf-8")
    if target != source:
        (workspace / target).write_text("target-content\n", encoding="utf-8")

    result = await _load("patch", workspace).handler(
        ToolCallContext("run", "session", "agent"),
        {
            "mode": "patch",
            "patch": (
                f"*** Begin Patch\n*** Move File: {source} -> {target}\n*** End Patch"
            ),
        },
    )

    payload = json.loads(result.content)
    assert payload["success"] is False
    assert payload["code"] == "invalid_request"
    assert (workspace / source).read_text(encoding="utf-8") == "source-content\n"
    if target != source:
        assert (workspace / target).read_text(encoding="utf-8") == "target-content\n"
