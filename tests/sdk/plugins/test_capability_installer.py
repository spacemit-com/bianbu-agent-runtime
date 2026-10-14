from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from bianbu_agent import CapabilityInstaller, AgentHome
from bianbu_agent.plugins.ownership import (
    InstalledCapabilityRecord,
    OWNERSHIP_FILENAME,
)
from bianbu_agent.skills.loader import DirectorySkillLoader


def _write_skill(root: Path, *, name: str = "research") -> None:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Research primary sources\n"
        "version: 1.0.0\n---\nUse primary sources.\n",
        encoding="utf-8",
    )
    (root / "references").mkdir()
    (root / "references" / "guide.md").write_text("guide\n", encoding="utf-8")


def test_installs_standalone_packages_with_deterministic_owned_records(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    skill_source = tmp_path / "source-skill"
    _write_skill(skill_source)
    installer = CapabilityInstaller(home)

    skill = installer.install_skill(skill_source)

    assert (skill.kind, skill.capability_id, skill.version, skill.origin) == (
        "skill", "research", "1.0.0", "standalone"
    )
    assert skill == CapabilityInstaller(
        AgentHome(tmp_path / "second-home")
    ).install_skill(skill_source)
    assert skill == installer.inspect("skill", "research")
    assert installer.list() == (skill,)
    assert installer.verify("skill", "research") is True
    assert (home.skills_dir / "research" / "SKILL.md").is_file()
    assert not tuple(home.staging_dir.iterdir())


def test_rejects_duplicate_targets_and_source_symlinks(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "research"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    installer.install_skill(source)

    with pytest.raises(FileExistsError, match="already installed"):
        installer.install_skill(source)

    linked = tmp_path / "linked"
    _write_skill(linked)
    (linked / "references" / "outside").symlink_to(tmp_path / "secret")
    with pytest.raises(ValueError, match="symbolic links"):
        installer.install_skill(linked)


def test_rejects_a_source_root_symlink_before_resolving_it(tmp_path: Path) -> None:
    source = tmp_path / "research"
    _write_skill(source)
    linked_source = tmp_path / "linked-source"
    linked_source.symlink_to(source, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic links"):
        CapabilityInstaller(AgentHome(tmp_path / ".bianbu-agent")).install_skill(
            linked_source
        )


def test_rejects_nested_reserved_ownership_metadata_in_sources_and_loaders(
    tmp_path: Path,
) -> None:
    source = tmp_path / "research"
    _write_skill(source)
    nested = source / "nested"
    nested.mkdir()
    nested_metadata = nested / OWNERSHIP_FILENAME
    nested_metadata.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="reserved ownership metadata"):
        CapabilityInstaller(AgentHome(tmp_path / ".bianbu-agent")).install_skill(
            source
        )


@pytest.mark.parametrize("capability_id", ("../outside", "nested/name", ".", ""))
def test_rejects_traversal_and_non_component_identifiers(
    tmp_path: Path, capability_id: str
) -> None:
    installer = CapabilityInstaller(AgentHome(tmp_path / ".bianbu-agent"))

    with pytest.raises(ValueError, match="capability_id"):
        installer.inspect("skill", capability_id)


def test_failed_copy_rolls_back_the_staging_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "research"
    _write_skill(source)
    installer = CapabilityInstaller(home)

    def fail_copy(*args: object, **kwargs: object) -> None:
        raise OSError("copy failed")

    monkeypatch.setattr("bianbu_agent.plugins.installer.shutil.copy2", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        installer.install_skill(source)

    assert not (home.skills_dir / "research").exists()
    assert not tuple(home.staging_dir.iterdir())


def test_uninstall_refuses_modified_owned_target_then_removes_exact_verified_target(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "research"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    record = installer.install_skill(source)
    target = home.skills_dir / record.capability_id
    (target / "references" / "guide.md").write_text("modified\n", encoding="utf-8")

    assert installer.verify("skill", "research") is False
    with pytest.raises(ValueError, match="digest"):
        installer.uninstall("skill", "research")
    assert target.is_dir()
    assert not tuple(home.staging_dir.glob("uninstall-*"))


def test_standalone_operations_reject_plugin_owned_metadata(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "research"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    record = installer.install_skill(source)
    target = home.skills_dir / record.capability_id
    plugin_record = InstalledCapabilityRecord(
        kind="skill",
        capability_id=record.capability_id,
        version=record.version,
        content_digest=record.content_digest,
        origin="plugin",
        owner_plugin_id="vendor.example",
    )
    (target / OWNERSHIP_FILENAME).write_bytes(plugin_record.to_json_bytes())

    with pytest.raises(ValueError, match="standalone"):
        installer.inspect("skill", "research")
    with pytest.raises(ValueError, match="standalone"):
        installer.uninstall("skill", "research")
    assert target.is_dir()


def test_uninstall_leaves_an_unowned_swapped_target_in_place(tmp_path: Path) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "research"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    record = installer.install_skill(source)
    target = home.skills_dir / record.capability_id
    moved_owned = home.staging_dir / "owned-before-swap"
    target.rename(moved_owned)
    target.mkdir()
    (target / "attacker.txt").write_text("retain me\n", encoding="utf-8")

    assert installer.uninstall("skill", "research") is False

    assert target.is_dir()
    assert (target / "attacker.txt").read_text(encoding="utf-8") == "retain me\n"
    assert not tuple(home.staging_dir.glob("uninstall-*"))
    assert moved_owned.is_dir()


def test_atomic_publish_never_clobbers_a_target_created_during_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "research"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    original_write = __import__(
        "bianbu_agent.plugins.installer", fromlist=["write_ownership_record"]
    ).write_ownership_record

    def create_conflict(root: Path, record: InstalledCapabilityRecord) -> Path:
        result = original_write(root, record)
        target = home.skills_dir / record.capability_id
        target.mkdir()
        (target / "sentinel").write_text("do not replace\n", encoding="utf-8")
        return result

    monkeypatch.setattr(
        "bianbu_agent.plugins.installer.write_ownership_record", create_conflict
    )
    with pytest.raises(FileExistsError):
        installer.install_skill(source)

    assert (home.skills_dir / "research" / "sentinel").read_text(encoding="utf-8") == "do not replace\n"


def test_successful_logical_uninstall_removes_active_target_and_retains_tombstone(
    tmp_path: Path,
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "source"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    installer.install_skill(source)

    assert installer.uninstall("skill", "research") is True

    assert not (home.skills_dir / "research").exists()
    quarantined = tuple(home.staging_dir.glob("retired-skill-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "SKILL.md").is_file()
    assert (quarantined[0] / OWNERSHIP_FILENAME).is_file()


def test_uninstall_keeps_the_initially_verified_descriptor_through_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "source"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    installer.install_skill(source)
    original_verify = installer._verify_opened_target
    descriptors: list[tuple[int, int, int, str]] = []

    def record_descriptor(
        kind: str,
        capability_id: str,
        directory_fd: int,
        *,
        phase: str,
        declared: bool = False,
    ):
        inode = os.fstat(directory_fd)
        descriptors.append((directory_fd, inode.st_dev, inode.st_ino, phase))
        return original_verify(
            kind, capability_id, directory_fd, phase=phase, declared=declared
        )

    monkeypatch.setattr(installer, "_verify_opened_target", record_descriptor)
    assert installer.uninstall("skill", "research") is True

    assert [item[3] for item in descriptors] == ["installed", "quarantined"]
    assert descriptors[0][:3] == descriptors[1][:3]


def test_quarantine_name_swap_never_restores_or_deletes_a_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = AgentHome(tmp_path / ".bianbu-agent")
    source = tmp_path / "source"
    _write_skill(source)
    installer = CapabilityInstaller(home)
    installer.install_skill(source)
    original_rename = installer._rename_noreplace

    def swap_after_quarantine(
        old_directory_fd: int,
        old_name: str,
        new_directory_fd: int,
        new_name: str,
    ) -> None:
        original_rename(old_directory_fd, old_name, new_directory_fd, new_name)
        if new_name.startswith("retired-skill-"):
            quarantined = Path(f"/proc/self/fd/{new_directory_fd}") / new_name
            quarantined.rename(home.staging_dir / "verified-original")
            quarantined.mkdir()
            (quarantined / "attacker.txt").write_text("do not restore\n", encoding="utf-8")

    monkeypatch.setattr(installer, "_rename_noreplace", swap_after_quarantine)
    with pytest.raises(ValueError, match="quarantine name changed"):
        installer.uninstall("skill", "research")

    assert not (home.skills_dir / "research").exists()
    replacements = tuple(home.staging_dir.glob("retired-skill-*"))
    assert len(replacements) == 1
    assert (replacements[0] / "attacker.txt").read_text(encoding="utf-8") == "do not restore\n"
    assert (home.staging_dir / "verified-original" / "SKILL.md").is_file()
