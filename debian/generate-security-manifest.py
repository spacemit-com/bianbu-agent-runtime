#!/usr/bin/env python3
"""Build a signed Security Runtime manifest for an Agent Worker.

The release private key is supplied by the build/signing environment and is
never copied into the Debian package. Clean source builds may instead request
an explicitly marked development self-signature whose private key exists only
in memory. Such a package must not be mistaken for a production release.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import textwrap

import yaml

LOCAL_AGENT_ID = "starter-template"
AGENT_ID = "com.bianbu.runtime"
# The Runtime package always binds to the Runtime framework's tool catalog.
# securityd records this so the console lists runtime tools, never hermes ones.
FRAMEWORK = "runtime"
PACKAGED_MODEL = {"provider": "custom:bibit", "name": "deepseek-flash"}
ENTRYPOINT = (
    "/usr/bin/bianbu-agent-runtime", "--role", "agent-worker",
    "--config", "/home/agent/config.yaml", "worker",
)
_DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
DEVELOPMENT_MARKER = "DEVELOPMENT-SELF-SIGNED"
DEVELOPMENT_MARKER_TEXT = (
    "Development self-signed Runtime manifest.\n"
    "Not an official release trust anchor.\n"
)


def _validate_agent_id(value: str, field: str) -> str:
    normalized = str(value).strip()
    if _AGENT_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            f"{field} must start with a letter or digit and contain only "
            "letters, digits, '.', '_' or '-'; maximum length is 128"
        )
    return normalized


def _normalise_entrypoint(values: tuple[str, ...]) -> tuple[str, ...]:
    entrypoint = tuple(str(item).strip() for item in values)
    if not entrypoint or any(not item or "\x00" in item for item in entrypoint):
        raise ValueError("entrypoint must be a non-empty argv without NUL bytes")
    return entrypoint


def _canonical_agent_bytes(
    source: Path,
    local_agent_id: str = LOCAL_AGENT_ID,
    security_agent_id: str = AGENT_ID,
) -> bytes:
    """Match provision-user._initial_agent output, including safe_dump order."""
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("agent"), dict):
        raise ValueError("starter Agent YAML must contain an agent mapping")
    expected_id = _validate_agent_id(local_agent_id, "local_agent_id")
    if str(document["agent"].get("id", "")).strip() != expected_id:
        raise ValueError(f"Agent YAML id must be {expected_id}")
    # The Debian package renderer replaces the starter template's model with
    # the offline packaged provider/model.
    if expected_id == LOCAL_AGENT_ID:
        document["agent"]["model"] = dict(PACKAGED_MODEL)
    declared_metadata = document["agent"].get("metadata", {})
    declared_security_id = (
        declared_metadata.get("security_agent_id")
        if isinstance(declared_metadata, dict) else None
    )
    # The starter template obtains its Security identity from the Runtime
    # environment. Every independent Agent must carry an explicit identity.
    if expected_id != LOCAL_AGENT_ID:
        if str(declared_security_id or "").strip() != security_agent_id:
            raise ValueError(
                "independent Agent YAML metadata.security_agent_id must match "
                f"security_agent_id {security_agent_id!r}"
            )
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode("utf-8")


def _stage_agent_package(
    source: Path,
    staging: Path,
    local_agent_id: str,
    security_agent_id: str,
) -> None:
    """Copy the complete Agent package using Runtime's safe file contract."""
    source = source.expanduser()
    if source.is_symlink():
        raise ValueError(f"symbolic links are not allowed: {source}")
    source = source.resolve()
    if source.is_file() and source.name != "agent.yaml":
        if local_agent_id != LOCAL_AGENT_ID:
            raise ValueError("only the starter Agent may use a standalone template")
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "agent.yaml").write_bytes(
            _canonical_agent_bytes(source, local_agent_id, security_agent_id)
        )
        return
    source_root = source.parent if source.is_file() else source
    if not source_root.is_dir():
        raise ValueError(f"Agent package source is not a directory: {source_root}")
    if source_root.name != local_agent_id:
        raise ValueError("Agent package directory name must match local_agent_id")
    manifest_source = source_root / "agent.yaml"
    if not manifest_source.is_file() or manifest_source.is_symlink():
        raise ValueError(f"Agent package is missing a regular agent.yaml: {manifest_source}")
    staging.mkdir(parents=True, exist_ok=True)
    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {path}")
        if path.is_dir():
            continue
        relative = path.relative_to(source_root)
        if "__pycache__" in relative.parts:
            continue
        if not path.is_file():
            raise ValueError(f"unsupported Agent package entry: {path}")
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    # Canonicalization must match provision-user and AgentPackageManager.
    (staging / "agent.yaml").write_bytes(
        _canonical_agent_bytes(manifest_source, local_agent_id, security_agent_id)
    )


