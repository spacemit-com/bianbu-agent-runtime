from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from bianbu_agent.mcp.managed import (
    load_managed_mcp_config,
    npx_package_invocation,
    parse_managed_mcp,
    write_managed_mcp_package,
)


def test_parses_quoted_stdio_command_and_writes_one_file_package(
    tmp_path: Path,
) -> None:
    definition = parse_managed_mcp(
        "Filesystem",
        'npx -y @modelcontextprotocol/server-filesystem "/path/with spaces"',
    )

    assert definition.plugin_id == "mcp.filesystem"
    assert definition.display_name == "Filesystem"
    assert definition.executable == "npx"
    assert definition.args == (
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/path/with spaces",
    )
    assert definition.command_line == (
        "npx -y @modelcontextprotocol/server-filesystem '/path/with spaces'"
    )

    package = write_managed_mcp_package(definition, tmp_path)

    assert [item.name for item in package.iterdir()] == ["mcp.json"]
    assert json.loads((package / "mcp.json").read_text(encoding="utf-8")) == {
        "display_name": "Filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": [
            "-y",
            "@modelcontextprotocol/server-filesystem",
            "/path/with spaces",
        ],
        "timeout_seconds": 30.0,
        "startup_timeout_seconds": 120.0,
    }


def test_managed_document_separates_startup_from_tool_budget(
    tmp_path: Path,
) -> None:
    """One shared number made a slow npx first start look like a broken server."""

    definition = parse_managed_mcp("Bilibili", "npx bilibili-mcp-js")

    config = load_managed_mcp_config(definition, workspace=tmp_path)

    assert config.timeout_seconds == 30.0
    assert config.startup_timeout_seconds == 120.0
    assert config.command == "npx"
    assert config.args == ("bilibili-mcp-js",)


@pytest.mark.parametrize(
    ("command", "package", "arguments"),
    [
        ("npx bilibili-mcp-js", "bilibili-mcp-js", ()),
        ("npx -y bilibili-mcp-js", "bilibili-mcp-js", ()),
        ("npx -y bilibili-mcp-js --port 1", "bilibili-mcp-js", ("--port", "1")),
        (
            "npx -y @modelcontextprotocol/server-filesystem /data/projects",
            "@modelcontextprotocol/server-filesystem",
            ("/data/projects",),
        ),
        ("npx @scope/pkg@1.2.3 script.py", "@scope/pkg@1.2.3", ("script.py",)),
        # The package is named in a flag, so the positional argument is a
        # different executable: installing by positional argument would vendor
        # the wrong thing.
        ("npx -p @scope/pkg pkg-cli", "", ()),
        ("npx --package=@scope/pkg pkg-cli", "", ()),
        ("npx -c 'pkg-cli --help'", "", ()),
        # Not a registry spec, not npm at all.
        ("npx /opt/local/server.js", "", ()),
        ("npx ./relative-server", "", ()),
        ("npx", "", ()),
        ("node server.js", "", ()),
        ("python3 -m weather_mcp", "", ()),
    ],
)
def test_npx_invocations_are_split_from_the_declarations_we_cannot_pin(
    command: str,
    package: str,
    arguments: tuple[str, ...],
) -> None:
    definition = parse_managed_mcp("Target", command)

    assert definition.npm_package == package
    assert definition.npm_arguments == arguments


def test_npx_package_invocation_reads_launcher_and_never_the_package_name() -> None:
    assert npx_package_invocation("/usr/local/bin/npx.cmd", ("pkg",)) == (
        "pkg",
        (),
    )
    assert npx_package_invocation("pnpm", ("dlx", "pkg")) is None


def test_non_ascii_name_uses_stable_hash_identifier() -> None:
    first = parse_managed_mcp("文件系统", "filesystem-server /data")
    second = parse_managed_mcp("文件系统", "another-server")

    assert re.fullmatch(r"mcp\.server-[0-9a-f]{8}", first.plugin_id)
    assert second.plugin_id == first.plugin_id


@pytest.mark.parametrize(
    "command",
    [
        "npx server && touch /tmp/x",
        "npx server || true",
        "npx server | tee x",
        "npx server > x",
        "npx server < x",
        "npx server; id",
        "npx `id`",
        "npx $(id)",
        "npx server\nwhoami",
        "npx server\rwhoami",
    ],
)
def test_rejects_shell_syntax(command: str) -> None:
    with pytest.raises(ValueError, match="shell syntax is not supported"):
        parse_managed_mcp("Unsafe", command)


@pytest.mark.parametrize(
    ("name", "command", "message"),
    [
        ("", "npx server", "name must be a non-empty string"),
        ("Filesystem", "", "command must be a non-empty string"),
        ("Filesystem", "\"unterminated", "command contains invalid quoting"),
    ],
)
def test_rejects_invalid_managed_input(
    name: object,
    command: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_managed_mcp(name, command)


def test_rejects_existing_generated_package(tmp_path: Path) -> None:
    definition = parse_managed_mcp("Filesystem", "npx server")
    (tmp_path / definition.plugin_id).mkdir()

    with pytest.raises(FileExistsError, match="already exists"):
        write_managed_mcp_package(definition, tmp_path)
