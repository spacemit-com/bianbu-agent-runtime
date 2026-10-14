from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "ensure-bibit-intent.sh"


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "bibit-intent"
    (project / "src" / "bibit_intent").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='bibit-intent'\n")
    return project


def _run_script(tmp_path: Path, project: Path) -> subprocess.CompletedProcess[str]:
    binary_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    environment = {
        **os.environ,
        "PATH": f"{binary_dir}:/usr/bin:/bin",
        "BIBIT_INTENT_ROOT": str(project),
        "BIBIT_INTENT_DATA_ROOT": str(tmp_path / "data"),
        "BIBIT_INTENT_STATE_DIR": str(state_dir),
        "BIBIT_INTENT_START_TIMEOUT": "2",
        "FAKE_HEALTH_MARKER": str(tmp_path / "healthy"),
        "FAKE_UV_LOG": str(tmp_path / "uv.json"),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=PROJECT_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_ensure_bibit_intent_reuses_healthy_service(tmp_path: Path) -> None:
    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _write_executable(
        binary_dir / "curl",
        "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"ok\",\"registered_tools\":117}'\n",
    )
    _write_executable(
        binary_dir / "uv",
        f"#!{sys.executable}\nraise SystemExit('uv must not run')\n",
    )

    result = _run_script(tmp_path, project)

    assert result.returncode == 0, result.stderr
    assert "Bibit Intent 已就绪" in result.stdout
    assert not (tmp_path / "uv.json").exists()


def test_ensure_bibit_intent_rejects_an_unrelated_healthy_endpoint(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _write_executable(
        binary_dir / "curl",
        "#!/bin/sh\nprintf '%s\\n' '{\"ok\":true,\"wrapper_connected\":false}'\n",
    )
    _write_executable(
        binary_dir / "uv",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['FAKE_UV_LOG']).write_text(json.dumps(sys.argv[1:]))\n",
    )

    result = _run_script(tmp_path, project)

    assert result.returncode != 0
    assert (tmp_path / "uv.json").exists()


def test_ensure_bibit_intent_starts_service_and_waits_for_health(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _write_executable(
        binary_dir / "curl",
        "#!/bin/sh\n"
        "test -f \"$FAKE_HEALTH_MARKER\" || exit 1\n"
        "printf '%s\\n' '{\"status\":\"ok\",\"registered_tools\":117}'\n",
    )
    _write_executable(
        binary_dir / "uv",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['FAKE_UV_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
        "pathlib.Path(os.environ['FAKE_HEALTH_MARKER']).touch()\n",
    )

    result = _run_script(tmp_path, project)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "uv.json").read_text()) == [
        "run",
        "--project",
        str(project),
        "--no-dev",
        "bibit-intent",
        "serve",
        "--data-root",
        str(tmp_path / "data"),
        "--host",
        "127.0.0.1",
        "--port",
        "8766",
    ]
    assert "Bibit Intent 已启动" in result.stdout


def test_ensure_bibit_intent_prefers_deployed_venv_binary(tmp_path: Path) -> None:
    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(
        binary_dir / "curl",
        "#!/bin/sh\n"
        "test -f \"$FAKE_HEALTH_MARKER\" || exit 1\n"
        "printf '%s\\n' '{\"status\":\"ok\",\"registered_tools\":117}'\n",
    )
    _write_executable(
        binary_dir / "uv",
        f"#!{sys.executable}\nraise SystemExit('uv must not run when venv binary exists')\n",
    )
    _write_executable(
        venv_bin / "bibit-intent",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['FAKE_UV_LOG']).write_text(json.dumps(sys.argv))\n"
        "pathlib.Path(os.environ['FAKE_HEALTH_MARKER']).touch()\n",
    )

    result = _run_script(tmp_path, project)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "uv.json").read_text()) == [
        str(venv_bin / "bibit-intent"),
        "serve",
        "--data-root",
        str(tmp_path / "data"),
        "--host",
        "127.0.0.1",
        "--port",
        "8766",
    ]
    assert "Bibit Intent 已启动" in result.stdout


def test_ensure_bibit_intent_prefers_deployed_main_py_when_console_script_missing(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    venv_bin = project / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (project / "main.py").write_text("# entrypoint\n", encoding="utf-8")
    _write_executable(
        binary_dir / "curl",
        "#!/bin/sh\n"
        "test -f \"$FAKE_HEALTH_MARKER\" || exit 1\n"
        "printf '%s\\n' '{\"status\":\"ok\",\"registered_tools\":117}'\n",
    )
    _write_executable(
        binary_dir / "uv",
        f"#!{sys.executable}\nraise SystemExit('uv must not run when deployed main.py exists')\n",
    )
    _write_executable(
        venv_bin / "python",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['FAKE_UV_LOG']).write_text(json.dumps(sys.argv))\n"
        "pathlib.Path(os.environ['FAKE_HEALTH_MARKER']).touch()\n",
    )

    result = _run_script(tmp_path, project)

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / "uv.json").read_text()) == [
        str(venv_bin / "python"),
        str(project / "main.py"),
        "serve",
        "--data-root",
        str(tmp_path / "data"),
        "--host",
        "127.0.0.1",
        "--port",
        "8766",
    ]
    assert "Bibit Intent 已启动" in result.stdout


def test_ensure_bibit_intent_rejects_missing_external_source(tmp_path: Path) -> None:
    project = tmp_path / "missing"
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    _write_executable(
        binary_dir / "curl",
        "#!/bin/sh\nprintf '%s\\n' '{\"status\":\"ok\",\"registered_tools\":117}'\n",
    )
    _write_executable(binary_dir / "uv", "#!/bin/sh\nexit 0\n")

    result = _run_script(tmp_path, project)

    assert result.returncode != 0
    assert "未找到 bibit-intent 源码" in result.stderr
