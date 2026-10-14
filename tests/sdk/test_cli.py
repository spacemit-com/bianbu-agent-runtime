from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from bianbu_agent import AgentHome, AgentManifest, AgentModel, YAMLModelStore, ModelConfig
from bianbu_agent.plugins import PluginInstaller
from bianbu_agent.cli import build_parser, main_async


# A real stdio MCP server, used by the tests that start one through the API.
_STDIO_FIXTURE = Path(__file__).resolve().parent / "mcp" / "fixtures" / "stdio_server.py"


def _config(tmp_path: Path) -> tuple[Path, AgentHome]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": "./data",
                "service": {"host": "127.0.0.1", "port": 18765},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_path, AgentHome(tmp_path / "data").prepare()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ("list", "show"))
async def test_runtime_cli_model_reads_redact_stored_credentials(
    tmp_path: Path,
    action: str,
) -> None:
    config_path, home = _config(tmp_path)
    store = YAMLModelStore(home.models_dir)
    await store.open()
    try:
        await store.create(
            ModelConfig(
                id="provider-synthetic",
                provider="synthetic",
                model="credential",
                base_url=(
                    "https://user:password@models.example.test/v1?access=query#private"
                ),
                parameters={
                    "record_kind": "provider_credential",
                    "api_key": "synthetic-secret",
                    "authorization": "synthetic-authorization",
                    "unknown": "synthetic-unknown",
                },
            )
        )
    finally:
        await store.close()

    command = [
        sys.executable,
        "-m",
        "bianbu_agent.cli.main",
        "--config",
        str(config_path),
        "model",
        action,
    ]
    if action == "show":
        command.append("provider-synthetic")
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    for sentinel in (
        "synthetic-secret",
        "synthetic-authorization",
        "synthetic-unknown",
        "user",
        "password",
        "query",
    ):
        assert sentinel not in completed.stdout
        assert sentinel not in completed.stderr
    payload = json.loads(completed.stdout)
    projected = payload[0] if action == "list" else payload
    assert projected["base_url"] == "https://models.example.test/v1"
    assert projected["parameters"] == {"record_kind": "provider_credential"}


