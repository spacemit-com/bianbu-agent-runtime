"""Standalone Hook JSON transport; stdout is reserved for the response."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, redirect_stdout
import json
import os
import socket
import signal
import sys


@contextmanager
def hook_memory_registration():
    """Hold the Control-attested registration for this command's lifetime.

    SO_PEERCRED supplies host identity even inside a PID namespace. No identity
    fields or commands are sent by the plugin. Unmanaged development skips it.
    """
    path = os.environ.get("BIANBU_MEMORY_REGISTRATION_SOCKET")
    if not path:
        yield
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        try:
            connection.connect(path)
            connection.sendall(b"register\n")
            reply = bytearray()
            while len(reply) < 16 and not reply.endswith(b"\n"):
                chunk = connection.recv(16 - len(reply))
                if not chunk:
                    break
                reply.extend(chunk)
            if reply != b"ok\n":
                raise RuntimeError("Hook Memory identity registration was denied")
        except OSError as exc:
            raise RuntimeError("Hook Memory identity registration is unavailable") from exc
        connection.settimeout(None)
        yield


def main(handler):
    request = json.load(sys.stdin)
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise ValueError("expected Hook JSON protocol version 1")
    request.setdefault("metadata", {})

    async def dispatch():
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
        # The Runtime owns this private, per-session memory. It is never
        # included in trace metadata or written to disk by the plugin.
        from pathlib import Path
        plugin_root = str(Path(__file__).resolve().parents[1])
        if plugin_root not in sys.path:
            sys.path.append(plugin_root)
        from lib import get_bridge
        bridge = get_bridge()
        bridge.restore_session_state(request.get("state", {}), request["session_id"])
        try:
            response = await handler(request) or {}
            terminal = handler.__name__ == "after_run" or (
                handler.__name__ == "on_error"
                and request.get("status") in {"failed", "cancelled", "interrupted"}
            )
            response["state"] = {} if terminal else (
                bridge.session_state(request["session_id"]) if bridge.available
                else request.get("state", {})
            )
            return response
        except PermissionError as exc:
            error = exc.to_metadata() if hasattr(exc, "to_metadata") else {}
            return {"decision": "deny", "reason": str(exc), "error": error}
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    with hook_memory_registration(), redirect_stdout(sys.stderr):
        response = asyncio.run(dispatch())
    json.dump(response, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
