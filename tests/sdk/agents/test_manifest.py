from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from bianbu_agent.agents import AgentManifest, AgentModel, AgentSpec, validate_agent_id


DEFAULT_MANIFEST = '''schema_version: 1
agent:
  id: bianbu-agent-default
  version: 1.0.0
  name: BianbuAgent
  name_zh: 边布智能助手
  description: Project-local Bianbu Agent
  soul: Help the user complete practical work safely.
  model: null
  tools:
    - '*'
  skills:
    - '*'
  metadata:
    avatar: 🤖
    enabled: true
    retries: 2
    tags:
      - default
      - local
'''


def test_manifest_parses_and_round_trips_declaration(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(DEFAULT_MANIFEST, encoding="utf-8")

    manifest = AgentManifest.from_path(path)

    assert manifest.schema_version == 1
    assert manifest.version == "1.0.0"
    assert manifest.spec == AgentSpec(
        id="bianbu-agent-default",
        plugins=(),
        name="BianbuAgent",
        name_zh="边布智能助手",
        description="Project-local Bianbu Agent",
        soul="Help the user complete practical work safely.",
        tools=("*",),
        skills=("*",),
        metadata={
            "avatar": "🤖",
            "enabled": True,
            "retries": 2,
            "tags": ["default", "local"],
        },
    )
    rewritten = AgentManifest.from_yaml(manifest.to_yaml())
    assert rewritten.schema_version == 2
    assert rewritten.version == manifest.version
    assert rewritten.spec == manifest.spec
    assert manifest.to_yaml().endswith("\n")


def test_manifest_reads_and_writes_the_v2_display_fields(tmp_path: Path) -> None:
    """The AgentHub/APM v2 record keeps its display fields on a round trip."""

    payload = DEFAULT_MANIFEST.replace("schema_version: 1", "schema_version: 2").replace(
        "  name_zh: 边布智能助手\n",
        "  name_zh: 边布智能助手\n"
        "  name_en: Bianbu Assistant\n"
        "  description_zh: 本地边布智能助手\n",
    )
    path = tmp_path / "agent.yaml"
    path.write_text(payload, encoding="utf-8")

    manifest = AgentManifest.from_path(path)

    assert manifest.schema_version == 2
    assert manifest.spec.name_zh == "边布智能助手"
    assert manifest.spec.name_en == "Bianbu Assistant"
    assert manifest.spec.description_zh == "本地边布智能助手"

    written = yaml.safe_load(manifest.to_yaml())
    assert written["schema_version"] == 2
    assert written["agent"]["name_en"] == "Bianbu Assistant"
    assert written["agent"]["description_zh"] == "本地边布智能助手"
    # A version 1 record still parses, so an older Runtime's record is readable.
    assert AgentManifest.from_yaml(DEFAULT_MANIFEST).schema_version == 1


def test_manifest_missing_mcps_defaults_to_no_server_access() -> None:
    manifest = AgentManifest.from_yaml(DEFAULT_MANIFEST)

    assert manifest.spec.mcps == ()
    assert manifest.spec.hooks == ()


def test_manifest_never_writes_agent_selected_hooks() -> None:
    """Hooks belong to Plugins, so agent.yaml never carries one."""

    manifest = AgentManifest(
        schema_version=1,
        version="1.0.0",
        spec=AgentSpec(id="greeter", name="Greeter"),
    )

    payload = yaml.safe_load(manifest.to_yaml())
    assert "hooks" not in payload["agent"]
    assert AgentManifest.from_yaml(manifest.to_yaml()).spec.hooks == ()

    with pytest.raises(ValueError, match="unsupported agent fields"):
        AgentManifest.from_yaml(
            "schema_version: 1\n"
            "agent:\n"
            "  id: greeter\n"
            "  name: Greeter\n"
            "  version: 1.0.0\n"
            "  hooks: [greeting.direct-reply]\n"
        )


def test_manifest_round_trips_config_only_permissions() -> None:
    permissions = {
        "level": "L3",
        "capabilities": [
            {
                "name": "file.write",
                "description": "Write user documents",
                "domain": "execute",
                "risk": "L3",
                "confirmation": "always",
                "scope": "~/Documents",
            }
        ],
        "network": {
            "mode": "allowlist",
            "domains": ["api.openai.com"],
        },
    }
    manifest = AgentManifest(
        schema_version=1,
        version="1.2.0",
        spec=AgentSpec(
            id="guarded-writer",
            name="Guarded Writer",
            permissions=permissions,
        ),
    )

    restored = AgentManifest.from_yaml(manifest.to_yaml())

    assert dict(restored.spec.permissions) == permissions
    payload = yaml.safe_load(manifest.to_yaml())
    assert payload["agent"]["permissions"] == permissions


def test_manifest_round_trips_structured_agent_model() -> None:
    manifest = AgentManifest(
        schema_version=1,
        version="1.0.0",
        spec=AgentSpec(
            id="researcher",
            name="Researcher",
            model=AgentModel(
                provider="openrouter",
                name="qwen/qwen3-235b",
            ),
        ),
    )

    restored = AgentManifest.from_yaml(manifest.to_yaml())

    assert restored.spec.model == AgentModel(
        provider="openrouter",
        name="qwen/qwen3-235b",
    )


def test_manifest_rejects_a_legacy_model_string() -> None:
    with pytest.raises(ValueError, match="agent.model must be a mapping or null"):
        AgentManifest.from_yaml(
            DEFAULT_MANIFEST.replace("  model: null", "  model: model-old")
        )


def test_manifest_round_trips_explicit_whole_mcp_servers() -> None:
    manifest = AgentManifest(
        schema_version=1,
        version="1.0.0",
        spec=AgentSpec(
            id="weather-researcher",
            name="Weather Researcher",
            mcps=("mcp.weather", "office.core/filesystem"),
        ),
    )

    restored = AgentManifest.from_yaml(manifest.to_yaml())

    assert restored.spec.mcps == ("mcp.weather", "office.core/filesystem")


def test_manifest_round_trips_mcp_wildcard() -> None:
    payload = DEFAULT_MANIFEST.replace(
        "  skills:\n    - '*'",
        "  skills:\n    - '*'\n  mcps:\n    - '*'",
    )

    manifest = AgentManifest.from_yaml(payload)

    assert manifest.spec.mcps == ("*",)
    assert AgentManifest.from_yaml(manifest.to_yaml()).spec.mcps == ("*",)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (DEFAULT_MANIFEST.replace("schema_version: 1", "schema_version: 3"), "schema_version"),
        (
            DEFAULT_MANIFEST.replace("  name: BianbuAgent\n", "  name: BianbuAgent\n  name_en: [Bianbu]\n"),
            "agent.name_en",
        ),
        (
            DEFAULT_MANIFEST.replace(
                "  description: Project-local Bianbu Agent\n",
                "  description: Project-local Bianbu Agent\n  description_zh: [本地]\n",
            ),
            "agent.description_zh",
        ),
        (DEFAULT_MANIFEST.replace("agent:\n", "unsupported: true\nagent:\n"), "top-level"),
        (DEFAULT_MANIFEST.replace("  name: BianbuAgent\n", ""), "agent.name"),
        (
            DEFAULT_MANIFEST.replace("  version: 1.0.0", "  version: '1.0'"),
            "agent.version",
        ),
        (DEFAULT_MANIFEST.replace("  id: bianbu-agent-default", "  id: ../escape"), "agent.id"),
        (DEFAULT_MANIFEST.replace("    - '*'\n  skills:", "    - '*'\n    - clock\n  skills:"), "wildcard"),
        (
            DEFAULT_MANIFEST.replace(
                "  skills:\n    - '*'",
                "  skills:\n    - '*'\n  mcps:\n    - '*'\n    - mcp.weather",
            ),
            "wildcard",
        ),
        (DEFAULT_MANIFEST.replace("  skills:\n    - '*'", "  skills: '*'"), "agent.skills"),
        (DEFAULT_MANIFEST.replace("  model: null", "  model: 42"), "agent.model"),
        (DEFAULT_MANIFEST.replace("  model: null", "  model: model-old"), "agent.model"),
        (DEFAULT_MANIFEST.replace("  metadata:\n", "  extra: true\n  metadata:\n"), "unsupported agent fields"),
    ],
)
def test_manifest_rejects_invalid_schema(payload: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AgentManifest.from_yaml(payload)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("- schema_version\n- agent\n", "mapping"),
        ("schema_version: [\n", "invalid agent YAML"),
        (DEFAULT_MANIFEST.replace("schema_version: 1", "schema_version: true"), "integer"),
        (DEFAULT_MANIFEST.replace("    retries: 2", "    retries: null"), "metadata"),
    ],
)
def test_manifest_rejects_invalid_yaml_values(payload: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        AgentManifest.from_yaml(payload)


def test_manifest_rejects_duplicate_yaml_mapping_keys() -> None:
    payload = DEFAULT_MANIFEST.replace(
        "  id: bianbu-agent-default\n",
        "  id: writer\n  id: bianbu-agent-default\n",
    )

    with pytest.raises(ValueError, match="duplicate key"):
        AgentManifest.from_yaml(payload)


@pytest.mark.parametrize(
    "payload",
    [
        """\
schema_version: 1
agent:
  id: writer
  version: 1.0.0
  name: Writer
  metadata: &metadata
    self: *metadata
""",
        """\
schema_version: 1
agent:
  id: writer
  version: 1.0.0
  name: Writer
  metadata:
    leaf: &leaf
      value: shared
    branch: &branch
      - *leaf
      - *leaf
    fanout:
      - *branch
      - *branch
""",
    ],
    ids=("recursive", "acyclic-fanout"),
)
def test_manifest_rejects_yaml_aliases(payload: str) -> None:
    with pytest.raises(ValueError, match="YAML aliases are not allowed"):
        AgentManifest.from_yaml(payload)


def test_manifest_path_qualifies_yaml_alias_validation_error(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(
        """\
schema_version: 1
agent:
  id: writer
  version: 1.0.0
  name: Writer
  metadata: &metadata
    self: *metadata
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="YAML aliases are not allowed") as exc_info:
        AgentManifest.from_path(path)

    assert str(path) in str(exc_info.value)


def test_manifest_rejects_old_toml_shaped_payload() -> None:
    payload = """\
schema_version = 1
[agent]
id = "writer"
version = "1.0.0"
name = "Writer"
"""

    with pytest.raises(ValueError, match="root must be a mapping"):
        AgentManifest.from_yaml(payload)


@pytest.mark.parametrize(
    "agent_id",
    ["", ".hidden", "-dash", "with/slash", "with\\slash", "x" * 129],
)
def test_validate_agent_id_rejects_unsafe_values(agent_id: str) -> None:
    with pytest.raises(ValueError, match="agent.id"):
        validate_agent_id(agent_id)


def test_manifest_rejects_metadata_that_yaml_cannot_represent() -> None:
    with pytest.raises(ValueError, match="metadata"):
        AgentManifest(
            schema_version=1,
            version="1.0.0",
            spec=AgentSpec(id="writer", name="Writer", metadata={"value": None}),
        )