@pytest.mark.asyncio
async def test_runtime_cli_model_create_redacts_store_result(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _ = _config(tmp_path)
    secret = "synthetic-create-secret"

    async def create_with_credentials(
        _store: YAMLModelStore,
        config: ModelConfig,
    ) -> ModelConfig:
        return ModelConfig(
            id=config.id,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            parameters={"api_key": secret, "capability": "chat", "visible": "private"},
        )

    monkeypatch.setattr(YAMLModelStore, "create", create_with_credentials)

    assert await main_async(
        [
            "--config",
            str(config_path),
            "model",
            "create",
            "synthetic-model",
            "synthetic",
            "demo",
        ]
    ) == 0

    output = capsys.readouterr().out
    assert secret not in output
    assert json.loads(output)["parameters"] == {}


@pytest.mark.asyncio
async def test_plugin_effective_cli_uses_runtime_and_home_layers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, home = _config(tmp_path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["plugins"] = {"vendor.bundle": {"mode": "runtime"}}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    source = tmp_path / "source"
    source.mkdir()
    (source / "plugin.yaml").write_text(
        """\
schema_version: 1
name: vendor.bundle
version: 1.2.0
config_schema:
  type: object
  properties:
    mode: {type: string, default: default}
""",
        encoding="utf-8",
    )
    PluginInstaller(home).install(source)
    (home.root / "plugins.yaml").write_text(
        """\
schema_version: 1
plugins:
  vendor.bundle:
    config: {mode: home}
""",
        encoding="utf-8",
    )

    assert await main_async(
        ["--config", str(config_path), "plugin", "effective"]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "contributions": {
                "hooks": [],
                "mcps": [],
                "skills": [],
                "tools": [],
            },
            "dependency_order": 0,
            "id": "vendor.bundle",
            "version": "1.2.0",
            "source": str(home.plugins_dir / "vendor.bundle"),
            "generation_id": None,
            "state": "configured",
            "enabled": True,
            "dependencies": [],
            "config": {"mode": "home"},
            "provenance": {"mode": "home"},
            "members": [],
        }
    ]


@pytest.mark.asyncio
async def test_resource_lists_exclude_plugin_members_while_plugin_list_keeps_kinds(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path, home = _config(tmp_path)
    source = tmp_path / "plugin-source"
    (source / "skills" / "review").mkdir(parents=True)
    (source / "mcps" / "weather").mkdir(parents=True)
    (source / "plugin.yaml").write_text(
        "schema_version: 1\nname: office.core\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (source / "skills" / "review" / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review files\n---\n\n# Review\n",
        encoding="utf-8",
    )
    (source / "mcps" / "weather" / "mcp.json").write_text(
        '{"transport":"stdio","command":"fake"}',
        encoding="utf-8",
    )
    PluginInstaller(home).install(source)

    assert await main_async(["--config", str(config_path), "skill", "list"]) == 0
    skills = json.loads(capsys.readouterr().out)
    assert all(item["capability_id"] != "office.core/review" for item in skills)

    assert await main_async(["--config", str(config_path), "mcp", "list"]) == 0
    mcps = json.loads(capsys.readouterr().out)
    assert all(item["id"] != "office.core/weather" for item in mcps)

    assert await main_async(["--config", str(config_path), "plugin", "list"]) == 0
    plugins = json.loads(capsys.readouterr().out)
    members = next(item for item in plugins if item["manifest"]["id"] == "office.core")["members"]
    assert {(item["kind"], item["local_name"]) for item in members} == {
        ("mcp", "weather"),
        ("skill", "review"),
    }


@pytest.mark.parametrize("action", ("show", "verify"))
def test_runtime_cli_mcp_drops_retired_subcommands(action: str) -> None:
    """No caller needs the retired package-oriented show/verify verbs."""

    parser = build_parser()
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(["--config", "/tmp/config.yaml", "mcp", action, "weather"])

    assert failure.value.code == 2


@pytest.mark.asyncio
async def test_runtime_cli_mcp_installs_an_apm_package_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """APM stages a package directory and calls `mcp install` on that path.

    Dropping this verb breaks every MCP install from AgentHub, so the contract
    is pinned here rather than left to the package manager's own tests.
    """

    config_path, home = _config(tmp_path)
    source = tmp_path / "apm-staging" / "mcp.nws"
    source.mkdir(parents=True)
    (source / "mcp.json").write_text(
        json.dumps({
            "display_name": "nws",
            "transport": "streamable-http",
            "timeout_seconds": 30.0,
            "url": "https://nws.caseyjhand.com/mcp",
        }),
        encoding="utf-8",
    )

    assert await main_async(
        ["--config", str(config_path), "mcp", "install", str(source)]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "mcp.nws"
    document = json.loads(
        (home.mcps_dir / "mcp.nws" / "mcp.json").read_text(encoding="utf-8")
    )
    assert document["url"] == "https://nws.caseyjhand.com/mcp"


@pytest.mark.asyncio
async def test_runtime_cli_add_registers_a_managed_mcp(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """``add`` is AgentHub's install verb, so it must return a declaration.

    AgentHub kills the Agent CLI after 30s, while preparing an npm-backed
    server needs longer than that on a cold cache.  Everything expensive has to
    happen after this command returned, so it only registers what it was given.
    """

    config_path, home = _config(tmp_path)

    assert await main_async(
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "Filesystem",
            "--command",
            sys.executable,
            "--args",
            str(_STDIO_FIXTURE),
        ]
    ) == 0

    payload = json.loads(capfd.readouterr().out)
    assert payload["id"] == "mcp.filesystem"
    # The declaration is installed; the Runtime starts the server and reports
    # what it found as MCP health.
    assert payload["status"] == "preparing"
    assert payload["tools"] == []
    assert payload["detail"] == ""
    assert payload["vendored"] is False
    assert payload["startup_timeout_seconds"] == 120.0
    document = json.loads(
        (home.mcps_dir / "mcp.filesystem" / "mcp.json").read_text(encoding="utf-8")
    )
    assert document == {
        "display_name": "Filesystem",
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(_STDIO_FIXTURE)],
        "timeout_seconds": 30.0,
        "startup_timeout_seconds": 120.0,
    }


@pytest.mark.asyncio
async def test_runtime_cli_add_never_runs_npm(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The npm work is what made ``add`` outlive AgentHub's 30s timeout."""

    config_path, home = _config(tmp_path)
    monkeypatch.setattr(
        "bianbu_agent.mcp.install.vendor_npm_server",
        lambda *_args, **_kwargs: pytest.fail(
            "mcp add must not vendor; the Runtime prepares in the background"
        ),
    )
    monkeypatch.setattr(
        "bianbu_agent.mcp.install.probe_mcp_server",
        lambda **_kwargs: pytest.fail("mcp add must not start the server"),
    )

    assert await main_async(
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "Bilibili",
            "--command",
            "npx",
            "--args",
            "-y",
            "bilibili-mcp-js",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"] == "mcp.bilibili"
    assert payload["status"] == "preparing"
    assert payload["vendored"] is False
    assert not home.mcp_runtime_dir.exists()
    document = json.loads(
        (home.mcps_dir / "mcp.bilibili" / "mcp.json").read_text(encoding="utf-8")
    )
    # The declaration keeps naming the package; the Runtime resolves it.
    assert document["command"] == "npx"
    assert document["args"] == ["-y", "bilibili-mcp-js"]

    # Removing it still drops whatever a previous Runtime version vendored.
    (home.mcp_runtime_dir / "mcp.bilibili").mkdir(parents=True)
    assert await main_async(
        ["--config", str(config_path), "mcp", "remove", "Bilibili"]
    ) == 0
    capsys.readouterr()
    assert not (home.mcp_runtime_dir / "mcp.bilibili").exists()


@pytest.mark.asyncio
async def test_runtime_cli_add_installs_a_server_it_cannot_start(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A server that cannot start is now the Runtime's finding, not add's.

    ``add`` must not refuse a declaration it cannot prove: the only place that
    can judge it is the Runtime, which reports the failure as MCP health
    (``detail``) instead of as a failed install.
    """

    config_path, home = _config(tmp_path)

    assert await main_async(
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "Broken",
            "--command",
            "/nonexistent/managed-mcp",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "preparing"
    document = json.loads(
        (home.mcps_dir / "mcp.broken" / "mcp.json").read_text(encoding="utf-8")
    )
    assert document["command"] == "/nonexistent/managed-mcp"


@pytest.mark.asyncio
async def test_runtime_cli_no_verify_is_still_accepted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AgentHub callers keep working: verification moved to the Runtime."""

    config_path, home = _config(tmp_path)

    assert await main_async(
        [
            "--config",
            str(config_path),
            "mcp",
            "add",
            "Pending",
            "--command",
            "/nonexistent/managed-mcp",
            "--no-verify",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "preparing"
    assert payload["tools"] == []
    assert payload["detail"] == ""
    assert (home.mcps_dir / "mcp.pending" / "mcp.json").is_file()


@pytest.mark.asyncio
async def test_runtime_cli_upgrade_replaces_an_installed_mcp(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without --upgrade a second add must refuse instead of half-replacing."""

    config_path, home = _config(tmp_path)
    base = ["--config", str(config_path), "mcp", "add", "Weather", "--command"]
    assert await main_async([*base, "weather-mcp", "--no-verify"]) == 0
    capsys.readouterr()

    with pytest.raises(ValueError, match="already exists"):
        await main_async([*base, "other-mcp"])

    assert await main_async(
        [*base, "other-mcp", "--upgrade", "--no-verify"]
    ) == 0
    capsys.readouterr()
    document = json.loads(
        (home.mcps_dir / "mcp.weather" / "mcp.json").read_text(encoding="utf-8")
    )
    assert document["command"] == "other-mcp"


@pytest.mark.asyncio
async def test_runtime_cli_mcp_list_includes_global_and_user_servers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP catalog must expose system/global declarations like other resources."""

    config_path, home = _config(tmp_path)
    from bianbu_agent.mcp import system

    global_root = tmp_path / "global-mcps" / "mcp.nws"
    global_root.mkdir(parents=True)
    (global_root / "mcp.json").write_text(
        json.dumps({
            "display_name": "nws",
            "transport": "streamable-http",
            "url": "https://nws.caseyjhand.com/mcp",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(system, "SYSTEM_MCP_ROOT", global_root.parent)

    await main_async([
        "--config", str(config_path), "mcp", "add", "local",
        "--command", "/bin/true", "--no-verify",
    ])
    capsys.readouterr()
    await main_async(["--config", str(config_path), "mcp", "list"])
    listed = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in listed} == {"mcp.local", "mcp.nws"}


@pytest.mark.asyncio
async def test_runtime_cli_required_nws_remote_mcp_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep the published nws HTTPS MCP installation command executable."""

    config_path, home = _config(tmp_path)
    assert await main_async([
        "--config", str(config_path), "mcp", "add", "nws",
        "--url", "https://nws.caseyjhand.com/mcp",
    ]) == 0
    capsys.readouterr()
    document = json.loads(
        (home.mcps_dir / "mcp.nws" / "mcp.json").read_text(encoding="utf-8")
    )
    assert document["transport"] == "streamable-http"
    assert document["url"] == "https://nws.caseyjhand.com/mcp"


@pytest.mark.asyncio
async def test_runtime_cli_gets_and_removes_managed_mcp_by_short_name(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Requiring the internal mcp. prefix would break the public short-name CLI."""

    config_path, home = _config(tmp_path)
    assert await main_async(
            ["--config", str(config_path), "mcp", "add", "weather",
             "--command", "weather-mcp", "--no-verify"]
    ) == 0
    capsys.readouterr()

    assert await main_async(
        ["--config", str(config_path), "mcp", "get", "weather"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "mcp.weather"

    assert await main_async(
        ["--config", str(config_path), "mcp", "remove", "weather"]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": True}
    assert not (home.mcps_dir / "mcp.weather").exists()


def test_runtime_cli_mcp_help_exposes_command_based_management() -> None:
    """Dropping a public MCP action would make the packaged CLI undiscoverable."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            "/tmp/config.yaml",
            "mcp",
            "add",
            "filesystem",
            "--command",
            "server-command",
        ]
    )

    assert args.resource == "mcp"
    assert args.action == "add"
    assert args.name == "filesystem"
    assert args.command == "server-command"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "mcp_id"),
    (("foo.bar", "foo.bar"), ("mcp.filesystem", "mcp.filesystem")),
)
async def test_runtime_cli_uses_one_mcp_id_for_add_and_get(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    name: str,
    mcp_id: str,
) -> None:
    """Divergent add/get normalization would make an installed MCP unreachable."""

    config_path, _ = _config(tmp_path)
    assert await main_async(
        ["--config", str(config_path), "mcp", "add", name,
         "--command", "/bin/true", "--no-verify"]
    ) == 0
    assert json.loads(capsys.readouterr().out)["id"] == mcp_id

    assert await main_async(
        ["--config", str(config_path), "mcp", "get", name]
    ) == 0
    assert json.loads(capsys.readouterr().out)["id"] == mcp_id


def test_runtime_cli_mcp_add_requires_a_command_at_parse_time() -> None:
    """Accepting an empty command would expose an uncaught CLI traceback."""

    parser = build_parser()
    with pytest.raises(SystemExit) as failure:
        parser.parse_args(
            ["--config", "/tmp/config.yaml", "mcp", "add", "filesystem"]
        )

    assert failure.value.code == 2


@pytest.mark.asyncio
async def test_runtime_cli_agent_create_persists_structured_provider_and_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI Agent creation uses the same Provider/Model terminology and schema as HTTP."""

    config_path, home = _config(tmp_path)

    assert await main_async(
        [
            "--config",
            str(config_path),
            "agent",
            "create",
            "Writer",
            "--provider",
            "openrouter",
            "--model",
            "qwen/qwen3",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["id"].startswith("com.bianbu.")
    assert payload["model"] == {"provider": "openrouter", "name": "qwen/qwen3"}
    assert "hooks" not in payload, "Hooks belong to Plugins, not to the Agent contract"
    stored = AgentManifest.from_path(home.agents_dir / payload["id"] / "agent.yaml")
    assert stored.spec.model == AgentModel("openrouter", "qwen/qwen3")
    assert stored.spec.hooks == ()


def test_runtime_cli_agent_create_requires_provider_and_model_pair() -> None:
    """The former flat optional --model form cannot serialize a null repair Agent."""

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--config",
                "/tmp/config.yaml",
                "agent",
                "create",
                "Writer",
                "--model",
                "qwen/qwen3",
            ]
        )
