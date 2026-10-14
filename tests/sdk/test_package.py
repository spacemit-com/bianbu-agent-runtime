from __future__ import annotations

import os
import subprocess
import zipfile
from configparser import ConfigParser
from io import StringIO
from pathlib import Path


def test_wheel_contains_only_bianbu_agent_runtime(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.pop("UV_FIND_LINKS", None)
    environment.pop("UV_INDEX_URL", None)
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points_text = archive.read(entry_points_name).decode("utf-8")

    assert any(name.startswith("bianbu_agent/") for name in names)
    assert any(name.startswith("bianbu_agent/plugins/") for name in names)
    assert not any(name.startswith("bianbu_agent/_legacy/") for name in names)
    assert sum(name.endswith("/console/web/chat.html") for name in names) == 1
    assert "bianbu_agent/console/router.py" not in names
    assert "bianbu_agent/console/facade.py" not in names
    parser = ConfigParser()
    parser.read_file(StringIO(entry_points_text))
    assert (
        parser["console_scripts"]["bianbu-agent"]
        == "bianbu_agent.cli.main:main"
    )
    assert "bianbu_agent.plugins" not in parser
