from __future__ import annotations

import errno
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import time

import pytest
import yaml

import importlib

sdk_cli = importlib.import_module("bianbu_agent.cli.commands.models")
from bianbu_agent.home import AgentHome


PROJECT_ROOT = Path(__file__).parents[2]
_FORMER_MODEL_ENV = (
    "CHAT_MODEL_ID",
    "CHAT_API_KEY",
    "CHAT_BASE_URL",
    "VISION_MODEL_ID",
    "VISION_API_KEY",
    "VISION_BASE_URL",
)


def _command(home: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "bianbu_agent.cli.main",
        "--config",
        str(home / "config.yaml"),
        "model",
        "bootstrap-chat",
    ]


def _ensure_config(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "data_dir": ".",
                "service": {"host": "127.0.0.1", "port": 18765},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("VIRTUAL_ENV", None)
    for name in _FORMER_MODEL_ENV:
        environment.pop(name, None)
    return environment


def _read_until(master: int, marker: str, *, timeout: float = 15.0) -> bytes:
    expected = marker.encode()
    received = bytearray()
    deadline = time.monotonic() + timeout
    while expected not in received:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                f"timed out waiting for {marker!r}: {received.decode(errors='replace')}"
            )
        ready, _, _ = select.select([master], [], [], remaining)
        if not ready:
            continue
        try:
            received.extend(os.read(master, 4096))
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
    if expected not in received:
        raise AssertionError(
            f"process ended before {marker!r}: {received.decode(errors='replace')}"
        )
    return bytes(received)


