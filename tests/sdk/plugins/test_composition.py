from __future__ import annotations

from pathlib import Path

from bianbu_agent.plugins.composition import (
    compose_plugin_config,
    load_plugin_overlays,
)
from bianbu_agent.plugins.models import PluginManifest


def _manifest() -> PluginManifest:
    return PluginManifest(
        id="vendor.bundle",
        version="1.0.0",
        config_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "default": "safe"},
                "nested": {
                    "type": "object",
                    "default": {},
                    "properties": {
                        "limit": {"type": "integer", "default": 10},
                        "label": {"type": "string"},
                    },
                },
            },
            "additionalProperties": False,
        },
    )


def test_composes_defaults_runtime_home_and_programmatic_overlays() -> None:
    effective = compose_plugin_config(
        _manifest(),
        runtime_config={"nested": {"limit": 20, "label": "runtime"}},
        home_config={"mode": "fast", "nested": {"label": "home"}},
        spec_config={"nested": {"limit": 30}},
    )

    assert dict(effective.config) == {
        "mode": "fast",
        "nested": {"limit": 30, "label": "home"},
    }
    assert dict(effective.provenance) == {
        "mode": "home",
        "nested.label": "home",
        "nested.limit": "programmatic",
    }


def test_loads_strict_home_plugin_overlay(tmp_path: Path) -> None:
    path = tmp_path / "plugins.yaml"
    path.write_text(
        """\
schema_version: 1
plugins:
  vendor.bundle:
    enabled: false
    config:
      mode: fast
""",
        encoding="utf-8",
    )

    overlays = load_plugin_overlays(path)

    assert overlays["vendor.bundle"].enabled is False
    assert dict(overlays["vendor.bundle"].config) == {"mode": "fast"}


def test_missing_home_overlay_is_an_empty_layer(tmp_path: Path) -> None:
    assert load_plugin_overlays(tmp_path / "plugins.yaml") == {}


def test_provenance_drops_replaced_ancestor_and_descendant_paths() -> None:
    manifest = PluginManifest(
        id="vendor.bundle",
        version="1.0.0",
        config_schema={"type": "object", "additionalProperties": True},
    )

    scalar = compose_plugin_config(
        manifest,
        runtime_config={"value": {"nested": 1}},
        home_config={"value": "replacement"},
    )
    nested = compose_plugin_config(
        manifest,
        runtime_config={"value": "original"},
        home_config={"value": {"nested": 2}},
    )

    assert dict(scalar.provenance) == {"value": "home"}
    assert dict(nested.provenance) == {"value.nested": "home"}


def test_directory_config_is_validated_between_schema_and_runtime(tmp_path):
    from bianbu_agent.plugins.manifest import load_plugin_manifest
    (tmp_path / 'plugin.yaml').write_text('''schema_version: 1
name: vendor.bundle
version: 1.0.0
config_schema:
  type: object
  additionalProperties: false
  properties:
    limit: {type: integer, minimum: 1, default: 5}
    label: {type: string, default: schema}
''')
    (tmp_path / 'config.yaml').write_text('limit: 9\nlabel: package\n')
    manifest = load_plugin_manifest(tmp_path)
    effective = compose_plugin_config(manifest, runtime_config={'limit': 12})
    assert dict(effective.config) == {'limit': 12, 'label': 'package'}
    assert effective.provenance['label'] == 'plugin-config'
    assert effective.provenance['limit'] == 'runtime'


def test_directory_config_rejects_non_object_and_symlink(tmp_path):
    import pytest
    from bianbu_agent.plugins.manifest import load_plugin_manifest
    (tmp_path / 'plugin.yaml').write_text('schema_version: 1\nname: vendor.bundle\nversion: 1.0.0\n')
    config = tmp_path / 'config.yaml'
    config.write_text('- item\n')
    with pytest.raises(ValueError, match='config.yaml'):
        load_plugin_manifest(tmp_path)
    config.unlink()
    config.symlink_to(tmp_path / 'missing')
    with pytest.raises(ValueError, match='config.yaml'):
        load_plugin_manifest(tmp_path)


def test_composed_config_rejects_non_json_internal_objects():
    import pytest
    with pytest.raises(TypeError):
        compose_plugin_config(PluginManifest(id="vendor.bundle", version="1.0.0"),
                              runtime_config={"service": object()})
