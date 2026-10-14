"""Deterministic Plugin configuration layers shared by boot and inspection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import json

import yaml

from bianbu_agent.plugins.models import PluginManifest
from bianbu_agent.plugins.specs import PluginSpec


@dataclass(frozen=True, slots=True)
class PluginOverlay:
    enabled: bool = True
    config: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class EffectivePluginConfig:
    config: Mapping[str, object]
    provenance: Mapping[str, str]


def _copy_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _copy_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_copy_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_copy_value(item) for item in value]
    return value


def plugin_config_document(config: Mapping[str, object]) -> dict[str, object]:
    """Produce a detached, strict JSON document for command Hook input."""
    return json.loads(json.dumps(_copy_value(config), allow_nan=False))


def _schema_defaults(schema: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return result
    for raw_name, raw_child in properties.items():
        name = str(raw_name)
        if not isinstance(raw_child, Mapping):
            continue
        if "default" in raw_child:
            value = _copy_value(raw_child["default"])
        elif raw_child.get("type") == "object":
            value = _schema_defaults(raw_child)
            if not value:
                continue
        else:
            continue
        if isinstance(value, dict) and raw_child.get("type") == "object":
            nested = _schema_defaults(raw_child)
            value = _merge(value, nested)
        result[name] = value
    return result


def _merge(base: Mapping[str, object], overlay: Mapping[str, object]) -> dict[str, object]:
    result = _copy_value(base)
    assert isinstance(result, dict)
    for key, value in overlay.items():
        normalized = str(key)
        current = result.get(normalized)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[normalized] = _merge(current, value)
        else:
            result[normalized] = _copy_value(value)
    return result


def _record_leaves(
    value: Mapping[str, object],
    source: str,
    provenance: dict[str, str],
    prefix: str = "",
) -> None:
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping) and item:
            _record_leaves(item, source, provenance, path)
        else:
            provenance[path] = source


def _clear_provenance_conflicts(
    value: Mapping[str, object],
    provenance: dict[str, str],
    prefix: str = "",
) -> None:
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(item, Mapping) and item:
            _clear_provenance_conflicts(item, provenance, path)
            continue
        for existing in tuple(provenance):
            if (
                existing == path
                or existing.startswith(path + ".")
                or path.startswith(existing + ".")
            ):
                provenance.pop(existing, None)


def compose_plugin_config(
    manifest: PluginManifest,
    *,
    runtime_config: Mapping[str, object] | None = None,
    home_config: Mapping[str, object] | None = None,
    spec_config: Mapping[str, object] | None = None,
) -> EffectivePluginConfig:
    """Compose the five documented layers and validate the exact result."""

    layers = (
        ("manifest-default", _schema_defaults(manifest.config_schema)),
        ("plugin-config", manifest.config_defaults),
        ("runtime", dict(runtime_config or {})),
        ("home", dict(home_config or {})),
        ("programmatic", dict(spec_config or {})),
    )
    config: dict[str, object] = {}
    provenance: dict[str, str] = {}
    for source, layer in layers:
        _clear_provenance_conflicts(layer, provenance)
        config = _merge(config, layer)
        _record_leaves(layer, source, provenance)
    validated = manifest.validate_config(plugin_config_document(config))
    return EffectivePluginConfig(
        config=validated,
        provenance=MappingProxyType(dict(sorted(provenance.items()))),
    )


def load_plugin_overlays(path: Path) -> Mapping[str, PluginOverlay]:
    selected = Path(path)
    if not selected.exists():
        return MappingProxyType({})
    if selected.is_symlink() or not selected.is_file():
        raise ValueError("plugins.yaml must be a regular file")
    try:
        document = yaml.safe_load(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"invalid plugins.yaml: {exc}") from exc
    if not isinstance(document, Mapping):
        raise ValueError("plugins.yaml must contain an object")
    unknown = sorted(set(document) - {"schema_version", "plugins"})
    if unknown:
        raise ValueError(f"unknown plugins.yaml key: {unknown[0]}")
    if document.get("schema_version") != 1:
        raise ValueError("plugins.yaml schema_version must be 1")
    raw_plugins = document.get("plugins", {})
    if not isinstance(raw_plugins, Mapping):
        raise ValueError("plugins.yaml plugins must be an object")
    result: dict[str, PluginOverlay] = {}
    for raw_id, raw_overlay in raw_plugins.items():
        plugin_id = PluginSpec(id=str(raw_id)).id
        if not isinstance(raw_overlay, Mapping):
            raise ValueError(f"plugins.{plugin_id} must be an object")
        unknown_overlay = sorted(set(raw_overlay) - {"enabled", "config"})
        if unknown_overlay:
            raise ValueError(
                f"unknown plugins.{plugin_id} key: {unknown_overlay[0]}"
            )
        enabled = raw_overlay.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"plugins.{plugin_id}.enabled must be boolean")
        config = raw_overlay.get("config", {})
        if not isinstance(config, Mapping):
            raise ValueError(f"plugins.{plugin_id}.config must be an object")
        result[plugin_id] = PluginOverlay(
            enabled=enabled,
            config=MappingProxyType(dict(config)),
        )
    return MappingProxyType(result)


__all__ = [
    "EffectivePluginConfig",
    "PluginOverlay",
    "compose_plugin_config",
    "load_plugin_overlays",
]
