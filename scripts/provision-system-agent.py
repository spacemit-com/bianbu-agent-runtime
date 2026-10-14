#!/usr/bin/env python3
"""Privileged publisher used by package startup and explicit administrator updates."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import pwd

from bianbu_agent.provisioning import publish_system_starter, system_starter_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--agent-id")
    parser.add_argument("--template", type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError("system Agent publication requires administrator permissions")
    if args.user:
        if args.data_dir or args.agent_id:
            parser.error("--user cannot be combined with --data-dir or --agent-id")
        account = pwd.getpwnam(args.user)
        data_dir = Path(account.pw_dir).resolve() / ".bianbu-agent"
        agent_id = system_starter_id(data_dir)
        if not agent_id:
            descriptor = os.open(data_dir / ".managed-profile.json", os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                document = json.loads(stream.read(65536))
            agent_id = str(document.get("starter_agent_id") or "")
    else:
        if not args.data_dir or not args.agent_id:
            parser.error("--data-dir and --agent-id are required without --user")
        data_dir, agent_id = args.data_dir, args.agent_id
    print(publish_system_starter(data_dir, args.template, agent_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
