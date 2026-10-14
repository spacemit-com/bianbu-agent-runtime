#!/usr/bin/python3.14
"""Register a signed Agent Runtime manifest with securityd.

This helper is a privileged package boundary, not a Worker mechanism. It is
called during package installation and by systemd pre-start after a Security reset;
the private signing key is never present on the target device.  A missing
manifest or a failed InstallPackage is fatal so a package cannot appear
installed while its Worker entrypoint is untrusted.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def _find_client_dir(candidates: list[str] | None = None) -> str:
    if candidates is None:
        configured = os.environ.get("BIANBU_SECURITY_CLIENT_DIR", "").strip()
        candidates = [configured] if configured else []
        # ``bianbu-agent-securityd`` is the current package name. Keep the
        # historical path as a fallback for in-place upgrades from 0.1.x.
        candidates.extend((
            "/usr/lib/bianbu-agent-securityd/client",
            "/usr/lib/bianbu-securityd/client",
        ))
    client_dir = next(
        (path for path in candidates
         if path and Path(path, "bianbu_security_client.py").is_file()),
        None,
    )
    if client_dir is None:
        raise RuntimeError("Security client module not found in installed paths")
    return client_dir


def _client():
    client_dir = _find_client_dir()
    if client_dir not in sys.path:
        sys.path.insert(0, client_dir)
    try:
        import bianbu_security_client as value  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "bianbu_security_client is not installed; cannot register Runtime manifest"
        ) from exc
    return value


def register(package: Path, *, expected_agent_id: str | None = None) -> dict[str, object]:
    package = package.expanduser().resolve()
    manifest = package / "manifest.toml"
    signature = package / "manifest.toml.sig"
    public_key = package / "pubkey.hex"
    if not package.is_dir() or not manifest.is_file() or not signature.is_file() or not public_key.is_file():
        raise RuntimeError(
            f"signed Runtime manifest package is incomplete: {package}"
        )
    result = dict(_client().install_package(str(package)))
    if result.get("ok") is not True:
        raise RuntimeError(
            f"securityd rejected the Runtime manifest: "
            f"{result.get('reason', 'unknown error')}"
        )
    registered_agent_id = str(result.get("agent_id", ""))
    if expected_agent_id is not None and registered_agent_id != expected_agent_id:
        raise RuntimeError(
            f"manifest registered unexpected agent id: {registered_agent_id!r}"
        )
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--optional",
        action="store_true",
        help="defer registration when the Security Runtime is unavailable",
    )
    parser.add_argument("--if-missing", action="store_true",
                        help="restore a missing registration without overwriting existing policy")
    parser.add_argument("package", type=Path)
    parser.add_argument(
        "--expected-agent-id",
        default=None,
        help="校验 securityd 返回的 Agent ID；不指定时使用 manifest 中的 ID",
    )
    args = parser.parse_args(argv)
    if args.if_missing and not args.expected_agent_id:
        parser.error("--if-missing requires --expected-agent-id")
    try:
        if args.if_missing:
            info = _client().get_agent_info(args.expected_agent_id)
            if not isinstance(info, dict) or "manifest" not in info:
                raise RuntimeError("invalid Security Agent info response")
            existing = info["manifest"]
            if existing is not None:
                if not isinstance(existing, dict) or existing.get("status") != "active":
                    raise RuntimeError(f"existing Runtime manifest is not active: {existing}")
                print(f"Security Runtime manifest already registered: agent={args.expected_agent_id}")
                return 0
        result = register(args.package, expected_agent_id=args.expected_agent_id)
    except Exception as exc:
        if not args.optional:
            raise
        print(
            f"warning: Security Runtime registration deferred: {exc}",
            file=sys.stderr,
        )
        return 0
    print(
        f"registered Security Runtime manifest: agent={result['agent_id']} "
        f"permissions={result.get('count', 0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
