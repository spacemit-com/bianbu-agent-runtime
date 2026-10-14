"""Worker-owned Skill files retained for the lifetime of generation leases."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
import fcntl
import shutil
import tempfile
from typing import BinaryIO

from bianbu_agent._internal.locking import FilesystemLock
from bianbu_agent.plugins.ownership import installed_content_files


task_skills: ContextVar[object | None] = ContextVar("task_skills", default=None)


def get_task_skills():
    """Return detached metadata for the current task's pinned Skills, if any."""
    skills = task_skills.get()
    if skills is None:
        return None
    # ``display_name`` 一并带出：system prompt 只注入 display_name，
    # skill_view 必须能用同一个句柄回查，否则模型「看得见、打不开」。
    return {name: {"description": skill.spec.description,
                   "display_name": str(skill.spec.display_name or name),
                   "manifest_path": str(skill.spec.metadata.get("manifest_path") or "")}
            for name, skill in skills.items()}


class SkillVersions:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.directories: dict[int, Path] = {}
        self._locks: dict[Path, BinaryIO] = {}
        self.collect_orphans()

    def _reserve(self) -> Path:
        # The shared guard closes the mkdir/lease-file race with another Worker.
        with FilesystemLock(self.root / ".gc.lock", exclusive=True):
            directory = Path(tempfile.mkdtemp(prefix="generation-", dir=self.root))
            stream = (directory / ".lease").open("a+b")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            self._locks[directory] = stream
            return directory

    def collect_orphans(self) -> None:
        with FilesystemLock(self.root / ".gc.lock", exclusive=True):
            for directory in self.root.glob("generation-*"):
                if not directory.is_dir() or directory.is_symlink():
                    continue
                try:
                    with (directory / ".lease").open("a+b") as stream:
                        try:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        shutil.rmtree(directory)
                except OSError:
                    # Cleanup is retried later; it must not fail a running loop.
                    continue

    def _remove(self, directory: Path) -> None:
        with FilesystemLock(self.root / ".gc.lock", exclusive=True):
            shutil.rmtree(directory)
            stream = self._locks.pop(directory, None)
            if stream is not None:
                stream.close()

    def freeze(self, generation):
        if not generation.skills:
            return generation
        directory = self._reserve()
        replacements = {}
        try:
            for index, skill in enumerate(generation.skills.values()):
                manifest = skill.spec.metadata.get("manifest_path")
                if not manifest:
                    continue
                source = Path(str(manifest)).parent
                target = directory / str(index)
                for path in installed_content_files(source):
                    copied = target / path.relative_to(source)
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, copied)
                metadata = dict(skill.spec.metadata)
                for key in ("manifest_path", "source_path"):
                    if metadata.get(key):
                        metadata[key] = str(target / Path(str(metadata[key])).relative_to(source))
                resources = {
                    key: str(target / Path(value).relative_to(source))
                    for key, value in skill.spec.resources.items()
                }
                replacements[id(skill)] = replace(skill, spec=replace(
                    skill.spec, metadata=metadata, resources=resources,
                ))
            def frozen(skill):
                return replacements.get(id(skill), skill)
            catalog = generation.capability_catalog
            if catalog is not None:
                catalog = replace(catalog, skills={key: frozen(value) for key, value in catalog.skills.items()})
            plugins = {
                key: replace(value, contributions=replace(
                    value.contributions, skills=tuple(frozen(skill) for skill in value.contributions.skills),
                )) for key, value in generation.plugins.items()
            }
            result = replace(generation, plugins=plugins, capability_catalog=catalog)
        except BaseException:
            self._remove(directory)
            raise
        self.directories[generation.id] = directory
        return result

    def collect(self, live: set[int]) -> None:
        for generation_id, directory in tuple(self.directories.items()):
            if generation_id not in live:
                try:
                    self._remove(directory)
                except OSError:
                    continue
                del self.directories[generation_id]
        self.collect_orphans()
