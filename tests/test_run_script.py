from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys


PROJECT_ROOT = Path(__file__).parents[1]


def _executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _bind(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen()
    return listener


def _project(tmp_path: Path) -> Path:
    """复制 run.sh 到临时 checkout，并给出最小可用的 config/development.yaml。"""

    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    launcher = project / "run.sh"
    launcher.write_bytes((PROJECT_ROOT / "run.sh").read_bytes())
    launcher.chmod(0o755)
    config = project / "config" / "development.yaml"
    config.parent.mkdir()
    config.write_text(
        "data_dir: ../.bianbu-agent\n"
        "service:\n"
        "  host: 0.0.0.0\n"
        "  port: 9899\n",
        encoding="utf-8",
    )
    # run.sh 用仓库自带的这两个 helper 注册开发自签安全身份。
    debian = project / "debian"
    debian.mkdir()
    for name in ("reset-security-binding.py", "register-security-manifest.py"):
        _logging_stub(debian / name)
    return project


def _logging_stub(path: Path) -> None:
    _executable(
        path,
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "with pathlib.Path(os.environ['HOST_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps([name, *sys.argv[1:]]) + '\\n')\n",
    )


def _uv_stub(path: Path) -> None:
    _executable(
        path,
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with pathlib.Path(os.environ['UV_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if any('sysconfig' in arg for arg in sys.argv):\n"
        "    print(os.environ['RUN_SITE_PACKAGES'])\n"
        "if any(arg.endswith('provision-starter-agent.py') for arg in sys.argv):\n"
        "    print('com.bianbu.0123456789ab')\n"
        "if any(arg.endswith('prepare-development-security-manifest.py') for arg in sys.argv):\n"
        "    runtime_config = pathlib.Path(sys.argv[sys.argv.index('--runtime-config-output') + 1])\n"
        "    runtime_config.parent.mkdir(parents=True, exist_ok=True)\n"
        "    runtime_config.write_text('security:\\n  required: true\\n')\n"
        "    output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "    package = output / 'com.bianbu.runtime.development'\n"
        "    package.mkdir(parents=True, exist_ok=True)\n"
        "    (package / 'manifest.toml').write_text('[agent]\\nid=\"com.bianbu.runtime.development\"\\n[runtime]\\nentrypoint=[\"worker\"]\\n')\n"
        "if os.environ.get('RUN_ENV_LOG') and 'bianbu-agent' in sys.argv:\n"
        "    keys = ('BIANBU_SEC_SESSION', 'BIANBU_AGENT_ID', 'COM_BIANBU_AGENT_ID', 'BIANBU_WORKER_AGENT_ID', 'BIANBU_RUNTIME_SECURITY', 'BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER', 'BIANBU_AGENT_MEMORY_SOCKET', 'BIBIT_INTENT_BASE_URL', 'PYTHONPATH', 'BIANBU_SYSTEM_RESOURCE_ROOT')\n"
        "    pathlib.Path(os.environ['RUN_ENV_LOG']).write_text(json.dumps({key: os.environ.get(key) for key in keys}))\n",
    )


def _runtime_environment(tmp_path: Path, binary_dir: Path) -> tuple[dict[str, str], list[socket.socket]]:
    security_socket = _bind(tmp_path / "security.sock")
    memory_socket = _bind(tmp_path / "memory.sock")
    registry = tmp_path / "memory-registry"
    registry.mkdir()
    security_client = tmp_path / "security-client"
    security_client.mkdir()
    memory_sdk = tmp_path / "system-dist-packages" / "bianbu_agent_sdk"
    memory_sdk.mkdir(parents=True)
    (memory_sdk / "__init__.py").write_text("# test SDK\n", encoding="utf-8")
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:{os.environ['PATH']}",
        "UV_LOG": str(tmp_path / "uv.jsonl"),
        "HOST_LOG": str(tmp_path / "host.jsonl"),
        "RUN_ENV_LOG": str(tmp_path / "environment.json"),
        "RUN_SITE_PACKAGES": str(site_packages),
        "BIANBU_SEC_UDS": str(tmp_path / "security.sock"),
        "BIANBU_AGENT_MEMORY_SOCKET": str(tmp_path / "memory.sock"),
        "BIANBU_AGENT_MEMORY_REGISTRY_DIR": str(registry),
        "BIANBU_SECURITY_CLIENT_DIR": str(security_client),
        "BIANBU_AGENT_MEMORY_SDK_DIR": str(memory_sdk),
        "BIANBU_SECURITY_MANIFEST_STAGE": str(tmp_path / "manifest-stage"),
        "BIANBU_SECURITYD_HOME": "",
        "PYTHONPATH": "",
    }
    return environment, [security_socket, memory_socket]


def test_run_only_starts_the_source_runtime(tmp_path: Path) -> None:
    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _uv_stub(binary_dir / "uv")

    host_log = tmp_path / "host.jsonl"
    host_dispatch = (
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "args = sys.argv[1:]\n"
        "with pathlib.Path(os.environ['HOST_LOG']).open('a') as stream:\n"
        "    stream.write(json.dumps([name, *args]) + '\\n')\n"
        "if name == 'sudo': os.execvp(args[0], args)\n"
        "if name == 'id':\n"
        "    print('tester' if '-un' in args else ('1000' if '-u' in args else 'tester sudo bianbu-agent bianbu-security'))\n"
    )
    for name in ("id", "install", "sudo", "systemctl"):
        _executable(binary_dir / name, host_dispatch)

    environment, listeners = _runtime_environment(tmp_path, binary_dir)
    environment["HOST_LOG"] = str(host_log)
    try:
        result = subprocess.run(
            ["bash", str(project / "run.sh"), "--port", "9899"],
            cwd=project,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        for listener in listeners:
            listener.close()

    assert result.returncode == 0, result.stderr
    host_calls = [json.loads(line) for line in host_log.read_text().splitlines()]
    # run.sh 只启动：装依赖、加组、拉起服务都属于 scripts/dev-provision.sh。
    assert not [call for call in host_calls if "apt-get" in call or "usermod" in call]
    assert not [call for call in host_calls if call[0] == "systemctl" and call[1] in {"enable", "start"}]
    # 让出端口：停掉占用同一端口的已安装实例。
    assert ["sudo", "systemctl", "stop", "bianbu-agent-runtime.service"] in host_calls
    # 注册安全身份：先重置旧绑定，再用刚生成的清单登记开发身份。
    manifest_stage = environment["BIANBU_SECURITY_MANIFEST_STAGE"]
    assert [
        "reset-security-binding.py",
        "com.bianbu.runtime.development",
        "source-run",
    ] in host_calls
    assert [
        "register-security-manifest.py",
        manifest_stage,
        "--expected-agent-id",
        "com.bianbu.runtime.development",
    ] in host_calls

    uv_calls = [json.loads(line) for line in (tmp_path / "uv.jsonl").read_text().splitlines()]
    assert any(
        any(arg.endswith("prepare-development-security-manifest.py") for arg in call)
        for call in uv_calls
    )
    assert any(
        any(arg.endswith("stop-development-runtime.py") for arg in call)
        for call in uv_calls
    )
    agent_call = next(call for call in uv_calls if "bianbu-agent" in call)
    # 启动用的是合并后的配置，且参数原样透传。
    assert str(project / ".bianbu-agent" / "config-all.yaml") in agent_call
    assert agent_call[-2:] == ["--port", "9899"]
    assert all("--with" not in call for call in uv_calls)

    # Worker 起的 Hook / MCP 子进程环境被裁剪过，只能靠 venv 级 import 路径找 Memory SDK。
    memory_python_root = project / ".bianbu-agent" / "runtime" / "python-packages"
    assert (memory_python_root / "bianbu_agent_sdk").is_symlink()
    assert (memory_python_root / "bianbu_agent_sdk").resolve() == Path(
        environment["BIANBU_AGENT_MEMORY_SDK_DIR"]
    ).resolve()
    pth = Path(environment["RUN_SITE_PACKAGES"]) / "bianbu-agent-memory-sdk.pth"
    assert pth.read_text().splitlines() == [
        str(memory_python_root),
        "/home/agent/runtime/python-packages",
    ]

    assert json.loads((tmp_path / "environment.json").read_text()) == {
        "BIANBU_SEC_SESSION": "0",
        "BIANBU_AGENT_ID": "com.bianbu.runtime.development",
        "COM_BIANBU_AGENT_ID": "com.bianbu.0123456789ab",
        "BIANBU_WORKER_AGENT_ID": "com.bianbu.0123456789ab",
        "BIANBU_RUNTIME_SECURITY": "required",
        "BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER": "0",
        "BIANBU_AGENT_MEMORY_SOCKET": environment["BIANBU_AGENT_MEMORY_SOCKET"],
        "BIBIT_INTENT_BASE_URL": "http://127.0.0.1:8765",
        "PYTHONPATH": environment["BIANBU_SECURITY_CLIENT_DIR"],
        "BIANBU_SYSTEM_RESOURCE_ROOT": str(project / "resources"),
    }


def test_run_never_installs_uv_packages_or_services(tmp_path: Path) -> None:
    """环境只由 scripts/dev-provision.sh 准备：run.sh 缺什么都不会自己装。"""

    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    host_log = tmp_path / "host.jsonl"
    _logging_stub(binary_dir / "id")
    _logging_stub(binary_dir / "curl")
    _logging_stub(binary_dir / "apt-get")
    _logging_stub(binary_dir / "systemctl")
    _logging_stub(binary_dir / "usermod")
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
        "HOST_LOG": str(host_log),
        "BIANBU_SEC_UDS": str(tmp_path / "missing-securityd.sock"),
        "BIANBU_AGENT_MEMORY_SOCKET": str(tmp_path / "missing-memoryd.sock"),
        "BIANBU_AGENT_MEMORY_REGISTRY_DIR": str(tmp_path / "missing-registry"),
        "BIANBU_SECURITY_CLIENT_DIR": str(tmp_path / "missing-client"),
        "BIANBU_AGENT_MEMORY_SDK_DIR": str(tmp_path / "missing-sdk"),
    }
    result = subprocess.run(
        ["/bin/bash", str(project / "run.sh")],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    host_calls = (
        [json.loads(line) for line in host_log.read_text().splitlines() if line.strip()]
        if host_log.exists()
        else []
    )
    assert not [
        call
        for call in host_calls
        if call[0] in {"apt-get", "curl", "systemctl", "usermod", "install"}
    ]
