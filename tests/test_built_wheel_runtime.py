from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

from scripts.smoke_installed_wheel import _bound_listener


PROJECT_ROOT = Path(__file__).parents[1]


def test_installed_wheel_smoke_holds_its_selected_port() -> None:
    """The live smoke keeps ownership of its port until Uvicorn accepts it."""

    listener = _bound_listener()
    competing = __import__("socket").socket()
    try:
        host, port = listener.getsockname()
        with pytest.raises(OSError):
            competing.bind((host, port))
    finally:
        competing.close()
        listener.close()


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("wheel")
    environment = dict(os.environ)
    environment.pop("UV_FIND_LINKS", None)
    environment.pop("UV_INDEX_URL", None)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(output)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    )
    return next(output.glob("bianbu_agent-*.whl"))


def test_built_wheel_contains_one_bianbu_agent_package(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
    python_roots = {
        name.split("/", 1)[0]
        for name in names
        if name.endswith(".py") and ".dist-info/" not in name
    }
    assert python_roots == {"bianbu_agent"}
    assert "bianbu_agent/console/web/chat.html" in names
    assert sum(name.endswith("/console/web/chat.html") for name in names) == 1
    assert "bianbu_agent/console/router.py" not in names
    assert "bianbu_agent/console/facade.py" not in names
    assert not any(
        name.startswith(("agent/", "cron/", "hermes_cli/", "plugins/", "providers/"))
        for name in names
    )


def test_extracted_wheel_imports_without_checkout(built_wheel: Path, tmp_path: Path) -> None:
    target = tmp_path / "installed"
    with zipfile.ZipFile(built_wheel) as archive:
        archive.extractall(target)
    script = textwrap.dedent(
        """
        import bianbu_agent
        import bianbu_agent.server.app
        assert callable(bianbu_agent.create_app)
        """
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(target)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
