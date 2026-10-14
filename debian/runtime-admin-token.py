#!/usr/bin/python3.14
"""Provision and inspect the Runtime management API credential.

The system Runtime is deliberately kept in production mode.  This helper is
used by the package maintainer script to create a credential on first install
and by operators to retrieve or rotate it without putting the secret in the
Runtime YAML, a unit file, or a journal message.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile


DEFAULT_ENV = Path("/etc/bianbu-agent/runtime.env")
DEFAULT_TOKEN_FILE = Path("/etc/bianbu-agent/runtime-admin-token")
TOKEN_BYTES = 32
_DIRECT_KEY = "BIANBU_RUNTIME_ADMIN_TOKEN"
_FILE_KEY = "BIANBU_RUNTIME_ADMIN_TOKEN_FILE"


def _parse_env(path: Path) -> tuple[list[str], str, str]:
    """Read the two credential keys without evaluating arbitrary shell code."""

    if not path.is_file():
        return [], "", ""
    lines = path.read_text(encoding="utf-8").splitlines()
    direct = ""
    file_path = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key == _DIRECT_KEY and value:
            direct = value
        elif key == _FILE_KEY and value:
            file_path = value
    return lines, direct, file_path


def _set_mode_owner(path: Path, mode: int, group: str | None = None) -> None:
    os.chmod(path, mode)
    if group:
        try:
            import grp

            gid = grp.getgrnam(group).gr_gid
        except (KeyError, ImportError):
            # Source-tree tests do not necessarily have the package group;
            # the maintainer script calls this after creating it.
            gid = None
        if gid is not None:
            os.chown(path, 0, gid)


def _atomic_write(path: Path, content: str, mode: int = 0o640, group: str | None = None) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    temporary = Path(name)
    try:
        os.fchmod(fd, mode)
        if group:
            try:
                import grp

                os.fchown(fd, 0, grp.getgrnam(group).gr_gid)
            except (KeyError, ImportError):
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _set_mode_owner(path, mode, group)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _credential_path(configured: str, env_path: Path) -> Path:
    path = Path(configured).expanduser()
    if not path.is_absolute():
        raise RuntimeError(
            "BIANBU_RUNTIME_ADMIN_TOKEN_FILE must be an absolute path"
        )
    return path


def _read_credential(env_path: Path) -> str:
    _lines, direct, configured_file = _parse_env(env_path)
    if direct:
        return direct
    if configured_file:
        path = _credential_path(configured_file, env_path)
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return ""
    return ""


def ensure_credentials(
    env_path: Path = DEFAULT_ENV,
    token_path: Path = DEFAULT_TOKEN_FILE,
    *,
    group: str | None = "bianbu-runtime",
) -> str:
    """Ensure a usable credential exists, preserving an operator override."""

    lines, direct, configured_file = _parse_env(env_path)
    if direct:
        _set_mode_owner(env_path, 0o640, group)
        return direct
    if configured_file:
        path = _credential_path(configured_file, env_path)
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            existing = ""
        if existing:
            _set_mode_owner(env_path, 0o640, group)
            _set_mode_owner(path, 0o640, group)
            return existing
        raise RuntimeError(f"configured Runtime admin token file is empty: {path}")

    try:
        existing = token_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        existing = ""
    if not existing:
        existing = secrets.token_hex(TOKEN_BYTES)
        _atomic_write(token_path, existing + "\n", 0o640, group)
    else:
        _set_mode_owner(token_path, 0o640, group)

    # Keep the env file credential-free; the Runtime user reads the protected
    # file through BIANBU_RUNTIME_ADMIN_TOKEN_FILE.
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"{_FILE_KEY}={token_path}")
    _atomic_write(env_path, "\n".join(lines).rstrip() + "\n", 0o640, group)
    return existing


def rotate_credentials(
    env_path: Path = DEFAULT_ENV,
    token_path: Path = DEFAULT_TOKEN_FILE,
    *,
    group: str | None = "bianbu-runtime",
) -> str:
    """Replace the configured credential while retaining the env contract."""

    lines, direct, configured_file = _parse_env(env_path)
    token = secrets.token_hex(TOKEN_BYTES)

    if direct:
        replaced = False
        updated: list[str] = []
        for line in lines:
            if line.strip().startswith(f"{_DIRECT_KEY}="):
                updated.append(f"{_DIRECT_KEY}={token}")
                replaced = True
            else:
                updated.append(line)
        if not replaced:
            updated.append(f"{_DIRECT_KEY}={token}")
        _atomic_write(env_path, "\n".join(updated).rstrip() + "\n", 0o640, group)
    elif configured_file:
        target = _credential_path(configured_file, env_path)
        _atomic_write(target, token + "\n", 0o640, group)
        _set_mode_owner(env_path, 0o640, group)
    else:
        _atomic_write(token_path, token + "\n", 0o640, group)
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{_FILE_KEY}={token_path}")
        _atomic_write(env_path, "\n".join(lines).rstrip() + "\n", 0o640, group)
    return token


def _restart_runtime() -> None:
    for unit in (
        "bianbu-agent-runtime.service",
        "bianbu-agent-runtime@*.service",
    ):
        try:
            subprocess.run(
                ["systemctl", "try-restart", unit],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bianbu-agent-runtime-token")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--ensure", action="store_true", help=argparse.SUPPRESS)
    actions.add_argument("--get-token", action="store_true", help="输出当前 Runtime 管理 Token")
    actions.add_argument("--rotate", action="store_true", help="生成新 Token 并重启 Runtime")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV, help=argparse.SUPPRESS)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.ensure:
            ensure_credentials(args.env, args.token_file)
            return 0
        if args.get_token:
            token = _read_credential(args.env)
            if not token:
                print("Runtime 管理 Token 未配置", file=sys.stderr)
                return 1
            print(token)
            return 0
        token = rotate_credentials(args.env, args.token_file)
        if not args.no_restart:
            _restart_runtime()
        print(token)
        return 0
    except (OSError, RuntimeError, UnicodeError) as exc:
        print(f"bianbu-agent-runtime-token: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
