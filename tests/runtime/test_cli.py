from __future__ import annotations

import json
import importlib
from pathlib import Path
import socket
from types import SimpleNamespace

import pytest
import yaml

from bianbu_agent.config import load_runtime_config

cli = importlib.import_module("bianbu_agent.cli.main")
server_commands = importlib.import_module("bianbu_agent.cli.commands.server")


def _config(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
) -> Path:
    path = tmp_path / "deployment" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "data_dir": "./data",
                "service": {"host": host, "port": 18765},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_parser_allows_configless_resource_commands_and_rejects_home(tmp_path: Path) -> None:
    parser = cli.build_parser()

    args = parser.parse_args(["agent", "list"])
    assert args.config is None
    with pytest.raises(SystemExit):
        parser.parse_args(["--home", str(tmp_path), "agent", "list"])

    args = parser.parse_args(["--config", str(tmp_path / "runtime.yaml")])
    assert args.resource is None
    assert args.config == tmp_path / "runtime.yaml"


def test_main_treats_keyboard_interrupt_as_a_quiet_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = SimpleNamespace(resource="serve", action=None)
    parser = SimpleNamespace(parse_args=lambda: args)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)

    def interrupt(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupt)

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 130


def test_lan_ipv4_addresses_exclude_unusable_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    addresses = {
        "enp3s0": [SimpleNamespace(family=socket.AF_INET, address="10.0.91.153")],
        "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
        "docker0": [SimpleNamespace(family=socket.AF_INET, address="172.17.0.1")],
        "tun0": [SimpleNamespace(family=socket.AF_INET, address="10.8.0.2")],
        "down0": [SimpleNamespace(family=socket.AF_INET, address="192.168.2.8")],
    }
    stats = {
        "enp3s0": SimpleNamespace(isup=True, flags="up,broadcast,running"),
        "lo": SimpleNamespace(isup=True, flags="up,loopback,running"),
        "docker0": SimpleNamespace(isup=True, flags="up,broadcast,running"),
        "tun0": SimpleNamespace(isup=True, flags="up,pointopoint,running"),
        "down0": SimpleNamespace(isup=False, flags="broadcast"),
    }
    monkeypatch.setattr(server_commands.psutil, "net_if_addrs", lambda: addresses)
    monkeypatch.setattr(server_commands.psutil, "net_if_stats", lambda: stats)

    assert server_commands._lan_ipv4_addresses() == ("10.0.91.153",)


def test_browser_access_messages_show_local_and_cross_device_urls() -> None:
    assert server_commands._browser_access_messages(
        "0.0.0.0", 9899, lan_addresses=("10.0.91.153",)
    ) == (
        "本机请访问：http://127.0.0.1:9899",
        "其他设备请访问：http://10.0.91.153:9899",
        "请勿在浏览器中使用：http://0.0.0.0:9899",
    )


def test_browser_access_messages_use_placeholder_without_lan_address() -> None:
    messages = server_commands._browser_access_messages(
        "0.0.0.0", 9899, lan_addresses=()
    )

    assert messages[1] == "其他设备请访问：http://<本机局域网IP>:9899"


def test_browser_access_messages_skip_non_wildcard_host() -> None:
    assert server_commands._browser_access_messages(
        "127.0.0.1", 9899, lan_addresses=("10.0.91.153",)
    ) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", [[], ["serve"]])
async def test_implicit_and_explicit_serve_use_loaded_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: list[str],
) -> None:
    config_path = _config(tmp_path)
    seen = []

    async def fake_serve(config) -> int:
        seen.append(config)
        return 0

    monkeypatch.setattr(server_commands, "serve", fake_serve)

    assert await cli.main_async(["--config", str(config_path), *suffix]) == 0
    assert len(seen) == 1
    assert seen[0].config_path == config_path.resolve()
    assert seen[0].service.port == 18765


@pytest.mark.asyncio
async def test_serve_explicitly_enforces_one_http_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Process-local Run admission locks require a single HTTP worker."""

    config = load_runtime_config(_config(tmp_path, host="0.0.0.0"))
    captured: dict[str, object] = {}

    monkeypatch.setattr(server_commands, "create_runtime_app", lambda _: object())

    def fake_config(*args, **kwargs):
        captured.update(kwargs)
        return object()

    class FakeServer:
        def __init__(self, config) -> None:
            pass

        async def serve(self) -> None:
            pass

    class FakeOwner:
        def __init__(self, config) -> None:
            pass

        def acquire(self) -> None:
            pass

        def release(self) -> None:
            pass

    monkeypatch.setattr(server_commands.uvicorn, "Config", fake_config)
    monkeypatch.setattr(server_commands.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server_commands, "RuntimeProcessOwner", FakeOwner)
    monkeypatch.setattr(
        server_commands,
        "_lan_ipv4_addresses",
        lambda: ("10.0.91.153",),
    )

    assert await server_commands.serve(config) == 0
    assert captured["workers"] == 1
    assert capsys.readouterr().err.splitlines() == [
        "本机请访问：http://127.0.0.1:18765",
        "其他设备请访问：http://10.0.91.153:18765",
        "请勿在浏览器中使用：http://0.0.0.0:18765",
    ]


@pytest.mark.asyncio
async def test_serve_rejects_unmanaged_production(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "production")
    monkeypatch.delenv("BIANBU_RUNTIME_MANAGED", raising=False)
    config = load_runtime_config(_config(tmp_path))
    with pytest.raises(RuntimeError, match="生产模式禁止裸启动"):
        await server_commands.serve(config)


@pytest.mark.asyncio
async def test_agent_commands_allow_removing_any_agent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = _config(tmp_path)
    assert await cli.main_async(
        [
            "--config",
            str(config_path),
            "agent",
            "create",
            "Default",
            "--provider",
            "openai",
            "--model",
            "gpt-test",
        ]
    ) == 0
    agent_id = json.loads(capsys.readouterr().out)["id"]

    assert await cli.main_async(
        ["--config", str(config_path), "agent", "remove", agent_id]
    ) == 0