def _load_private_key(path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = path.read_bytes()
    if b"BEGIN" in raw:
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("private key must be Ed25519")
        return key
    compact = b"".join(raw.split())
    try:
        seed = bytes.fromhex(compact.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("private key must be PEM or raw hex") from exc
    if len(seed) not in {32, 64}:
        raise ValueError("raw Ed25519 private key must be 32 or 64 bytes")
    return Ed25519PrivateKey.from_private_bytes(seed[:32])


def _public_hex(key) -> str:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _normalise_allowed_domains(values: tuple[str, ...]) -> tuple[str, ...]:
    domains: list[str] = []
    for value in values:
        domain = str(value).strip().lower().rstrip(".")
        labels = domain.split(".")
        if (not domain or len(domain) > 253 or
                (len(labels) < 2 and domain != "localhost") or
                any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)):
            raise ValueError(f"allowed domain must be a DNS hostname: {value!r}")
        if domain not in domains:
            domains.append(domain)
    return tuple(domains)


def _decode_hex_file(path: Path, *, expected_bytes: int) -> bytes:
    if not path.is_file():
        raise ValueError(f"required manifest file does not exist: {path}")
    try:
        value = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"invalid hex in manifest file: {path}") from exc
    if len(value) != expected_bytes:
        raise ValueError(
            f"manifest file must contain {expected_bytes} bytes: {path}"
        )
    return value


def _manifest_body(version: str,
                   allowed_domains: tuple[str, ...] = (), *,
                   network_mode: str = "allow_all",
                   agent_id: str = AGENT_ID,
                   name: str | None = None,
                   entrypoint: tuple[str, ...] = ENTRYPOINT) -> str:
    # Capability management has exactly one dimension: the tool.  Every entry
    # below is one ``allow / ask / deny`` policy for one runtime tool, in the
    # same order as securityd's authoritative ``_RUNTIME_TOOLS`` catalog.  The
    # catalog moved from four dimensions (domain / risk level / confirm
    # strategy) to the tool alone, and securityd now rejects any manifest that
    # still speaks the old vocabulary, so this table must track that module.
    # The default is ``allow``: the shipped manifest only lists the tools so a
    # clean install is visible in the WebUI; operators tighten individual
    # tools to ``ask`` or ``deny`` when they need a confirmation gate.
    permissions = """\
read_file = "allow"
read_image = "allow"
search_files = "allow"
write_file = "allow"
patch = "allow"
terminal = "allow"
process = "allow"
web_search = "allow"
web_extract = "allow"
browser_navigate = "allow"
browser_click = "allow"
browser_type = "allow"
browser_press = "allow"
browser_scroll = "allow"
browser_back = "allow"
browser_snapshot = "allow"
browser_get_images = "allow"
browser_console = "allow"
skill_view = "allow"
skills_list = "allow"
skill_manage = "allow"
todo = "allow"
kanban_list = "allow"
kanban_show = "allow"
kanban_attachments = "allow"
kanban_create = "allow"
kanban_attach = "allow"
kanban_attach_url = "allow"
kanban_comment = "allow"
kanban_complete = "allow"
kanban_block = "allow"
kanban_unblock = "allow"
kanban_link = "allow"
kanban_heartbeat = "allow"
"""
    security_agent_id = _validate_agent_id(agent_id, "security_agent_id")
    runtime_entrypoint = _normalise_entrypoint(entrypoint)
    default_name = (
        "Bianbu Agent Default Runtime"
        if security_agent_id == AGENT_ID else
        f"Bianbu Agent Runtime ({security_agent_id})"
    )
    display_name = str(name or default_name).strip()
    if not display_name:
        raise ValueError("name must not be empty")
    network_mode = str(network_mode).strip().lower()
    if network_mode not in {"whitelist", "deny_all", "allow_all", "learning"}:
        raise ValueError(f"unsupported network mode: {network_mode}")
    domains = _normalise_allowed_domains(allowed_domains)
    encoded = ", ".join(json.dumps(value) for value in domains)
    network = textwrap.dedent(f'''\

            [permissions.network]
            mode = {json.dumps(network_mode)}
            allowed_domains = [{encoded}]
            ''')
    return textwrap.dedent(f'''\
        [agent]
        id = {json.dumps(security_agent_id)}
        version = "{version}"
        name = {json.dumps(display_name)}
        signature = "ed25519:PLACEHOLDER"
        framework = {json.dumps(FRAMEWORK)}

        [permissions]
        {permissions.rstrip()}
        {network.rstrip()}
        [runtime]
        entrypoint = [{', '.join(json.dumps(item) for item in runtime_entrypoint)}]
        ''')


