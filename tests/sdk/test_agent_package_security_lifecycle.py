from __future__ import annotations

from pathlib import Path

import pytest

from bianbu_agent.agents import AgentManifest, AgentPackageManager, AgentSpec
from bianbu_agent.agents.projects import build_agent_project
from bianbu_agent.home import AgentHome


def _source(root: Path, *, agent_id: str = "writer", version: str = "1.0.0", name: str = "Writer") -> Path:
    root.mkdir(parents=True)
    manifest = AgentManifest(
        schema_version=1,
        version=version,
        spec=AgentSpec(id=agent_id, name=name),
    )
    (root / "agent.yaml").write_text(manifest.to_yaml(), encoding="utf-8")
    (root / "README.md").write_text(name, encoding="utf-8")
    return root


def test_install_with_security_accepts_unsigned_package_and_marks_pending(
    tmp_path: Path,
) -> None:
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    def unavailable(_package):
        raise RuntimeError("securityd unavailable")

    result = manager.install_with_security(
        _source(tmp_path / "writer" / "writer"),
        register=unavailable,
        required=False,
    )

    assert result.security_status == "security_pending"
    assert result.package.id == "writer"
    assert manager.inspect("writer") is not None


def test_install_with_security_required_failure_rolls_back_new_package(
    tmp_path: Path,
) -> None:
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    def denied(_package):
        raise RuntimeError("registration denied")

    with pytest.raises(RuntimeError, match="registration denied"):
        manager.install_with_security(
            _source(tmp_path / "writer" / "writer"),
            register=denied,
            required=True,
        )

    assert manager.inspect("writer") is None


def test_install_with_security_required_pending_result_rolls_back_package(
    tmp_path: Path,
) -> None:
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    with pytest.raises(RuntimeError, match="registration unavailable"):
        manager.install_with_security(
            _source(tmp_path / "writer" / "writer"),
            register=lambda _package: None,
            required=True,
        )

    assert manager.inspect("writer") is None


def test_install_with_security_required_upgrade_failure_restores_previous_package(
    tmp_path: Path,
) -> None:
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())
    manager.install(_source(tmp_path / "v1" / "writer", version="1.0.0", name="Old"))

    def denied(_package):
        raise RuntimeError("registration denied")

    with pytest.raises(RuntimeError, match="registration denied"):
        manager.install_with_security(
            _source(tmp_path / "v2" / "writer", version="2.0.0", name="New"),
            upgrade=True,
            register=denied,
            required=True,
        )

    restored = manager.inspect("writer")
    assert restored is not None
    assert restored.version == "1.0.0"
    assert restored.manifest.spec.name == "Old"


def test_install_with_security_duplicate_id_fails_before_registration(
    tmp_path: Path,
) -> None:
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())
    manager.install(_source(tmp_path / "first" / "writer"))
    calls: list[str] = []

    def register(package):
        calls.append(package.id)

    with pytest.raises(FileExistsError, match="already installed"):
        manager.install_with_security(
            _source(tmp_path / "second" / "writer"), register=register, required=False
        )

    assert calls == []


def test_install_with_security_accepts_unsigned_agent_archive(
    tmp_path: Path,
) -> None:
    project = _source(tmp_path / "project" / "writer")
    artifact = build_agent_project(project, output=tmp_path / "writer.agent")
    manager = AgentPackageManager(AgentHome(tmp_path / "home").prepare())

    result = manager.install_with_security(
        artifact.path,
        register=lambda package: {"agent_id": package.id, "status": "active"},
        required=True,
    )

    assert result.security_status == "registered"
    assert result.package.content_digest == artifact.content_digest
