#!/usr/bin/env python3
"""Privileged publisher used by package startup and explicit administrator updates."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import pwd
import tempfile
import yaml

from bianbu_agent.config import load_runtime_config
from bianbu_agent.provisioning import publish_system_starter, system_starter_id


def _privileged() -> bool:
    return os.geteuid() == 0


def _strip_retired_runtime_keys(path: Path) -> None:
    """Migrate a profile before any reader sees it.

    The unit provisions the user profile, publishes the system starter and
    provisions again.  This publisher is the middle step and it reads the same
    config, so an upgraded profile that still carries retired keys would fail
    here even though the surrounding provisions migrate it.
    """

    if path.is_symlink() or not path.is_file():
        return
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return
    if not isinstance(document, dict):
        return
    runtime = document.get("runtime")
    if not isinstance(runtime, dict):
        return
    changed = False
    for scope in ("control", "worker"):
        section = runtime.get(scope)
        if not isinstance(section, dict):
            continue
        removed = False
        for key in ("execution", "startup_timeout_seconds"):
            if key in section:
                del section[key]
                removed = True
        if removed:
            changed = True
            if not section:
                del runtime[scope]
    if not changed:
        return
    payload = yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    staging = path.with_name(f".{path.name}.migrate-{os.getpid()}")
    descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _render_template(template: Path) -> Path:
    """Render the packaged template through the build/runtime renderer."""

    candidates = (
        template.parents[2] / "debian" / "render-starter-agent.py",
        Path("/usr/lib/bianbu-agent-runtime/render-starter-agent.py"),
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        spec = importlib.util.spec_from_file_location("render_starter_agent", candidate)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # Render next to the template: the Expert's private skills/mcps/plugins
        # trees live in that directory, and publishing has to see them.
        descriptor, name = tempfile.mkstemp(
            prefix=".rendered-", suffix=".yaml", dir=template.parent
        )
        os.close(descriptor)
        rendered = Path(name)
        module.render_starter_agent(template, rendered)
        return rendered
    return template


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--agent-id")
    parser.add_argument("--template", type=Path, required=True)
    args = parser.parse_args()
    if not _privileged():
        raise PermissionError("system Agent publication requires administrator permissions")
    if args.user:
        if args.data_dir or args.agent_id:
            parser.error("--user cannot be combined with --data-dir or --agent-id")
        account = pwd.getpwnam(args.user)
        data_dir = Path(account.pw_dir).resolve() / ".bianbu-agent"
        template = Path(args.template)
        _strip_retired_runtime_keys(data_dir / "config.yaml")
        configured = load_runtime_config(template.parents[2] / "config.yaml").initial_agent
        if template.parent.name != configured:
            template = template.parents[2] / "agents" / configured / "agent.yaml"
        agent_id = system_starter_id(data_dir)
        if agent_id is not None:
            try:
                deployed = load_runtime_config(data_dir / "config.yaml").initial_agent
            except (OSError, ValueError):
                deployed = ""
            if deployed != configured:
                # The user profile still names an older packaged default. Keep
                # its ID so existing sessions stay bound, but publish the
                # current template under the ID provision-user will adopt.
                agent_id = None
        if agent_id is None:
            agent_id = configured
        rendered = _render_template(template)
        try:
            print(publish_system_starter(data_dir, rendered, agent_id))
        finally:
            if rendered != template:
                rendered.unlink(missing_ok=True)
        return 0
    else:
        if not args.data_dir or not args.agent_id:
            parser.error("--data-dir and --agent-id are required without --user")
        data_dir, agent_id = args.data_dir, args.agent_id
    print(publish_system_starter(data_dir, args.template, agent_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