def _finish(master: int, process: subprocess.Popen[bytes]) -> bytes:
    received = bytearray()
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master], [], [], 0.1)
        if ready:
            try:
                received.extend(os.read(master, 4096))
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                break
        if process.poll() is not None and not ready:
            break
    process.wait(timeout=5)
    return bytes(received)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def _spawn(home: Path) -> tuple[subprocess.Popen[bytes], int]:
    _ensure_config(home)
    master, slave = pty.openpty()
    process = subprocess.Popen(
        _command(home),
        cwd=PROJECT_ROOT,
        env=_clean_environment(),
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    return process, master


def _spawn_controlling_pty(home: Path) -> tuple[int, int]:
    _ensure_config(home)
    pid, master = pty.fork()
    if pid == 0:
        os.chdir(PROJECT_ROOT)
        os.execvpe(_command(home)[0], _command(home), _clean_environment())
        raise AssertionError("exec unexpectedly returned")
    return pid, master


@pytest.mark.asyncio
async def test_model_bootstrap_persists_only_provider_without_secret_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    secret = "create-only-cli-secret"
    captured: dict[str, object] = {}

    async def create_only(store, provider):
        captured.update(
            provider_id=provider.id,
            base_url=provider.base_url,
            api_key=provider.api_key,
        )
        return None

    async def reject_upsert(store, **values):
        raise AssertionError("bootstrap used replacement-capable upsert")

    monkeypatch.setattr(
        sdk_cli.YAMLModelStore,
        "create_provider",
        create_only,
    )
    monkeypatch.setattr(
        sdk_cli.YAMLModelStore,
        "upsert_provider",
        reject_upsert,
    )

    result = await sdk_cli.persist_chat_bootstrap(
        home,
        ("chat-model", "https://chat.cli.test/v1", secret),
    )

    output = capsys.readouterr()
    assert result == 0
    assert captured == {
        "provider_id": "chat-model",
        "base_url": "https://chat.cli.test/v1",
        "api_key": secret,
    }
    assert secret not in output.out
    assert secret not in output.err


@pytest.mark.asyncio
async def test_model_bootstrap_maps_authoritative_create_collision_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    secret = "collision-cli-secret"

    async def collide(store, provider):
        raise ValueError("record already exists: provider-chat-model")

    monkeypatch.setattr(
        sdk_cli.YAMLModelStore,
        "create_provider",
        collide,
    )

    result = await sdk_cli.persist_chat_bootstrap(
        home,
        ("chat-model", "https://chat.cli.test/v1", secret),
    )

    output = capsys.readouterr()
    assert result == 1
    assert "already configured" in output.err
    assert secret not in output.out
    assert secret not in output.err


@pytest.mark.asyncio
async def test_model_bootstrap_maps_storage_failure_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    secret = "rollback-cli-secret"

    async def fail_storage(store, provider):
        raise OSError("synthetic provider write failure")

    monkeypatch.setattr(
        sdk_cli.YAMLModelStore,
        "create_provider",
        fail_storage,
    )

    result = await sdk_cli.persist_chat_bootstrap(
        home,
        ("chat-model", "https://chat.cli.test/v1", secret),
    )

    output = capsys.readouterr()
    assert result == 1
    assert "bootstrap failed" in output.err.lower()
    assert secret not in output.out
    assert secret not in output.err


def _wait_child(pid: int, *, timeout: float = 5.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        waited, status = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    raise TimeoutError("interactive bootstrap did not exit after one Ctrl-C")


def _read_remaining(master: int) -> bytes:
    received = bytearray()
    while True:
        ready, _, _ = select.select([master], [], [], 0.1)
        if not ready:
            break
        try:
            received.extend(os.read(master, 4096))
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            break
    return bytes(received)


def test_interactive_bootstrap_does_not_depend_on_uv_process_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    home = tmp_path / "project" / ".bianbu-agent"
    process, master = _spawn(home)
    try:
        # Importing the CLI and LangGraph can exceed five seconds on K3/RISC-V.
        output = bytearray(_read_until(master, "Provider ID: ", timeout=15.0))
        os.write(master, b"\x04")
        output.extend(_finish(master, process))
    finally:
        os.close(master)
        _kill_process_group(process)

    assert b"Provider ID: " in output


def test_documented_model_bootstrap_runs_interactively_without_exposing_secret(
    tmp_path: Path,
) -> None:
    home = tmp_path / "project" / ".bianbu-agent"
    secret = "pty-bootstrap-secret"
    process, master = _spawn(home)
    output = bytearray()
    try:
        output.extend(_read_until(master, "Provider ID: "))
        os.write(master, b"test-provider\n")
        output.extend(_read_until(master, "OpenAI-compatible Base URL: "))
        os.write(master, b"https://chat.bootstrap.test/v1\n")
        output.extend(_read_until(master, "API Key: "))
        os.write(master, secret.encode() + b"\n")
        output.extend(_finish(master, process))
    finally:
        os.close(master)
        _kill_process_group(process)

    rendered = output.decode(errors="replace")
    assert process.returncode == 0, rendered
    assert secret not in rendered
    assert secret not in " ".join(_command(home))
    assert secret not in _clean_environment().values()
    records = tuple((home / "models").rglob("*.yaml"))
    assert {path.relative_to(home / "models").as_posix() for path in records} == {
        "test-provider/provider.yaml"
    }
    payload = yaml.safe_load(records[0].read_text(encoding="utf-8"))
    assert payload["provider"] == "test-provider"
    assert payload["model"] == "credential"
    assert payload["base_url"] == "https://chat.bootstrap.test/v1"
    assert {path.stat().st_mode & 0o777 for path in records} == {0o600}


def test_model_bootstrap_eof_leaves_no_partial_model_records(tmp_path: Path) -> None:
    home = tmp_path / "project" / ".bianbu-agent"
    process, master = _spawn(home)
    try:
        output = bytearray(_read_until(master, "Provider ID: "))
        os.write(master, b"\x04")
        output.extend(_finish(master, process))
    finally:
        os.close(master)
        _kill_process_group(process)

    assert process.returncode != 0
    assert not tuple((home / "models").rglob("*.yaml"))
    assert "Traceback" not in output.decode(errors="replace")


@pytest.mark.filterwarnings(
    "ignore:This process .* is multi-threaded, use of forkpty.*:DeprecationWarning"
)
def test_model_bootstrap_single_terminal_interrupt_exits_without_partial_records(
    tmp_path: Path,
) -> None:
    home = tmp_path / "project" / ".bianbu-agent"
    pid, master = _spawn_controlling_pty(home)
    output = bytearray()
    try:
        output.extend(_read_until(master, "Provider ID: "))
        os.write(master, b"\x03")
        try:
            exit_code = _wait_child(pid)
        except TimeoutError:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise
        output.extend(_read_remaining(master))
    finally:
        os.close(master)

    rendered = output.decode(errors="replace")
    assert exit_code != 0
    assert "Traceback" not in rendered
    assert not tuple((home / "models").rglob("*.yaml"))


def test_model_bootstrap_repeated_run_fails_closed_without_overwrite(
    tmp_path: Path,
) -> None:
    home = tmp_path / "project" / ".bianbu-agent"
    process, master = _spawn(home)
    try:
        _read_until(master, "Provider ID: ")
        os.write(master, b"first-provider\n")
        _read_until(master, "OpenAI-compatible Base URL: ")
        os.write(master, b"https://first.bootstrap.test/v1\n")
        _read_until(master, "API Key: ")
        os.write(master, b"first-secret\n")
        _finish(master, process)
    finally:
        os.close(master)
        _kill_process_group(process)
    assert process.returncode == 0
    before = {
        path.relative_to(home / "models"): path.read_bytes()
        for path in (home / "models").rglob("*.yaml")
    }

    repeated = subprocess.run(
        _command(home),
        cwd=PROJECT_ROOT,
        env=_clean_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert repeated.returncode != 0
    assert b"already configured" in repeated.stderr
    assert b"Traceback" not in repeated.stderr
    assert {
        path.relative_to(home / "models"): path.read_bytes()
        for path in (home / "models").rglob("*.yaml")
    } == before


def test_readme_uses_the_real_interactive_bootstrap_command() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "uv run bianbu-agent --config config/development.yaml model bootstrap-chat" in readme
    assert "uv run python - <<'PY'" not in readme
