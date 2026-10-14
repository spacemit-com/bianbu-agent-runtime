from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from bianbu_agent.home import AgentHome
from bianbu_agent.mcp import MCPPackageManager
from bianbu_agent.mcp import manager as manager_module


def _write_mcp(root: Path, *, command: str = "weather-mcp") -> Path:
    root.mkdir(parents=True)
    (root / "mcp.json").write_text(
        json.dumps({"transport": "stdio", "command": command}),
        encoding="utf-8",
    )
    return root


def test_manager_installs_and_uninstalls_under_first_class_root(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / "state").prepare()
    source = _write_mcp(tmp_path / "mcp.weather")
    manager = MCPPackageManager(home)

    installed = manager.install(source)

    assert installed.id == "mcp.weather"
    assert installed.root == home.mcps_dir / "mcp.weather"
    assert manager.inspect("mcp.weather") == installed
    assert manager.list() == (installed,)
    assert manager.uninstall("mcp.weather") is True
    assert manager.inspect("mcp.weather") is None
    assert len(tuple(home.staging_dir.glob("retired-mcp-mcp.weather-*"))) == 1


def test_uninstall_commits_when_quarantine_cleanup_is_deferred(
    tmp_path: Path,
) -> None:
    """Logical removal retains the version for active generation leases."""

    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    manager.install(_write_mcp(tmp_path / "mcp.weather"))

    assert manager.uninstall("mcp.weather") is True
    assert manager.inspect("mcp.weather") is None
    assert tuple(home.staging_dir.glob("retired-mcp-mcp.weather-*"))