def generate(*, agent_source: Path, config_source: Path, output: Path,
             private_key: Path | None, uid: int, version: str,
             allowed_domains: tuple[str, ...] = (), network_mode: str = "allow_all",
             security_agent_id: str = AGENT_ID,
             local_agent_id: str = LOCAL_AGENT_ID,
             entrypoint: tuple[str, ...] = ENTRYPOINT,
             name: str | None = None,
             development_self_signed: bool = False) -> Path:
    if uid < 0:
        raise ValueError("uid must be non-negative")
    if private_key is None and not development_self_signed:
        raise ValueError(
            "choose exactly one signing mode: private_key or "
            "development_self_signed"
        )
    if private_key is not None and development_self_signed:
        raise ValueError(
            "choose exactly one signing mode: private_key or "
            "development_self_signed"
        )
    security_id = _validate_agent_id(security_agent_id, "security_agent_id")
    local_id = _validate_agent_id(local_agent_id, "local_agent_id")
    staging = output / "agent"
    if staging.exists() or staging.is_symlink():
        if staging.is_symlink():
            raise ValueError(f"staging path must not be a symbolic link: {staging}")
        shutil.rmtree(staging)
    _stage_agent_package(agent_source, staging, local_id, security_id)
    if development_self_signed:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        key = Ed25519PrivateKey.generate()
    else:
        assert private_key is not None
        if not private_key.is_file():
            raise ValueError(f"private key does not exist: {private_key}")
        key = _load_private_key(private_key)
    body = _manifest_body(
        version, allowed_domains,
        network_mode=network_mode,
        agent_id=security_id, name=name, entrypoint=entrypoint,
    )
    payload = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("signature")
    ).encode("utf-8")
    signature = key.sign(payload).hex()
    package = output / security_id
    package.mkdir(parents=True, exist_ok=True)
    (package / "manifest.toml").write_text(body, encoding="utf-8")
    (package / "manifest.toml.sig").write_text(signature + "\n", encoding="ascii")
    (package / "pubkey.hex").write_text(_public_hex(key) + "\n", encoding="ascii")
    marker = output / DEVELOPMENT_MARKER
    if development_self_signed:
        marker.write_text(DEVELOPMENT_MARKER_TEXT, encoding="utf-8")
    else:
        marker.unlink(missing_ok=True)
    return package


