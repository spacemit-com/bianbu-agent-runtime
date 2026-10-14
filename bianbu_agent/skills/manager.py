"""User-installed Skills layered over read-only packaged Skills."""

from bianbu_agent._internal.package_manager import StandalonePackageManager
from bianbu_agent.home import AgentHome
from bianbu_agent.plugins.ownership import InstalledCapabilityRecord
from bianbu_agent.skills.loader import DirectorySkillLoader
from bianbu_agent.system_resources import system_resource_dir


class SkillPackageManager(StandalonePackageManager):
    def __init__(self, home: AgentHome, *, file_only: bool = False) -> None:
        super().__init__(home, "skill", file_only=file_only)

    def _system_record(self, package_id: str) -> InstalledCapabilityRecord | None:
        from bianbu_agent.plugins.installer import CapabilityInstaller

        package_id = CapabilityInstaller._validate_id(package_id)
        root = system_resource_dir("skills") / package_id
        if not root.exists():
            return None
        if root.is_symlink():
            raise ValueError("system Skill must be a regular directory")
        spec = DirectorySkillLoader().load(root).spec
        return InstalledCapabilityRecord(
            kind="skill", capability_id=spec.name, version=spec.version,
            content_digest=spec.metadata["content_digest"],
        )

    def list(self) -> tuple[InstalledCapabilityRecord, ...]:
        root = system_resource_dir("skills")
        records = {}
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    record = self._system_record(child.name)
                    if record is not None:
                        records[record.capability_id] = record
        records.update((record.capability_id, record) for record in super().list())
        return tuple(records[key] for key in sorted(records))

    def inspect(self, package_id: str) -> InstalledCapabilityRecord | None:
        return super().inspect(package_id) or self._system_record(package_id)

    def verify(self, package_id: str) -> bool:
        if super().inspect(package_id) is not None:
            return super().verify(package_id)
        return self._system_record(package_id) is not None

    def discard_install(self, installed: InstalledCapabilityRecord) -> bool:
        """Remove only the exact user declaration whose activation failed."""
        return self._installer._uninstall(
            "skill", installed.capability_id, expected_origin="standalone",
            expected_owner_plugin_id=None, expected_record=installed,
        )

    def assert_deletable(self, package_id: str) -> None:
        if self._system_record(package_id) is not None:
            raise ValueError("system Skill cannot be uninstalled")

    def uninstall(self, package_id: str) -> bool:
        self.assert_deletable(package_id)
        return super().uninstall(package_id)


__all__ = ["SkillPackageManager"]