def test_manager_rejects_extra_files_and_symlinked_config(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    extra = _write_mcp(tmp_path / "mcp.extra")
    (extra / "README.md").write_text("unexpected", encoding="utf-8")

    with pytest.raises(ValueError, match="only contain mcp.json"):
        manager.install(extra)

    real = _write_mcp(tmp_path / "real")
    linked = tmp_path / "mcp.linked"
    linked.mkdir()
    (linked / "mcp.json").symlink_to(real / "mcp.json")
    with pytest.raises(ValueError, match="regular file"):
        manager.install(linked)


def test_install_rejects_config_inode_replacement_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing validated mcp.json must not publish attacker-controlled bytes."""

    home = AgentHome(tmp_path / "state").prepare()
    source = _write_mcp(tmp_path / "mcp.weather", command="trusted")
    replacement = tmp_path / "attacker.json"
    replacement.write_text(
        json.dumps({"transport": "stdio", "command": "attacker"}),
        encoding="utf-8",
    )
    original_open = os.open
    swapped = False

    def replace_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "mcp.json" and dir_fd is not None and not swapped:
            swapped = True
            (source / "mcp.json").unlink()
            (source / "mcp.json").symlink_to(replacement)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(manager_module.os, "open", replace_before_open)

    with pytest.raises(ValueError, match="changed|symbolic link|unsafe"):
        MCPPackageManager(home).install(source)
    assert swapped is True
    assert not (home.mcps_dir / "mcp.weather").exists()


def test_install_rejects_source_root_replacement_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snapshot must remain owned by the source directory inode it opened."""

    home = AgentHome(tmp_path / "state").prepare()
    source = _write_mcp(tmp_path / "mcp.weather")
    retained = tmp_path / "retained-weather"
    original_open = os.open
    swapped = False

    def replace_root_before_file_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "mcp.json" and dir_fd is not None and not swapped:
            swapped = True
            source.rename(retained)
            source.symlink_to(retained, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(manager_module.os, "open", replace_root_before_file_open)

    with pytest.raises(ValueError, match="changed|symbolic link|unsafe"):
        MCPPackageManager(home).install(source)
    assert swapped is True
    assert source.is_symlink()
    assert (retained / "mcp.json").is_file()
    assert not (home.mcps_dir / "mcp.weather").exists()


def test_install_rejects_stage_name_replacement_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publishing must stay bound to the stage directory whose bytes were written."""

    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    source = _write_mcp(tmp_path / "mcp.weather", command="trusted")
    retained = home.staging_dir / "retained-trusted-stage"
    original_write = manager._write_config
    replaced = False

    def replace_stage_after_write(directory_fd: int, payload: bytes) -> None:
        nonlocal replaced
        original_write(directory_fd, payload)
        if replaced:
            return
        replaced = True
        stage = Path(os.readlink(f"/proc/self/fd/{directory_fd}"))
        stage.rename(retained)
        _write_mcp(stage, command="attacker")

    monkeypatch.setattr(manager, "_write_config", replace_stage_after_write)

    with pytest.raises(ValueError, match="stage|changed|publish"):
        manager.install(source)

    assert replaced is True
    assert not (home.mcps_dir / "mcp.weather").exists()
    assert json.loads((retained / "mcp.json").read_text())["command"] == "trusted"
    attacker = next(
        child
        for child in home.staging_dir.iterdir()
        if child != retained and (child / "mcp.json").is_file()
    )
    assert json.loads((attacker / "mcp.json").read_text())["command"] == "attacker"


def test_install_quarantines_destination_replacement_immediately_after_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-rename swap must fail closed without deleting the replacement."""

    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    source = _write_mcp(tmp_path / "mcp.weather", command="trusted")
    retained = home.staging_dir / "retained-published-stage"
    original_rename = manager._rename_noreplace
    replaced = False

    def replace_destination_after_publish(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        nonlocal replaced
        original_rename(
            old_directory_fd,
            old_name,
            new_directory_fd,
            new_name,
        )
        if replaced or new_name != "mcp.weather":
            return
        replaced = True
        target = home.mcps_dir / new_name
        target.rename(retained)
        _write_mcp(target, command="attacker")

    monkeypatch.setattr(
        manager,
        "_rename_noreplace",
        replace_destination_after_publish,
    )

    with pytest.raises(ValueError, match="published|changed|destination"):
        manager.install(source)

    assert replaced is True
    assert not (home.mcps_dir / "mcp.weather").exists()
    assert json.loads((retained / "mcp.json").read_text())["command"] == "trusted"
    assert any(
        json.loads((child / "mcp.json").read_text())["command"] == "attacker"
        for child in home.staging_dir.iterdir()
        if child != retained and (child / "mcp.json").is_file()
    )


def test_install_quarantines_stage_replacement_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check-to-rename window cannot publish a replacement stage inode."""

    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    source = _write_mcp(tmp_path / "mcp.weather", command="trusted")
    retained = home.staging_dir / "retained-stage-during-publish"
    original_rename = manager._rename_noreplace
    replaced = False

    def replace_stage_before_rename(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        nonlocal replaced
        if not replaced and old_name.startswith("mcp-mcp.weather-"):
            replaced = True
            stage = home.staging_dir / old_name
            stage.rename(retained)
            _write_mcp(stage, command="attacker")
        original_rename(
            old_directory_fd,
            old_name,
            new_directory_fd,
            new_name,
        )

    monkeypatch.setattr(manager, "_rename_noreplace", replace_stage_before_rename)

    with pytest.raises(ValueError, match="stage|changed|publish"):
        manager.install(source)

    assert replaced is True
    assert not (home.mcps_dir / "mcp.weather").exists()
    assert json.loads((retained / "mcp.json").read_text())["command"] == "trusted"
    assert any(
        json.loads((child / "mcp.json").read_text())["command"] == "attacker"
        for child in home.staging_dir.iterdir()
        if child != retained and (child / "mcp.json").is_file()
    )


def test_install_rejects_mcp_catalog_root_replacement_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication into a renamed-away catalog descriptor must not report success."""

    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    source = _write_mcp(tmp_path / "mcp.weather", command="trusted")
    retained_catalog = tmp_path / "retained-mcp-catalog"
    original_rename = manager._rename_noreplace
    replaced = False

    def replace_catalog_before_publish(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        nonlocal replaced
        if not replaced and new_name == "mcp.weather":
            replaced = True
            home.mcps_dir.rename(retained_catalog)
            home.mcps_dir.mkdir()
        original_rename(
            old_directory_fd,
            old_name,
            new_directory_fd,
            new_name,
        )

    monkeypatch.setattr(manager, "_rename_noreplace", replace_catalog_before_publish)

    with pytest.raises(ValueError, match="catalog|root|changed|publish"):
        manager.install(source)

    assert replaced is True
    assert not (home.mcps_dir / "mcp.weather").exists()
    assert not (retained_catalog / "mcp.weather").exists()


def test_install_eexist_preserves_independent_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed no-replace publication must not quarantine an independently won target."""

    home = AgentHome(tmp_path / "state").prepare()
    source = _write_mcp(tmp_path / "mcp.weather", command="trusted")
    target = home.mcps_dir / "mcp.weather"
    manager = MCPPackageManager(home)
    original_rename = manager._rename_noreplace
    raced = False

    def publish_independent_target_before_rename(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        nonlocal raced
        if not raced and old_name.startswith("mcp-mcp.weather-"):
            raced = True
            _write_mcp(target, command="independent")
        original_rename(
            old_directory_fd,
            old_name,
            new_directory_fd,
            new_name,
        )

    monkeypatch.setattr(
        manager,
        "_rename_noreplace",
        publish_independent_target_before_rename,
    )

    with pytest.raises(FileExistsError, match="already exists"):
        manager.install(source)

    assert raced is True
    assert json.loads((target / "mcp.json").read_text())["command"] == "independent"
    assert json.loads((source / "mcp.json").read_text())["command"] == "trusted"
    assert any(
        json.loads((child / "mcp.json").read_text())["command"] == "trusted"
        for child in home.staging_dir.iterdir()
        if (child / "mcp.json").is_file()
    )


@pytest.mark.parametrize("operation", ("inspect", "uninstall"))
def test_installed_operations_reject_root_replacement_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Inspect and uninstall stay bound to the verified installed directory inode."""

    home = AgentHome(tmp_path / "state").prepare()
    manager = MCPPackageManager(home)
    manager.install(_write_mcp(tmp_path / "mcp.weather"))
    target = home.mcps_dir / "mcp.weather"
    retained = home.staging_dir / "retained-weather"
    original_open = os.open
    swapped = False

    def replace_root_before_file_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == "mcp.json" and dir_fd is not None and not swapped:
            swapped = True
            target.rename(retained)
            target.symlink_to(retained, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(manager_module.os, "open", replace_root_before_file_open)

    with pytest.raises(ValueError, match="changed|symbolic link|unsafe"):
        getattr(manager, operation)("mcp.weather")
    assert swapped is True
    assert target.is_symlink()
    assert (retained / "mcp.json").is_file()