def validate_existing(*, agent_source: Path, config_source: Path,
                      input_dir: Path, uid: int, version: str,
                      allowed_domains: tuple[str, ...] = (),
                      network_mode: str = "allow_all") -> Path:
    """Verify that a signed manifest is valid and bound to this source tree."""
    if uid < 0:
        raise ValueError("uid must be non-negative")
    _canonical_agent_bytes(agent_source)
    expected = _manifest_body(
        version,
        allowed_domains=allowed_domains,
        network_mode=network_mode,
    )
    package = input_dir / AGENT_ID
    manifest = package / "manifest.toml"
    if not manifest.is_file():
        raise ValueError(f"required manifest file does not exist: {manifest}")
    actual = manifest.read_text(encoding="utf-8")
    if actual != expected:
        raise ValueError("manifest does not match the current source, config, and version")

    signature = _decode_hex_file(package / "manifest.toml.sig", expected_bytes=64)
    public_key = _decode_hex_file(package / "pubkey.hex", expected_bytes=32)
    payload = "\n".join(
        line for line in actual.splitlines()
        if not line.strip().startswith("signature")
    ).encode("utf-8")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, payload)
    except (InvalidSignature, ValueError) as exc:
        raise ValueError("manifest signature verification failed") from exc
    return package


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-source", type=Path, required=True)
    parser.add_argument("--config-source", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    signing = parser.add_mutually_exclusive_group(required=True)
    signing.add_argument("--private-key", type=Path)
    signing.add_argument("--development-self-signed", action="store_true")
    signing.add_argument("--validate-existing", type=Path)
    parser.add_argument("--uid", type=int, default=1000)
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument(
        "--security-agent-id", default=AGENT_ID,
        help=f"Security Runtime 身份（默认：{AGENT_ID}）",
    )
    parser.add_argument(
        "--local-agent-id", default=LOCAL_AGENT_ID,
        help=f"agent.yaml 中的模板 ID（默认：{LOCAL_AGENT_ID}）",
    )
    parser.add_argument(
        "--name", default=None,
        help="Security manifest 的显示名称",
    )
    parser.add_argument(
        "--entrypoint", nargs="+", default=list(ENTRYPOINT),
        help="Worker 固定 argv；必须与 Runtime 实际启动命令一致",
    )
    parser.add_argument(
        "--entrypoint-json", default=None,
        help="JSON argv 数组；用于包含以 '-' 开头参数的固定 Worker 命令",
    )
    parser.add_argument(
        "--allowed-domain", action="append", default=[],
        help="允许 Agent Worker 访问的模型/API 域名；可重复指定",
    )
    parser.add_argument(
        "--network-mode", default="allow_all",
        choices=("whitelist", "deny_all", "allow_all", "learning"),
        help="Agent 初始网络策略（默认 allow_all）",
    )
    args = parser.parse_args()
    if args.validate_existing is not None:
        package = validate_existing(
            agent_source=args.agent_source,
            config_source=args.config_source,
            input_dir=args.validate_existing,
            uid=args.uid,
            version=args.version,
            allowed_domains=tuple(args.allowed_domain),
            network_mode=args.network_mode,
        )
    else:
        if args.output is None:
            parser.error("--output is required when generating a manifest")
        entrypoint = args.entrypoint
        if args.entrypoint_json is not None:
            entrypoint = json.loads(args.entrypoint_json)
            if not isinstance(entrypoint, list):
                parser.error("--entrypoint-json must contain a JSON array")
        package = generate(
            agent_source=args.agent_source, config_source=args.config_source,
            output=args.output, private_key=args.private_key, uid=args.uid,
            version=args.version,
            allowed_domains=tuple(args.allowed_domain),
            security_agent_id=args.security_agent_id,
            local_agent_id=args.local_agent_id,
            entrypoint=tuple(entrypoint),
            name=args.name,
            development_self_signed=args.development_self_signed,
            network_mode=args.network_mode,
        )
    print(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
