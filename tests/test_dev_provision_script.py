from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


PROJECT_ROOT = Path(__file__).parents[1]
PROVISION = PROJECT_ROOT / "scripts" / "dev-provision.sh"


def _provision_packages() -> list[str]:
    match = re.search(
        r"local packages=\(\n(?P<body>.*?)\n\s*\)",
        PROVISION.read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert match is not None, "dev-provision.sh 应保留 install_all_packages 的 packages 数组"
    return [
        line.strip()
        for line in match.group("body").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _debian_runtime_depends() -> set[str]:
    collected: list[str] = []
    collecting = False
    for line in (
        (PROJECT_ROOT / "debian" / "control").read_text(encoding="utf-8").splitlines()
    ):
        if line.startswith("Depends:"):
            collecting = True
            collected.append(line[len("Depends:") :])
        elif collecting and line.startswith((" ", "\t")):
            collected.append(line)
        elif collecting:
            break
    names: set[str] = set()
    for entry in ",".join(collected).split(","):
        entry = entry.strip()
        if not entry or entry.startswith("${"):
            continue
        names.add(entry.split()[0])
    return names


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _bind(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    return listener


def test_dev_provision_installs_packages_groups_and_services(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    provision = scripts / "dev-provision.sh"
    provision.write_bytes(PROVISION.read_bytes())
    provision.chmod(0o755)
    host_log = tmp_path / "host.jsonl"
    _executable(
        scripts / "ensure-searxng.sh",
        "#!/usr/bin/env bash\nprintf '%s\\n' '[\"ensure-searxng\"]' >> \"$HOST_LOG\"\n",
    )

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    host_dispatch = (
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with pathlib.Path(os.environ['HOST_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps([name, *args]) + '\\n')\n"
        "if name == 'sudo': os.execvp(args[0], args)\n"
        "if name == 'id':\n"
        "    print('tester' if '-un' in args else ('1000' if '-u' in args else 'tester sudo'))\n"
        "if name == 'systemctl' and 'is-active' in args:\n"
        "    raise SystemExit(1)\n"
        "if name == 'curl': print('{\"status\":\"ok\",\"registered_tools\":131}')\n"
    )
    for name in (
        "apt-get",
        "curl",
        "id",
        "install",
        "sudo",
        "systemctl",
        "usermod",
        "uv",
    ):
        _executable(binary_dir / name, host_dispatch)

    security_socket = _bind(tmp_path / "security.sock")
    memory_socket = _bind(tmp_path / "memory.sock")
    registry = tmp_path / "memory-registry"
    registry.mkdir()
    security_client = tmp_path / "security-client"
    security_client.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "HOST_LOG": str(host_log),
        "BIANBU_SEC_UDS": str(tmp_path / "security.sock"),
        "BIANBU_AGENT_MEMORY_SOCKET": str(tmp_path / "memory.sock"),
        "BIANBU_AGENT_MEMORY_REGISTRY_DIR": str(registry),
        "BIANBU_SECURITY_CLIENT_DIR": str(security_client),
    }
    try:
        result = subprocess.run(
            ["bash", str(provision)],
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        security_socket.close()
        memory_socket.close()

    assert result.returncode == 0, result.stderr
    host_calls = [json.loads(line) for line in host_log.read_text().splitlines()]
    # 依赖装齐并升级到最新候选版本。
    assert ["sudo", "apt-get", "update"] in host_calls
    assert ["sudo", "apt-get", "install", "-y", *_provision_packages()] in host_calls
    # 当前用户缺组时一次补齐；服务只 start，不代为 enable。
    assert (
        ["sudo", "usermod", "-a", "-G", "bianbu-agent,bianbu-security", "tester"]
        in host_calls
    )
    for unit in ("bianbu-securityd.service", "bianbu-agent-memoryd.service"):
        assert ["systemctl", "is-active", "--quiet", unit] in host_calls
        assert ["sudo", "systemctl", "start", unit] in host_calls
    assert not any("enable" in call for call in host_calls)
    # bibit-intent 的单元归它自己的包：这里只探 healthz。
    assert ["curl", "--fail", "--silent", "--max-time", "2", "http://127.0.0.1:8765/healthz"] in host_calls
    assert ["ensure-searxng"] in host_calls
    assert not any(
        call[0] == "systemctl" and any("bibit" in argument for argument in call)
        for call in host_calls
    )


def test_dev_provision_covers_every_debian_runtime_dependency() -> None:
    """dev-provision.sh 的 APT 列表必须覆盖 debian/control 里 bianbu-agent-runtime 的全部 Depends。"""
    depends = _debian_runtime_depends()
    packages = set(_provision_packages())
    assert depends <= packages, (
        f"dev-provision.sh 的 packages 缺少 Runtime 的打包依赖：{sorted(depends - packages)}"
    )
