"""Strict parser for the single directory Plugin manifest contract.

``plugin.yaml`` carries identity only: ``name``, ``version``, ``description`` and
the optional ``dependencies``/``config_schema``/``entrypoint``.  Contributions
are whatever the package actually contains under ``tools/``, ``skills/``,
``hooks/`` and ``mcps/``, so there is no declaration to keep in sync and no
second manifest to fall back to.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import json

import yaml

from bianbu_agent.plugins.models import (
    PluginContributionManifest,
    PluginDependency,
    PluginManifest,
)


PLUGIN_MANIFEST_FILENAME = "plugin.yaml"
APC_PLUGIN_MANIFEST_FILENAME = "apc-plugin.json"
_APC_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "name_zh",
        "name_en",
        "version",
        "description",
        "description_zh",
        "skills",
    }
)
_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "description",
        "dependencies",
        "config_schema",
        "entrypoint",
    }
)
_CONTRIBUTION_DIRECTORIES = ("tools", "skills", "hooks", "mcps")


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return {str(key): item for key, item in value.items()}


def _list(value: object, field_name: str) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return tuple(value)


def _text(value: object, field_name: str, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _names(value: object, field_name: str) -> tuple[str, ...]:
    values = _list(value, field_name)
    if any(not isinstance(item, str) for item in values):
        raise ValueError(f"{field_name} entries must be strings")
    return tuple(item for item in values if isinstance(item, str))


def _load_directory_config(root: Path) -> Mapping[str, object]:
    path = root / "config.yaml"
    if path.is_symlink():
        raise ValueError("config.yaml must be a regular file")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("config.yaml must be a regular file no larger than 1 MiB")
    try:
        config = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "config.yaml")
        # The same snapshot crosses the command Hook JSON boundary.
        json.dumps(config, allow_nan=False)
        return config
    except (OSError, UnicodeError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid config.yaml: {exc}") from exc


def _member_names(root: Path, directory: str) -> tuple[str, ...]:
    location = root / directory
    if not location.exists():
        return ()
    if location.is_symlink() or not location.is_dir():
        raise ValueError(f"{directory} must be a regular directory")
    names: list[str] = []
    for child in sorted(location.iterdir(), key=lambda item: item.name):
        if child.is_symlink() or not child.is_dir():
            raise ValueError(f"{directory} members must be regular directories")
        names.append(child.name)
    return tuple(names)


def _scan_contributions(root: Path) -> PluginContributionManifest:
    """Read the contributions a package actually ships."""

    scanned = {
        directory: _member_names(root, directory)
        for directory in _CONTRIBUTION_DIRECTORIES
    }
    return PluginContributionManifest(
        tools=scanned["tools"],
        skills=scanned["skills"],
        hooks=scanned["hooks"],
        mcps=scanned["mcps"],
    )


def load_plugin_manifest(root: Path) -> PluginManifest:
    """Parse one Plugin manifest without importing Plugin code.

    An APM-installed Agent may carry a submitted Plugin sub-resource, whose
    manifest is ``apc-plugin.json``.  A native ``plugin.yaml`` always wins, so
    the Runtime keeps its installed-package contract for everything it owns.
    """

    package_root = Path(root)
    path = package_root / PLUGIN_MANIFEST_FILENAME
    submitted = package_root / APC_PLUGIN_MANIFEST_FILENAME
    if (
        not path.exists()
        and not path.is_symlink()
        and submitted.is_file()
        and not submitted.is_symlink()
    ):
        return _load_apc_plugin(package_root, submitted)
    if path.is_symlink() or not path.is_file():
        raise ValueError("plugin.yaml is required and must be a regular file")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid plugin.yaml: {exc}") from exc
    payload = _mapping(document, "plugin.yaml")
    unknown = sorted(set(payload) - _ROOT_KEYS)
    if unknown:
        raise ValueError(f"unknown plugin.yaml key: {unknown[0]}")
    if payload.get("schema_version") != 1:
        raise ValueError("plugin.yaml schema_version must be 1")

    raw_dependencies = _list(payload.get("dependencies"), "dependencies")
    dependencies: list[PluginDependency] = []
    for index, raw in enumerate(raw_dependencies):
        dependency = _mapping(raw, f"dependencies[{index}]")
        unknown_dependency = sorted(set(dependency) - {"name", "version"})
        if unknown_dependency:
            raise ValueError(
                f"unknown dependency key: {unknown_dependency[0]}"
            )
        dependencies.append(
            PluginDependency(
                plugin_id=_text(
                    dependency.get("name"), f"dependencies[{index}].name"
                ),
                version=_text(
                    dependency.get("version"),
                    f"dependencies[{index}].version",
                    required=False,
                ),
            )
        )

    contributions = _scan_contributions(package_root)

    config_schema = _mapping(payload.get("config_schema", {}), "config_schema")
    return PluginManifest(
        id=_text(payload.get("name"), "name"),
        version=_text(payload.get("version"), "version"),
        description=_text(payload.get("description"), "description", required=False),
        dependencies=tuple(dependencies),
        config_schema=config_schema,
        config_defaults=_load_directory_config(package_root),
        contributions=contributions,
        entrypoint=_text(payload.get("entrypoint"), "entrypoint", required=False),
    )


def _load_apc_plugin(package_root: Path, path: Path) -> PluginManifest:
    """Read an Agent-private Plugin from its APM submission manifest."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid apc-plugin.json: {exc}") from exc
    payload = _mapping(document, "apc-plugin.json")
    unknown = sorted(set(payload) - _APC_ROOT_KEYS)
    if unknown:
        raise ValueError(f"unknown apc-plugin.json key: {unknown[0]}")
    if payload.get("schema_version") != 2:
        raise ValueError("apc-plugin.json schema_version must be 2")
    declared = _names(payload.get("skills"), "skills")
    if not declared:
        raise ValueError("apc-plugin.json skills must not be empty")
    resolved_root = package_root.resolve()
    for entry in declared:
        relative = Path(entry)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "skills"
            or ".." in relative.parts
        ):
            raise ValueError(f"unsupported apc-plugin.json skill path: {entry}")
        target = (package_root / relative).resolve()
        if not target.is_dir() or not target.is_relative_to(resolved_root):
            raise ValueError(f"apc-plugin.json skill is missing: {entry}")
    return PluginManifest(
        id=_text(payload.get("name"), "name"),
        version=_text(payload.get("version"), "version"),
        description=_text(payload.get("description"), "description", required=False),
        config_defaults=_load_directory_config(package_root),
        contributions=_scan_contributions(package_root),
    )


def plugin_definition_path(root: Path) -> Path:
    """Return the single file that describes one installed Plugin package."""

    root = Path(root)
    native = root / PLUGIN_MANIFEST_FILENAME
    if native.exists() or native.is_symlink():
        return native
    submitted = root / APC_PLUGIN_MANIFEST_FILENAME
    return submitted if submitted.exists() else native


def load_installed_plugin_manifest(root: Path) -> PluginManifest:
    """Describe one installed Plugin directory from its own ``plugin.yaml``."""

    return load_plugin_manifest(root)


__all__ = [
    "APC_PLUGIN_MANIFEST_FILENAME",
    "PLUGIN_MANIFEST_FILENAME",
    "load_installed_plugin_manifest",
    "load_plugin_manifest",
    "plugin_definition_path",
]
