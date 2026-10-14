from __future__ import annotations

from types import SimpleNamespace

import pytest

from bianbu_agent.plugins import PluginDependency, PluginManifest
from bianbu_agent.plugins.registry import (
    DuplicatePluginError,
    PluginDependencyError,
    PluginRegistry,
)


def _factory(_context):
    raise AssertionError("resolution must not invoke factories")


def _manifest(
    plugin_id: str,
    *,
    version: str = "1.0.0",
    requires: tuple[PluginDependency, ...] = (),
) -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        version=version,
        dependencies=requires,
    )


def test_registry_resolves_dependencies_before_dependents() -> None:
    registry = PluginRegistry()
    registry.register(_factory, _manifest("a.core"))
    registry.register(
        _factory,
        _manifest(
            "b.feature",
            requires=(PluginDependency("a.core", ">=1"),),
        ),
    )

    resolved = registry.resolve({"b.feature", "a.core"})

    assert [item.manifest.id for item in resolved] == ["a.core", "b.feature"]


def test_registry_uses_plugin_id_as_stable_tie_breaker() -> None:
    registry = PluginRegistry()
    registry.register(_factory, _manifest("z.last"))
    registry.register(_factory, _manifest("a.first"))

    resolved = registry.resolve({"z.last", "a.first"})

    assert [item.manifest.id for item in resolved] == ["a.first", "z.last"]


def test_registry_rejects_duplicate_ids() -> None:
    registry = PluginRegistry()
    registry.register(_factory, _manifest("a.core"))

    with pytest.raises(DuplicatePluginError, match="a.core"):
        registry.register(_factory, _manifest("a.core"))


def test_registry_rejects_missing_dependency_and_version_mismatch() -> None:
    registry = PluginRegistry()
    registry.register(
        _factory,
        _manifest("b.feature", requires=(PluginDependency("a.core", ">=2"),)),
    )
    with pytest.raises(PluginDependencyError, match="not enabled"):
        registry.resolve({"b.feature"})

    registry.register(_factory, _manifest("a.core", version="1.0.0"))
    with pytest.raises(PluginDependencyError, match="requires a.core>=2"):
        registry.resolve({"a.core", "b.feature"})


def test_registry_rejects_dependency_cycles() -> None:
    registry = PluginRegistry()
    registry.register(
        _factory,
        _manifest("a.core", requires=(PluginDependency("b.feature"),)),
    )
    registry.register(
        _factory,
        _manifest("b.feature", requires=(PluginDependency("a.core"),)),
    )
    with pytest.raises(PluginDependencyError, match="cycle"):
        registry.resolve({"a.core", "b.feature"})


def test_entry_point_discovery_uses_only_plugin_group(monkeypatch) -> None:
    registry = PluginRegistry()
    loaded: list[str] = []

    def load_factory():
        loaded.append("loaded")
        return (_factory, _manifest("entry.plugin"))

    entry_point = SimpleNamespace(
        name="entry.plugin",
        value="fake.module:factory",
        group="bianbu_agent.plugins",
        load=load_factory,
    )
    monkeypatch.setattr(
        "bianbu_agent.plugins.registry.entry_points",
        lambda group: [entry_point] if group == "bianbu_agent.plugins" else [],
    )

    diagnostics = registry.discover_entry_points()

    assert diagnostics == ()
    assert loaded == ["loaded"]
    assert registry.resolve({"entry.plugin"})[0].source == "entry_point"


def test_entry_point_load_failure_becomes_diagnostic(monkeypatch) -> None:
    def fail():
        raise ImportError("boom")

    entry_point = SimpleNamespace(
        name="bad.plugin",
        value="missing.module:factory",
        group="bianbu_agent.plugins",
        load=fail,
    )
    monkeypatch.setattr(
        "bianbu_agent.plugins.registry.entry_points",
        lambda group: [entry_point],
    )

    diagnostics = PluginRegistry().discover_entry_points()

    assert diagnostics[0].code == "entry_point_load_failed"
    assert diagnostics[0].plugin_id == "bad.plugin"
    assert "boom" in diagnostics[0].message
