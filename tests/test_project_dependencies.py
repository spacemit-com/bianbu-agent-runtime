from __future__ import annotations

import subprocess
import tomllib
from email.parser import Parser
from pathlib import Path

from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).parents[1]


def test_distribution_builds_one_bianbu_agent_package() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    assert project["project"]["name"] == "bianbu-agent"
    assert project["project"]["requires-python"] == ">=3.14,<3.15"
    packages = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert packages == ["bianbu_agent"]


def test_removed_runtime_roots_and_dependencies_are_absent() -> None:
    removed = (
        "agent", "cron", "hermes_cli", "plugins", "providers",
        "hermes_constants.py", "hermes_state.py", "hermes_time.py", "utils.py",
    )
    assert not any((PROJECT_ROOT / name).exists() for name in removed)
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirements = {Requirement(value).name for value in project["project"]["dependencies"]}
    assert "hermes-agent" not in requirements
    assert "python-dotenv" not in requirements


def test_riscv_python_314_uses_the_available_markupsafe_wheel() -> None:
    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirement = next(
        Requirement(value)
        for value in project["project"]["dependencies"]
        if Requirement(value).name == "markupsafe"
    )
    assert str(requirement.specifier) == "==2.1.3"
    assert requirement.marker is not None


def test_debian_runtime_uses_python_314() -> None:
    assert "python3.14" in (PROJECT_ROOT / "debian/control").read_text()
    assert "RUNTIME_PYTHON := /usr/bin/python3.14" in (
        PROJECT_ROOT / "debian/rules"
    ).read_text()
    for relative_path in (
        "debian/provision-user.py",
        "debian/render-starter-agent.py",
    ):
        first_line = (PROJECT_ROOT / relative_path).read_text().splitlines()[0]
        assert first_line == "#!/usr/bin/python3.14"


def test_debian_runtime_requires_searxng(tmp_path: Path) -> None:
    generated = subprocess.run(
        [
            "dpkg-gencontrol",
            "-pbianbu-agent-runtime",
            f"-f{tmp_path / 'files'}",
            "-O",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    dependencies = {
        dependency.strip().split()[0]
        for dependency in Parser().parsestr(generated)["Depends"].split(",")
    }

    assert "searxng" in dependencies
