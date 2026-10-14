#!/usr/bin/python3.14
"""Reset the persisted Security caller binding during a privileged upgrade."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    if os.geteuid() != 0:
        print("reset-security-binding.py must run as root", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser()
    parser.add_argument("agent_id")
    parser.add_argument("reason", nargs="?", default="package-upgrade")
    args = parser.parse_args(argv)
    # Securityd's package name changed from the historical
    # ``bianbu-securityd`` to ``bianbu-agent-securityd``.  Keep the old path
    # as a compatibility fallback so upgrades work across either generation;
    # the environment override is useful for source/development installs.
    candidates = []
    configured = os.environ.get("BIANBU_SECURITY_CLIENT_DIR", "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.extend((
        Path("/usr/lib/bianbu-agent-securityd/client"),
        Path("/usr/lib/bianbu-securityd/client"),
    ))
    client_dir = next((path for path in candidates if (path / "bianbu_security_client.py").is_file()), None)
    if client_dir is None:
        print("Security client module not found in installed paths", file=sys.stderr)
        return 1
    if str(client_dir) not in sys.path:
        sys.path.insert(0, str(client_dir))
    try:
        from bianbu_security_client import reset_agent_binding
        removed = reset_agent_binding(args.agent_id, args.reason)
    except Exception as exc:  # noqa: BLE001 - installer must report the boundary
        print(f"Security binding reset failed: {exc}", file=sys.stderr)
        return 1
    print(f"Security binding reset: agent={args.agent_id} removed={bool(removed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
