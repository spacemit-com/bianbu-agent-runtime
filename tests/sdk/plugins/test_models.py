from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bianbu_agent.plugins import (
    PluginDiagnostic,
    PluginConfigError,
    PluginDependency,
    PluginHealth,
    PluginManifest,
    PluginState,
)
from bianbu_agent.plugins.models import failure_message


def test_manifest_normalizes_and_validates_identity_and_versions() -> None:
    manifest = PluginManifest(
        id=" bianbu.memory.local ",
        version="1.2.0",
        config_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
    )

    assert manifest.id == "bianbu.memory.local"
    assert dict(manifest.validate_config({"limit": 8})) == {"limit": 8}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "Memory"),
        ("id", "memory namespace"),
        ("version", "not-a-version"),
    ],
)
def test_manifest_rejects_invalid_identity_or_versions(
    field: str,
    value: str,
) -> None:
    values = {
        "id": "bianbu.memory.local",
        "version": "1.0.0",
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        PluginManifest(**values)


def test_manifest_rejects_duplicate_and_self_dependencies() -> None:
    dependency = PluginDependency("bianbu.storage.local", ">=1")

    with pytest.raises(ValueError, match="duplicate dependency"):
        PluginManifest(
            id="bianbu.memory.local",
            version="1.0.0",
            dependencies=(dependency, dependency),
        )

    with pytest.raises(ValueError, match="depend on itself"):
        PluginManifest(
            id="bianbu.memory.local",
            version="1.0.0",
            dependencies=(PluginDependency("bianbu.memory.local"),),
        )


def test_manifest_reports_exact_config_path_and_is_immutable() -> None:
    schema = {
        "type": "object",
        "properties": {
            "storage": {
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "required": ["limit"],
            }
        },
        "required": ["storage"],
    }
    manifest = PluginManifest(
        id="bianbu.memory.local",
        version="1.0.0",
        config_schema=schema,
    )

    with pytest.raises(PluginConfigError) as raised:
        manifest.validate_config({"storage": {"limit": "many"}})

    assert raised.value.path == ("storage", "limit")
    with pytest.raises(TypeError):
        manifest.config_schema["type"] = "array"
    with pytest.raises(FrozenInstanceError):
        manifest.version = "2.0.0"


def test_health_and_state_are_stable_public_values() -> None:
    assert PluginState.RUNNING.value == "running"
    assert PluginHealth.healthy().status == "healthy"
    assert PluginHealth.degraded("slow").message == "slow"


def test_plugin_diagnostic_never_raises_on_an_empty_message() -> None:
    """Reporting a failure may not become the failure."""

    diagnostic = PluginDiagnostic(code="plugin_start_failed", message="")

    assert diagnostic.message == "plugin_start_failed"


def test_failure_message_falls_back_to_the_exception_type() -> None:
    assert failure_message(_SilentTransportError()) == "_SilentTransportError"
    assert failure_message(RuntimeError("boom")) == "boom"
    assert failure_message("") == "plugin failed"


class _SilentTransportError(ConnectionError):
    """Stand-in for httpx/anyio errors, whose ``str`` is empty."""

    def __str__(self) -> str:
        return ""
