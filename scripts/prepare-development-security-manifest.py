#!/usr/bin/env python3
"""Prepare a source-checkout config and self-signed securityd manifest."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import sys
import tempfile

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bianbu_agent.config import load_runtime_config


AGENT_ID = "com.bianbu.runtime.development"
LOCAL_AGENT_ID = "starter-template"


def _load_generator(project_root: Path):
    path = project_root / "debian" / "generate-security-manifest.py"
    spec = importlib.util.spec_from_file_location("generate_security_manifest", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load security manifest generator: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_runtime_config(
    base_path: Path, output_path: Path, project_root: Path
) -> Path:
    base = load_runtime_config(base_path)
    document = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("runtime config must contain a mapping")
    document["data_dir"] = str(base.data_dir)
    security = document.setdefault("security", {})
    security["required"] = True
    runtime = document.setdefault("runtime", {})
    control = runtime.setdefault("control", {})
    control["startup_timeout_seconds"] = max(
        int(control.get("startup_timeout_seconds", 0)), 60
    )
    control["handshake_timeout_seconds"] = max(
        int(control.get("handshake_timeout_seconds", 0)), 60
    )
    worker = runtime.setdefault("worker", {})
    worker["startup_timeout_seconds"] = max(
        int(worker.get("startup_timeout_seconds", 0)), 60
    )
    worker["entrypoint"] = [
        sys.executable,
        "-m",
        "bianbu_agent.cli",
        "--role",
        "agent-worker",
        "--config",
        _worker_config_argument(output_path, Path(str(document["data_dir"]))),
        "worker",
    ]
    payload = yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, output_path)
    return output_path


def _worker_config_argument(runtime_config: Path, data_dir: Path) -> str:
    """Name the Worker config the way the sandbox namespace sees it.

    The Security Runtime binds the Agent data root at ``/home/agent`` and
    rewrites this argument back to the host config before starting a host-mode
    Worker, so a signed manifest must never embed the checkout path.  A config
    that lives outside the data root cannot be named that way; keep its host
    path, exactly as before.
    """
    try:
        relative = runtime_config.relative_to(data_dir)
    except ValueError:
        return str(runtime_config)
    return f"/home/agent/{relative.as_posix()}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--runtime-config-output", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    project_root = args.project_root.expanduser().resolve()
    config_path = args.config.expanduser().resolve()
    runtime_config = _write_runtime_config(
        config_path, args.runtime_config_output.expanduser().resolve(), project_root
    )
    config = load_runtime_config(runtime_config)
    agent_path = project_root / "resources" / "default-profile" / "starter-agent.yaml"
    document = yaml.safe_load(agent_path.read_text(encoding="utf-8"))
    agent = document.get("agent", {}) if isinstance(document, dict) else {}
    if not isinstance(agent, dict) or agent.get("id") != LOCAL_AGENT_ID:
        raise ValueError(f"invalid starter Agent template: {agent_path}")

    generator = _load_generator(project_root)
    model = agent.get("model")
    if not isinstance(model, dict):
        raise ValueError("starter Agent model must be a mapping")
    generator.PACKAGED_MODEL = dict(model)
    permissions = agent.get("permissions", {})
    network = permissions.get("network", {}) if isinstance(permissions, dict) else {}
    domains = network.get("domains", ()) if isinstance(network, dict) else ()
    version = str(agent.get("version", "1.0.0")).strip()
    if len(version) > 1 and version[0] in {"v", "V"} and version[1].isdigit():
        version = version[1:]
    supported_domains = tuple(str(value) for value in domains)
    package = generator.generate(
        agent_source=agent_path,
        config_source=runtime_config,
        output=args.output.expanduser().resolve(),
        private_key=None,
        uid=os.getuid(),
        version=version,
        allowed_domains=supported_domains,
        security_agent_id=AGENT_ID,
        local_agent_id=LOCAL_AGENT_ID,
        entrypoint=tuple(config.runtime.worker.entrypoint),
        development_self_signed=True,
    )
    print(package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
