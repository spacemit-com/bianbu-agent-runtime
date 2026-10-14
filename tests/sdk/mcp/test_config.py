from __future__ import annotations

import json
from pathlib import Path
import re

import pytest

from bianbu_agent.mcp.config import MCPServerConfig, load_mcp_config, validate_mcp_config


def _write(path: Path, document: object) -> Path:
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_loads_stdio_config_and_resolves_runtime_placeholders(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = _write(
        tmp_path / "mcp.json",
        {
            "display_name": "Managed Filesystem",
            "transport": "stdio",
            "command": "uvx",
            "args": ["server", "${workspace}"],
            "env": {"TOKEN": "${MCP_TOKEN}", "ROOT": "${workspace}"},
            "timeout_seconds": 12.5,
        },
    )

    config = load_mcp_config(
        path,
        workspace=workspace,
        environ={"MCP_TOKEN": "secret-value"},
    )

    assert config == MCPServerConfig(
        display_name="Managed Filesystem",
        transport="stdio",
        command="uvx",
        args=("server", str(workspace.resolve())),
        env={"TOKEN": "secret-value", "ROOT": str(workspace.resolve())},
        timeout_seconds=12.5,
    )


def test_rejects_empty_display_name(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {"display_name": " ", "transport": "stdio", "command": "server"},
    )

    with pytest.raises(ValueError, match="display_name must be a non-empty string"):
        load_mcp_config(path, workspace=tmp_path)


def test_loads_streamable_http_config(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {
            "transport": "streamable-http",
            "url": "https://mcp.example.test/rpc",
            "headers": {"Authorization": "Bearer ${TOKEN}"},
        },
    )

    config = load_mcp_config(
        path,
        workspace=tmp_path,
        environ={"TOKEN": "abc"},
    )

    assert config == MCPServerConfig(
        transport="streamable-http",
        url="https://mcp.example.test/rpc",
        headers={"Authorization": "Bearer abc"},
        timeout_seconds=30.0,
    )


def test_install_validation_defers_environment_only_http_url(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {"transport": "streamable-http", "url": "${MCP_URL}"},
    )

    validated = validate_mcp_config(path, workspace=tmp_path)
    loaded = load_mcp_config(
        path,
        workspace=tmp_path,
        environ={"MCP_URL": "https://mcp.example.test/rpc"},
    )

    assert validated.url == "${MCP_URL}"
    assert loaded.url == "https://mcp.example.test/rpc"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"transport": "stdio", "command": "python", "extra": True}, "unknown mcp.json key: extra"),
        ({"transport": "stdio", "command": ""}, "command must be a non-empty string"),
        ({"transport": "stdio", "command": "python", "args": "server.py"}, "args must be an array of strings"),
        ({"transport": "stdio", "command": "python", "env": {"TOKEN": 1}}, "env values must be strings"),
        ({"transport": "streamable-http", "url": "file:///tmp/socket"}, "url must be an HTTP(S) URL"),
        ({"transport": "streamable-http", "url": "https://example.test", "headers": []}, "headers must be an object of strings"),
        ({"transport": "stdio", "command": "python", "timeout_seconds": 0}, "timeout_seconds must be positive"),
        ({"transport": "ftp", "url": "https://example.test"}, "transport must be stdio, streamable-http, or sse"),
    ],
)
def test_rejects_invalid_config(
    tmp_path: Path,
    document: object,
    message: str,
) -> None:
    path = _write(tmp_path / "mcp.json", document)

    with pytest.raises(ValueError, match=re.escape(message)):
        load_mcp_config(path, workspace=tmp_path, environ={})


def test_missing_environment_variable_names_only_the_missing_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "mcp.json",
        {
            "transport": "stdio",
            "command": "python",
            "env": {"TOKEN": "prefix-${MISSING_TOKEN}"},
        },
    )

    with pytest.raises(ValueError) as caught:
        load_mcp_config(path, workspace=tmp_path, environ={"OTHER_SECRET": "hidden"})

    assert str(caught.value) == "environment variable is not set: MISSING_TOKEN"
    assert "hidden" not in str(caught.value)


def test_rejects_malformed_json_and_non_object_documents(tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    array = _write(tmp_path / "array.json", [])

    with pytest.raises(ValueError, match="invalid mcp.json"):
        load_mcp_config(malformed, workspace=tmp_path)
    with pytest.raises(ValueError, match="mcp.json must contain an object"):
        load_mcp_config(array, workspace=tmp_path)
