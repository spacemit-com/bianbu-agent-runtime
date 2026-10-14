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
        try:
            return await handler(request) or {}
        except PermissionError as exc:
            error = exc.to_metadata() if hasattr(exc, "to_metadata") else {}
            return {"decision": "deny", "reason": str(exc), "error": error}
        finally:
            loop.remove_signal_handler(signal.SIGTERM)

    with hook_memory_registration(), redirect_stdout(sys.stderr):
        response = asyncio.run(dispatch())
    json.dump(response, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
