#!/usr/bin/python3.14
"""Provision one user's private Bianbu Agent profile from packaged defaults."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Iterator, NamedTuple

import yaml

from bianbu_agent.provisioning import AGENT_ID_PATTERN, new_agent_id, system_starter_id, archive_user_starter
from bianbu_agent.agents.system_paths import system_agent_directory


MANAGED_KINDS = ("agents", "tools", "skills", "plugins", "mcps", "models")
# Earlier package generations shipped the Intent server as a standalone MCP
# instead of a Plugin member. The packaged Plugin owns it now, so the
# standalone copy is retired (after a backup) on every start.
RETIRED_MCP_IDS = ("bibit-intent",)
# Extension roots are created on demand by the manager that writes them.
# "runs" and "session-logs" are legacy: run records and the harness event log
# both live under "sessions" now. "cache" lost its last writer when the retired
# format migrations were removed. "hooks" held standalone Hook packages, which
# are gone: a Plugin contributes its Hooks as package members. They are only
# dropped while empty, so a profile that still holds content keeps it.
REQUIRED_DIRECTORIES = (
    "agents",
    "models",
    "sessions",
    "workspace",
    "logs",
    "runtime",
    "backups/package-upgrade",
)
LEGACY_DIRECTORIES = ("runs", "session-logs", "cache", "hooks")
# The Agent JSON migration and its lock file are gone; a profile that ran an
# older generation keeps the empty lock behind.  Only empty regular files are
# dropped, so anything a user put there survives.
LEGACY_FILES = (
    ".agent-json-migration.lock",
    "runtime/worker/.agent-json-migration.lock",
)
# Extension roots are created on demand by the manager that writes them, so an
# empty one is always a leftover. They are pruned on every start, including the
# starts where the packaged profile did not change and the component sync is
# skipped.
OPTIONAL_DIRECTORIES = ("mcps", "plugins", "skills", "tools")
STATE_NAME = ".managed-profile.json"
RECOMMENDED_AGENT_NAME = "BibitEdgeAssistant"


#: Execution modes a package release retired.  ``auto`` asked securityd to
#: choose between host and sandbox; the Runtime now states the mode itself, so
#: a config that still carries it is moved to the packaged default.
RETIRED_EXECUTION_MODES = ("auto",)


class ProvisionError(RuntimeError):
    """Raised when a user profile cannot be updated safely."""


class ProvisionResult(NamedTuple):
    updated: bool
    profile_digest: str


def _port_for_uid(uid: int) -> int:
    if uid < 1000:
        return 9899
    return 9899 + ((uid - 1000) % 55637)


def _require_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ProvisionError(f"{label} must not be a symbolic link: {path}")
    if not path.is_dir():
        raise ProvisionError(f"{label} is not a directory: {path}")
    return path


def _component_ids(profile: Path) -> dict[str, list[str]]:
    # Package definitions are read directly from the system resource root.
    # Empty desired sets retire only IDs recorded by previous provisioners.
    return {kind: [] for kind in MANAGED_KINDS}


def _validated_component_id(value: object) -> str:
    if not isinstance(value, str):
        raise ProvisionError("managed component ID must be a string")
    if (
        not value
        or value in {".", ".."}
        or "\0" in value
        or "/" in value
        or (os.altsep is not None and os.altsep in value)
    ):
        raise ProvisionError(f"unsafe managed component ID: {value!r}")
    return value


def _recommended_agent_ids(root: Path) -> list[str]:
    agents_root = root / "agents"
    matches: list[str] = []
    for directory in sorted(agents_root.iterdir(), key=lambda item: item.name):
        if directory.is_symlink():
            raise ProvisionError(
                f"recommended Agent directory must not be a symbolic link: {directory}"
            )
        if not directory.is_dir():
            continue
        manifest = directory / "agent.yaml"
        if manifest.is_symlink():
            raise ProvisionError(
                f"recommended Agent must not be a symbolic link: {manifest}"
            )
        if not manifest.is_file():
            continue
        try:
            document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        agent = document.get("agent") if isinstance(document, dict) else None
        if isinstance(agent, dict) and agent.get("name") == RECOMMENDED_AGENT_NAME:
            matches.append(directory.name)
    return matches


def _rebind_starter_sessions(root: Path, previous_ids: list[str], agent_id: str) -> None:
    previous = set(previous_ids) - {agent_id}
    if not previous:
        return
    for path in sorted((root / "sessions").glob("*.json")):
        if path.is_symlink():
            raise ProvisionError(f"session record must not be a symbolic link: {path}")
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict) and document.get("agent_id") in previous:
            document["agent_id"] = agent_id
            _atomic_private_bytes(
                path, (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
            )


def _hash_path(digest: hashlib._Hash, root: Path, path: Path) -> None:
    relative = path.relative_to(root).as_posix().encode("utf-8")
    if path.is_symlink():
        raise ProvisionError(f"packaged profile must not contain a symbolic link: {path}")
    if path.is_dir():
        digest.update(b"D\0" + relative + b"\0")
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            _hash_path(digest, root, child)
        return
    if not path.is_file():
        raise ProvisionError(f"packaged profile contains an unsupported entry: {path}")
    digest.update(b"F\0" + relative + b"\0")
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)


def _profile_digest(profile: Path) -> str:
    digest = hashlib.sha256(b"system-resources-v1")
    for name in ("config.yaml", "starter-agent.yaml", *MANAGED_KINDS):
        path = profile / name
        if path.exists() or path.is_symlink():
            _hash_path(digest, profile, path)
    return digest.hexdigest()


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    _hash_path(digest, path.parent, path)
    return digest.hexdigest()


def _initial_config(profile: Path, uid: int) -> bytes:
    source = profile / "config.yaml"
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProvisionError(f"could not read packaged config: {exc}") from exc
    if not isinstance(document, dict):
        raise ProvisionError("packaged config must be a mapping")
    document["data_dir"] = "~/.bianbu-agent"
    service = document.get("service")
    if not isinstance(service, dict):
        raise ProvisionError("packaged config must contain service settings")
    service["host"] = service.get("host", "0.0.0.0")
    service["port"] = _port_for_uid(uid)
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")


def _initial_agent(profile: Path, agent_id: str) -> bytes:
    source = profile / "starter-agent.yaml"
    try:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProvisionError(f"could not read packaged starter Agent: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("agent"), dict):
        raise ProvisionError("packaged starter Agent is invalid")
    document["agent"]["id"] = agent_id
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")


def _read_state(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise ProvisionError(f"managed profile state must not be a symbolic link: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProvisionError(f"could not read managed profile state: {exc}") from exc
    if not isinstance(document, dict):
        raise ProvisionError("managed profile state must be an object")
    return document


def _atomic_private_json(path: Path, document: dict[str, object]) -> None:
    payload = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
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


def _atomic_private_bytes(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
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


def _backup_config(root: Path, digest: str, source: Path) -> Path:
    base = root / "backups" / "package-upgrade" / digest[:12] / "config.yaml"
    candidate = base
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, candidate)
    return candidate


def _backup_starter_agent(
    root: Path, digest: str, agent_id: str, source: Path
) -> Path:
    base = (
        root
        / "backups"
        / "package-upgrade"
        / digest[:12]
        / "agents"
        / agent_id
        / "agent.yaml"
    )
    candidate = base
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    candidate.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, candidate)
    return candidate


def _replace_config(root: Path, digest: str, payload: bytes) -> bool:
    path = root / "config.yaml"
    if path.is_symlink():
        raise ProvisionError(f"user config must not be a symbolic link: {path}")
    if path.exists():
        if not path.is_file():
            raise ProvisionError(f"user config must be a regular file: {path}")
        if path.read_bytes() == payload:
            return False
        _backup_config(root, digest, path)
    _atomic_private_bytes(path, payload)
    return True


def _apply_profile_presets(root: Path, payload: bytes) -> bool:
    """Hand a new device preset section to an existing user config.

    ``presets`` is device policy: the packaged profile decides which
    capabilities every Expert receives, and a profile that starts declaring
    them must reach configs written before the key existed.  Every other key
    stays exactly as the user left it, and a config that already carries
    ``presets`` keeps its own value.
    """

    path = root / "config.yaml"
    if path.is_symlink() or not path.is_file():
        return False
    try:
        current = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile = yaml.safe_load(payload.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProvisionError(f"could not read config for the preset update: {exc}") from exc
    if not isinstance(current, dict) or not isinstance(profile, dict):
        return False
    if "presets" in current or "presets" not in profile:
        return False
    document = dict(current)
    document["presets"] = profile["presets"]
    payload_bytes = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    if payload_bytes == path.read_bytes():
        return False
    _atomic_private_bytes(path, payload_bytes)
    return True


def _requested_mode(document: object, scope: str) -> str | None:
    """The normalised execution mode one scope declares, when it declares one."""

    if not isinstance(document, dict):
        return None
    runtime = document.get("runtime")
    section = runtime.get(scope) if isinstance(runtime, dict) else None
    execution = section.get("execution") if isinstance(section, dict) else None
    mode = execution.get("requested_mode") if isinstance(execution, dict) else None
    if not isinstance(mode, str):
        return None
    return mode.strip().lower() or None


def _apply_profile_execution_defaults(root: Path, digest: str, payload: bytes) -> bool:
    """Carry the packaged execution defaults into an existing user config.

    ``runtime.{control,worker}.execution.requested_mode`` is device policy: it
    is the mode every Expert that states none of its own runs in.  A device
    that still carries a retired mode would keep the retired behaviour after
    the upgrade, so an unchanged retired value follows the packaged profile.
    Every other value -- an explicit ``sandbox``, an Expert's own choice --
    keeps its place.
    """

    path = root / "config.yaml"
    if path.is_symlink() or not path.is_file():
        return False
    try:
        current = yaml.safe_load(path.read_text(encoding="utf-8"))
        profile = yaml.safe_load(payload.decode("utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProvisionError(
            f"could not read config for the execution default update: {exc}"
        ) from exc
    if not isinstance(current, dict) or not isinstance(profile, dict):
        return False
    changed = False
    for scope in ("control", "worker"):
        packaged = _requested_mode(profile, scope)
        if packaged is None or packaged in RETIRED_EXECUTION_MODES:
            continue
        if _requested_mode(current, scope) in RETIRED_EXECUTION_MODES:
            current["runtime"][scope]["execution"]["requested_mode"] = packaged
            changed = True
    if not changed:
        return False
    payload_bytes = yaml.safe_dump(
        current, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    if payload_bytes == path.read_bytes():
        return False
    _backup_config(root, digest, path)
    _atomic_private_bytes(path, payload_bytes)
    return True


def _replace_starter_agent(
    root: Path, digest: str, agent_id: str, payload: bytes
) -> bool:
    path = root / "agents" / agent_id / "agent.yaml"
    directory = path.parent
    if directory.is_symlink():
        raise ProvisionError(
            f"starter Agent directory must not be a symbolic link: {directory}"
        )
    if directory.exists() and not directory.is_dir():
        raise ProvisionError(
            f"starter Agent directory must be a directory: {directory}"
        )
    directory.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ProvisionError(f"starter Agent must not be a symbolic link: {path}")
    if path.exists():
        if not path.is_file():
            raise ProvisionError(f"starter Agent must be a regular file: {path}")
        if path.read_bytes() == payload:
            return False
        _backup_starter_agent(root, digest, agent_id, path)
    _atomic_private_bytes(path, payload)
    return True


def _remove_entry(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _prune_empty_directory(path: Path) -> None:
    """Drop a directory the Runtime no longer owns when nothing is in it."""
    if path.is_symlink() or not path.is_dir():
        return
    try:
        path.rmdir()
    except OSError:
        pass


def _prune_empty_tree(path: Path) -> None:
    """Drop a retired root when it holds nothing but empty directories.

    A retired root can keep a leftover skeleton alive (``cache/`` holding an
    empty ``agent-json-migration/``) even though it carries no data.  Only
    directories are removed, bottom-up, and only while they are empty, so any
    real content keeps the whole branch.
    """
    if path.is_symlink() or not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        _prune_empty_tree(child)
    try:
        path.rmdir()
    except OSError:
        pass


def _prune_retired_file(path: Path) -> None:
    """Drop a lock file left behind by a retired migration when it is empty."""
    if path.is_symlink() or not path.is_file():
        return
    try:
        if path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def _backup_path(root: Path, digest: str, kind: str, component_id: str) -> Path:
    base = root / "backups" / "package-upgrade" / digest[:12] / kind / component_id
    candidate = base
    suffix = 2
    while candidate.exists() or candidate.is_symlink():
        candidate = base.with_name(f"{base.name}-{suffix}")
        suffix += 1
    return candidate


def _backup_component(
    root: Path,
    digest: str,
    kind: str,
    component_id: str,
    source: Path | None,
) -> None:
    target = root / kind / component_id
    if not target.exists() and not target.is_symlink():
        return
    if target.is_symlink():
        raise ProvisionError(f"managed component must not be a symbolic link: {target}")
    if not target.is_dir():
        raise ProvisionError(f"managed component is not a directory: {target}")
    if source is not None and _tree_digest(target) == _tree_digest(source):
        return
    backup = _backup_path(root, digest, kind, component_id)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(target, backup)


def _make_private_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ProvisionError(f"managed model tree must not contain a symbolic link: {path}")
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.is_file():
            os.chmod(path, 0o600)
        else:
            raise ProvisionError(f"managed model tree contains an unsupported entry: {path}")


def _sync_components(
    profile: Path,
    root: Path,
    digest: str,
    previous: dict[str, list[str]],
    current: dict[str, list[str]],
) -> None:
    for component_id in RETIRED_MCP_IDS:
        _backup_component(root, digest, "mcps", component_id, None)
        _remove_entry(root / "mcps" / component_id)
        previous = {**previous, "mcps": [item for item in previous.get("mcps", [])
                                        if item != component_id]}
    staging = Path(tempfile.mkdtemp(prefix="profile-", dir=root / ".staging"))
    try:
        for kind, component_ids in current.items():
            for component_id in component_ids:
                shutil.copytree(
                    profile / kind / component_id,
                    staging / kind / component_id,
                )
                if kind in {"agents", "models"}:
                    _make_private_tree(staging / kind / component_id)
        for kind in MANAGED_KINDS:
            target_root = root / kind
            if target_root.is_symlink():
                raise ProvisionError(f"managed directory must not be a symbolic link: {target_root}")
            if not current[kind] and not previous.get(kind):
                # Nothing to install or retire for this kind: keep the profile
                # free of the empty extension roots it never uses.
                _prune_empty_directory(target_root)
                continue
            target_root.mkdir(parents=True, exist_ok=True)
            current_ids = set(current[kind])
            for component_id in sorted(set(previous.get(kind, [])) - current_ids):
                _backup_component(root, digest, kind, component_id, None)
                _remove_entry(target_root / component_id)
            for component_id in current[kind]:
                source = profile / kind / component_id
                target = target_root / component_id
                _backup_component(root, digest, kind, component_id, source)
                if target.is_symlink():
                    raise ProvisionError(f"managed component must not be a symbolic link: {target}")
                _remove_entry(target)
                staged = staging / kind / component_id
                os.replace(staged, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@contextmanager
def _provision_lock(root: Path) -> Iterator[None]:
    lock_path = root / "runtime" / "provision.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def provision(profile_root: Path, home: Path, uid: int | None = None) -> ProvisionResult:
    profile = Path(profile_root).expanduser().resolve()
    _require_directory(profile, "default profile")
    resolved_home = Path(home).expanduser().resolve()
    _require_directory(resolved_home, "user home")
    selected_uid = os.getuid() if uid is None else int(uid)
    root = resolved_home / ".bianbu-agent"
    if root.is_symlink():
        raise ProvisionError(f"user profile must not be a symbolic link: {root}")
    root.mkdir(mode=0o700, exist_ok=True)
    if not root.is_dir():
        raise ProvisionError(f"user profile is not a directory: {root}")
    os.chmod(root, 0o700)
    for relative in (*REQUIRED_DIRECTORIES, ".staging"):
        directory = root / relative
        if directory.is_symlink():
            raise ProvisionError(f"user profile directory must not be a symbolic link: {directory}")
        directory.mkdir(parents=True, exist_ok=True)
    for relative in LEGACY_DIRECTORIES:
        _prune_empty_tree(root / relative)
    for relative in OPTIONAL_DIRECTORIES:
        _prune_empty_directory(root / relative)
    for relative in LEGACY_FILES:
        _prune_retired_file(root / relative)

    digest = _profile_digest(profile)
    current = _component_ids(profile)
    state_path = root / STATE_NAME
    config_payload = _initial_config(profile, selected_uid)
    with _provision_lock(root):
        state = _read_state(state_path)
        raw_previous = state.get("managed", {})
        previous = {
            kind: [_validated_component_id(item) for item in raw_previous.get(kind, [])]
            if isinstance(raw_previous, dict) and isinstance(raw_previous.get(kind, []), list)
            else []
            for kind in MANAGED_KINDS
        }
        system_id = system_starter_id(root)
        starter_agent_id = system_id or str(state.get("starter_agent_id") or "").strip()
        recommended_agent_ids = _recommended_agent_ids(root)
        reusable_agent_ids = [
            agent_id
            for agent_id in recommended_agent_ids
            if AGENT_ID_PATTERN.fullmatch(agent_id) is not None
        ]
        if not system_id and reusable_agent_ids and starter_agent_id not in reusable_agent_ids:
            starter_agent_id = reusable_agent_ids[0]
        elif not starter_agent_id:
            starter_agent_id = new_agent_id()
        if AGENT_ID_PATTERN.fullmatch(starter_agent_id) is None:
            raise ProvisionError(f"invalid starter Agent ID: {starter_agent_id!r}")
        config_path = root / "config.yaml"
        if config_path.is_symlink():
            raise ProvisionError(f"user config must not be a symbolic link: {config_path}")
        config_changed = False
        if not config_path.exists():
            config_changed = _replace_config(root, digest, config_payload)
        else:
            # ``presets`` is device policy, and a device that starts declaring
            # it has to reach configs written before the key existed.  Tying
            # this to the profile digest is not enough: the profile can already
            # carry the section while the user config predates the code that
            # copies it, leaving both digests equal and the user without it.
            # Everything else stays exactly as the user left it.
            config_changed = _apply_profile_presets(root, config_payload)
            if _apply_profile_execution_defaults(root, digest, config_payload):
                config_changed = True
        agent_changed = False
        # The preset expert's declaration is ours, but editing a system Agent is
        # how a user makes an override, and the Agent store reads the user
        # profile first and skips a system package that repeats an ID -- so that
        # copy always wins.  Left alone it would also keep every later
        # publication from ever reaching the device.
        #
        # Replace it only when the declaration we publish actually changed, and
        # archive it first.  A device keeps whatever its user wrote until a
        # package ships a new default, which costs one overwrite per shipped
        # change instead of one per start.
        shipped = str(state.get("starter_digest") or "")
        starter_digest = ""
        if system_id:
            published = system_agent_directory(root) / system_id / "agent.yaml"
            if published.is_file():
                starter_digest = _tree_digest(published)
        if system_id and starter_digest and starter_digest != shipped:
            if (root / "agents" / system_id).exists():
                archive_user_starter(root, system_id)
            shipped = starter_digest
            agent_changed = True
        if state.get("profile_digest") == digest and not agent_changed:
            return ProvisionResult(config_changed or agent_changed, digest)
        _sync_components(profile, root, digest, previous, current)
        _atomic_private_json(
            state_path,
            {
                "schema_version": 1,
                "profile_digest": digest,
                "starter_agent_id": starter_agent_id,
                "starter_digest": shipped,
                "managed": current,
            },
        )
        return ProvisionResult(True, digest)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--uid", type=int, default=os.getuid())
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        result = provision(arguments.profile, arguments.home, arguments.uid)
    except (OSError, ProvisionError, ValueError) as exc:
        print(f"bianbu-agent profile provisioning failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"updated": result.updated, "profile_digest": result.profile_digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
