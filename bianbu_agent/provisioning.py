"""Provision an ordinary starter Agent without introducing a global default."""

from __future__ import annotations

import os
import fcntl
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import stat

import yaml


AGENT_ID_PATTERN = re.compile(r"com\.bianbu\.[0-9a-f]{12}")
STARTER_ID_FILE = "starter-agent-id"
SYSTEM_OWNER_UID = 0


def system_starter_id(data_dir: Path) -> str | None:
    from bianbu_agent.agents.system_paths import system_agent_directory
    root = system_agent_directory(data_dir)
    marker = root.parent / STARTER_ID_FILE
    try:
        agent_id = marker.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if AGENT_ID_PATTERN.fullmatch(agent_id) is None:
        raise ValueError("invalid system starter Agent marker")
    if not (root / agent_id / "agent.yaml").is_file():
        return None
    return agent_id


def archive_user_starter(data_dir: Path, agent_id: str) -> None:
    """Preserve the previous local package after system publication succeeds."""
    if AGENT_ID_PATTERN.fullmatch(agent_id) is None:
        raise ValueError("invalid starter Agent ID")
    root = Path(data_dir).expanduser().resolve()
    source = root / "agents" / agent_id
    if source.is_symlink():
        raise ValueError("starter Agent directory must not be a symbolic link")
    if not source.exists():
        return
    backup = root / "backups" / "system-agent-migration"
    backup.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix=f"{agent_id}-", dir=backup)) / agent_id
    source.rename(destination)


def publish_system_starter(data_dir: Path, template: Path, agent_id: str) -> str:
    """Publish a read-only package declaration in a system-located profile.

    This entrypoint is only for an explicit administrator command or package
    provisioning, never an HTTP operation.  The profile directory belongs to the
    Runtime, so it is reconciled to exactly the declaration this Runtime ships:
    a package a retired generation left behind is removed here instead of being
    read past at startup.  User edits live in the user home, never in here.
    """
    from bianbu_agent.agents.system_paths import SYSTEM_PROFILE_ROOT, system_agent_directory
    from bianbu_agent.agents import AgentManifest
    import hashlib

    if os.geteuid() != SYSTEM_OWNER_UID:
        raise PermissionError("publishing a system Agent requires administrator permissions")
    if AGENT_ID_PATTERN.fullmatch(agent_id) is None:
        raise ValueError("invalid system starter Agent ID")
    document = yaml.safe_load(Path(template).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("agent"), dict):
        raise ValueError("starter Agent template must contain an agent mapping")
    if document["agent"].get("name") != "BibitEdgeAssistant":
        raise ValueError("system starter must be BibitEdgeAssistant")
    document["agent"]["id"] = agent_id
    document["agent"].setdefault("metadata", {}).setdefault("execution", {})["requested_mode"] = "host"
    payload = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
    AgentManifest.from_yaml(payload.decode())
    directory = system_agent_directory(data_dir)
    base = SYSTEM_PROFILE_ROOT
    if base.resolve() != base.absolute():
        raise ValueError("system profile path must not contain symbolic links")
    missing = []
    current = base
    while not current.exists():
        missing.append(current)
        current = current.parent
    for path in reversed(missing):
        path.mkdir(mode=0o755, exist_ok=True)
        os.chmod(path, 0o755)
    owner = Path(data_dir).lstat()
    if not stat.S_ISDIR(owner.st_mode):
        raise ValueError("Runtime data directory must be a real directory")
    for path in (base, directory.parent):
        path.mkdir(mode=0o755, exist_ok=True)
        entry = path.lstat()
        if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != SYSTEM_OWNER_UID or entry.st_mode & 0o022:
            raise PermissionError(f"unsafe system Agent directory: {path}")
        os.chmod(path, 0o755)
    directory.mkdir(mode=0o700, exist_ok=True)
    _secure_system_agents(directory, owner.st_uid)
    lock_path = directory.parent / ".provision.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        digest = hashlib.sha256(payload).hexdigest()
        digest_path = directory.parent / "starter-template-sha256"
        manifest_path = directory / agent_id / "agent.yaml"
        # The profile root is package-owned: it holds exactly the declaration
        # this Runtime ships.  A package a retired generation published here is
        # deleted rather than tolerated, so no read path has to guess a format.
        for stale in sorted(directory.iterdir(), key=lambda item: item.name):
            if stale.name == agent_id:
                continue
            _discard_system_package(stale)
        package_directory = manifest_path.parent
        package_directory.mkdir(mode=0o755, exist_ok=True)
        entry = package_directory.lstat()
        if entry.st_uid != SYSTEM_OWNER_UID:
            raise PermissionError(f"unsafe system Agent package: {package_directory}")
        if not stat.S_ISDIR(entry.st_mode):
            raise ValueError("system Agent package must not be a symbolic link")
        # mkdir() applies the process umask, and the packaged unit runs with
        # UMask=0077: relying on the mode argument alone leaves the package
        # directory unreadable for the Runtime user until some later publish
        # happens to reconcile it.
        os.chmod(package_directory, 0o755)
        _atomic_private_bytes(manifest_path, payload, mode=0o644)
        _atomic_private_bytes(directory.parent / STARTER_ID_FILE, (agent_id + "\n").encode(), mode=0o644)
        _atomic_private_bytes(digest_path, (digest + "\n").encode(), mode=0o644)
    finally:
        os.close(descriptor)
    return agent_id


def _discard_system_package(path: Path) -> None:
    """Remove one declaration the current Runtime does not publish."""

    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _secure_system_agents(directory: Path, previous_uid: int) -> None:
    """Reclaim old editable system copies without following links.

    Lock each directory's ownership before descending into it. This never
    touches user-home packages; subsequent edits create a user override.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(directory, flags)
    system_gid = 0 if SYSTEM_OWNER_UID == 0 else os.getgid()
    try:
        if os.fstat(descriptor).st_uid not in {SYSTEM_OWNER_UID, previous_uid}:
            raise PermissionError("system Agent directory belongs to another user")

        def secure(fd: int) -> None:
            os.fchown(fd, SYSTEM_OWNER_UID, system_gid)
            os.fchmod(fd, 0o755)
            for name in os.listdir(fd):
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(entry.st_mode):
                    child = os.open(name, flags, dir_fd=fd)
                    try:
                        secure(child)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(entry.st_mode) and entry.st_nlink == 1:
                    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        actual = os.fstat(child)
                        if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                            raise ValueError("hard links are not allowed in system Agent migration")
                        os.fchown(child, SYSTEM_OWNER_UID, system_gid)
                        os.fchmod(child, 0o644 | (actual.st_mode & 0o111))
                    finally:
                        os.close(child)
                else:
                    raise ValueError("links and special files are not allowed in system Agent migration")
        secure(descriptor)
    finally:
        os.close(descriptor)


def new_agent_id() -> str:
    return f"com.bianbu.{secrets.token_hex(6)}"


def _atomic_private_bytes(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


__all__ = ["AGENT_ID_PATTERN", "STARTER_ID_FILE", "new_agent_id"]
