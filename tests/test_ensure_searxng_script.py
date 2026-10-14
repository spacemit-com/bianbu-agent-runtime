import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
SOURCE_SCRIPT = PROJECT_ROOT / "scripts" / "ensure-searxng.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fixture(
    tmp_path: Path,
    *,
    os_release: str,
    architecture: str = "riscv64",
    package_state: str = "missing",
    healthy: bool = False,
    systemctl_heals: bool = True,
) -> tuple[Path, dict[str, str], Path]:
    assert SOURCE_SCRIPT.is_file(), "scripts/ensure-searxng.sh 尚未实现"

    script = tmp_path / "project" / "scripts" / SOURCE_SCRIPT.name
    script.parent.mkdir(parents=True)
    script.write_text(SOURCE_SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")

    os_release_file = tmp_path / "os-release"
    os_release_file.write_text(os_release, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    command_log = tmp_path / "commands.log"
    health_marker = tmp_path / "healthy"
    if healthy:
        health_marker.touch()

    _write_executable(
        fake_bin / "dpkg",
        '[[ "$1" == "--print-architecture" ]]\n'
        'printf "%s\\n" "$FAKE_ARCH"\n',
    )
    _write_executable(
        fake_bin / "curl",
        'printf "curl %s\\n" "$*" >> "$FAKE_COMMAND_LOG"\n'
        '[[ -e "$FAKE_HEALTH_MARKER" ]]\n',
    )
    _write_executable(
        fake_bin / "dpkg-query",
        'printf "dpkg-query %s\\n" "$*" >> "$FAKE_COMMAND_LOG"\n'
        'case "$FAKE_PACKAGE_STATE" in\n'
        '  installed) printf "install ok installed\\n" ;;\n'
        '  config-files) printf "deinstall ok config-files\\n" ;;\n'
        '  *) exit 1 ;;\n'
        "esac\n",
    )
    _write_executable(
        fake_bin / "sudo",
        'printf "sudo %s\\n" "$*" >> "$FAKE_COMMAND_LOG"\n'
        'if [[ "$1" == "apt" ]] || '
        '[[ "$1" == "systemctl" && "$FAKE_SYSTEMCTL_HEALS" == "1" ]]; then\n'
        '  touch "$FAKE_HEALTH_MARKER"\n'
        "fi\n",
    )
    _write_executable(fake_bin / "sleep", ":\n")

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "BIANBU_AGENT_OS_RELEASE_FILE": str(os_release_file),
        "FAKE_ARCH": architecture,
        "FAKE_PACKAGE_STATE": package_state,
        "FAKE_SYSTEMCTL_HEALS": "1" if systemctl_heals else "0",
        "FAKE_HEALTH_MARKER": str(health_marker),
        "FAKE_COMMAND_LOG": str(command_log),
    }
    return script, env, command_log


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)], env=env, text=True, capture_output=True
    )


def test_non_bianbu_system_is_unchanged(tmp_path: Path) -> None:
    script, env, command_log = _fixture(
        tmp_path, os_release='ID=ubuntu\nNAME="Ubuntu"\n'
    )
    result = _run(script, env)
    assert result.returncode == 0
    assert not command_log.exists()


def test_bianbu_requires_riscv64(tmp_path: Path) -> None:
    script, env, command_log = _fixture(
        tmp_path,
        os_release='ID=bianbu\nNAME="Bianbu"\n',
        architecture="amd64",
    )
    result = _run(script, env)
    assert result.returncode == 1
    assert "仅支持 riscv64" in result.stderr
    assert not command_log.exists()


def test_healthy_service_skips_privileged_commands(tmp_path: Path) -> None:
    script, env, command_log = _fixture(
        tmp_path, os_release='ID=bianbu\nNAME="Bianbu"\n', healthy=True
    )
    result = _run(script, env)
    assert result.returncode == 0
    assert "SearXNG 已就绪" in result.stdout
    assert "sudo " not in command_log.read_text(encoding="utf-8")


def test_missing_package_is_installed_from_bianbu_archive(tmp_path: Path) -> None:
    script, env, command_log = _fixture(
        tmp_path, os_release='NAME="BianBu Linux"\nID=debian\n'
    )
    result = _run(script, env)
    log = command_log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "sudo apt install -y searxng" in log
    assert "packages/searxng" not in log


def test_installed_package_restarts_service_without_reinstalling(
    tmp_path: Path,
) -> None:
    script, env, command_log = _fixture(
        tmp_path,
        os_release='ID=bianbu\nNAME="Bianbu"\n',
        package_state="installed",
    )
    result = _run(script, env)
    log = command_log.read_text(encoding="utf-8")
    assert result.returncode == 0
    assert "sudo systemctl enable --now searxng" in log
    assert "sudo apt" not in log


def test_removed_package_with_remaining_config_is_reinstalled(
    tmp_path: Path,
) -> None:
    script, env, command_log = _fixture(
        tmp_path,
        os_release='ID=bianbu\nNAME="Bianbu"\n',
        package_state="config-files",
    )
    result = _run(script, env)
    assert result.returncode == 0
    assert "sudo apt install -y searxng" in command_log.read_text(encoding="utf-8")


def test_unhealthy_service_blocks_startup(tmp_path: Path) -> None:
    script, env, command_log = _fixture(
        tmp_path,
        os_release='ID=bianbu\nNAME="Bianbu"\n',
        package_state="installed",
        systemctl_heals=False,
    )
    result = _run(script, env)
    assert result.returncode == 1
    assert "SearXNG 启动后仍不可用" in result.stderr
    assert command_log.read_text(encoding="utf-8").count("curl ") == 11
