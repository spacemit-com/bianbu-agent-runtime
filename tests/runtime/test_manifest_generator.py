from __future__ import annotations

import importlib.util
from pathlib import Path
import tomllib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def _load_generator():
    path = Path(__file__).parents[2] / "debian" / "generate-security-manifest.py"
    spec = importlib.util.spec_from_file_location("runtime_manifest_generator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_is_importable_without_importing_runtime_package(monkeypatch, tmp_path: Path):
    module = _load_generator()
    assert "bianbu_agent" not in module.__dict__


def test_generator_output_has_entrypoint_only_runtime_binding(tmp_path: Path):
    module = _load_generator()
    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.hex"
    key_path.write_text(private_key.private_bytes_raw().hex(), encoding="ascii")
    agent_source = Path(__file__).parents[2] / "resources/default-profile/starter-agent.yaml"
    package = module.generate(
        agent_source=agent_source,
        config_source=Path(__file__).parents[2] / "resources/default-profile/config.yaml",
        output=tmp_path / "out",
        private_key=key_path,
        uid=1000,
        version="9.9.9",
        allowed_domains=("api.example.com",),
    )
    assert (package / "manifest.toml").is_file()
    assert (package / "manifest.toml.sig").is_file()
    assert (package / "pubkey.hex").is_file()
    text = (package / "manifest.toml").read_text(encoding="utf-8")
    assert 'entrypoint = ["/usr/bin/bianbu-agent-runtime"' in text
    assert "package_digest" not in text
    assert "config_digest" not in text
    assert '[permissions.network]' in text
    assert 'mode = "allow_all"' in text
    assert 'allowed_domains = ["api.example.com"]' in text
    # Capability management is one dimension: the tool.  The manifest must
    # speak that vocabulary, because securityd rejects the retired four-
    # dimensional form (domain / risk level / confirm strategy) outright.
    assert 'read_file = "allow"' in text
    # 默认策略是 allow；需要审批的工具由使用者在控制台逐项收紧。
    assert 'terminal = "allow"' in text
    assert 'kanban_heartbeat = "allow"' in text
    assert 'framework = "runtime"' in text
    assert "risk_level" not in text
    assert "token_budget" not in text
    manifest = tomllib.loads(text)
    permissions = manifest["permissions"]
    tools = {name: value for name, value in permissions.items() if name != "network"}
    # Every entry is a three-state tool policy, and the table covers the whole
    # runtime catalog so a clean install never falls back to stale rows.
    assert set(tools.values()) <= {"allow", "ask", "deny"}
    assert len(tools) == 34
    # The signed package declares its framework so securityd scopes the
    # console catalog to the runtime tools only.
    assert manifest["agent"]["framework"] == "runtime"
    assert {"read_file", "terminal", "write_file", "kanban_heartbeat"} <= set(tools)


def test_generator_rejects_endpoint_instead_of_domain(tmp_path: Path):
    module = _load_generator()
    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.hex"
    key_path.write_text(private_key.private_bytes_raw().hex(), encoding="ascii")
    agent_source = Path(__file__).parents[2] / "resources/default-profile/starter-agent.yaml"
    import pytest
    with pytest.raises(ValueError, match="allowed domain"):
        module.generate(
            agent_source=agent_source,
            config_source=Path(__file__).parents[2] / "resources/default-profile/config.yaml",
            output=tmp_path / "out",
            private_key=key_path,
            uid=1000,
            version="9.9.9",
            allowed_domains=("https://api.example.com/v1",),
        )


def test_generator_supports_independent_agent_identity_and_entrypoint(tmp_path: Path):
    module = _load_generator()
    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.hex"
    key_path.write_text(private_key.private_bytes_raw().hex(), encoding="ascii")
    source = tmp_path / "vendor-research"
    source.mkdir()
    (source / "agent.yaml").write_text(
        """schema_version: 1
agent:
  id: vendor-research
  version: 1.2.3
  name: Vendor Research
  description: Research agent
  soul: Work safely
  model: null
  tools: []
  skills: []
  mcps: []
  hooks: []
  metadata:
    security_agent_id: com.vendor.research
""",
        encoding="utf-8",
    )
    (source / "avatar.png").write_bytes(b"not-an-image-but-a-stable-asset")
    package = module.generate(
        agent_source=source / "agent.yaml",
        config_source=Path(__file__).parents[2] / "resources/default-profile/config.yaml",
        output=tmp_path / "out",
        private_key=key_path,
        uid=1000,
        version="2.0.0",
        security_agent_id="com.vendor.research",
        local_agent_id="vendor-research",
        name="Vendor Research Runtime",
        entrypoint=("/usr/bin/bianbu-agent-runtime", "--role", "agent-worker", "worker"),
    )
    assert package.name == "com.vendor.research"
    text = (package / "manifest.toml").read_text(encoding="utf-8")
    assert 'id = "com.vendor.research"' in text
    assert 'name = "Vendor Research Runtime"' in text
    assert 'entrypoint = ["/usr/bin/bianbu-agent-runtime", "--role", "agent-worker", "worker"]' in text
    manifest = tomllib.loads(text)
    assert set(manifest["runtime"]) == {"entrypoint"}


def test_generator_rejects_independent_identity_without_agent_binding(tmp_path: Path):
    module = _load_generator()
    private_key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "private.hex"
    key_path.write_text(private_key.private_bytes_raw().hex(), encoding="ascii")
    source = tmp_path / "vendor-research"
    source.mkdir()
    (source / "agent.yaml").write_text(
        """schema_version: 1
agent:
  id: vendor-research
  version: 1.0.0
  name: Vendor Research
""",
        encoding="utf-8",
    )
    import pytest
    with pytest.raises(ValueError, match="security_agent_id"):
        module.generate(
            agent_source=source / "agent.yaml",
            config_source=Path(__file__).parents[2] / "resources/default-profile/config.yaml",
            output=tmp_path / "out",
            private_key=key_path,
            uid=1000,
            version="1.0.0",
            security_agent_id="com.vendor.research",
            local_agent_id="vendor-research",
        )


def test_generator_creates_marked_development_signature_without_private_key_file(
    tmp_path: Path,
):
    module = _load_generator()
    output = tmp_path / "out"

    package = module.generate(
        agent_source=(
            Path(__file__).parents[2]
            / "resources/default-profile/starter-agent.yaml"
        ),
        config_source=Path(__file__).parents[2] / "resources/default-profile/config.yaml",
        output=output,
        private_key=None,
        development_self_signed=True,
        uid=1000,
        version="9.9.9",
    )

    marker = output / "DEVELOPMENT-SELF-SIGNED"
    assert marker.read_text(encoding="utf-8") == (
        "Development self-signed Runtime manifest.\n"
        "Not an official release trust anchor.\n"
    )
    assert not tuple(output.rglob("*.pem"))
    assert not tuple(output.rglob("*private*"))

    body = (package / "manifest.toml").read_text(encoding="utf-8")
    payload = "\n".join(
        line for line in body.splitlines()
        if not line.strip().startswith("signature")
    ).encode("utf-8")
    signature = bytes.fromhex(
        (package / "manifest.toml.sig").read_text(encoding="ascii").strip()
    )
    public_key = bytes.fromhex(
        (package / "pubkey.hex").read_text(encoding="ascii").strip()
    )
    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)


def test_existing_manifest_validation_rejects_tampering_and_source_mismatch(
    tmp_path: Path,
):
    module = _load_generator()
    agent_source = (
        Path(__file__).parents[2]
        / "resources/default-profile/starter-agent.yaml"
    )
    config_source = Path(__file__).parents[2] / "resources/default-profile/config.yaml"
    output = tmp_path / "out"
    package = module.generate(
        agent_source=agent_source,
        config_source=config_source,
        output=output,
        private_key=None,
        development_self_signed=True,
        uid=1000,
        version="9.9.9",
    )

    assert module.validate_existing(
        agent_source=agent_source,
        config_source=config_source,
        input_dir=output,
        uid=1000,
        version="9.9.9",
    ) == package

    with pytest.raises(ValueError, match="does not match"):
        module.validate_existing(
            agent_source=agent_source,
            config_source=config_source,
            input_dir=output,
            uid=1000,
            version="9.9.10",
        )

    signature = package / "manifest.toml.sig"
    signature.write_text("00" * 64 + "\n", encoding="ascii")
    with pytest.raises(ValueError, match="signature verification failed"):
        module.validate_existing(
            agent_source=agent_source,
            config_source=config_source,
            input_dir=output,
            uid=1000,
            version="9.9.9",
        )
