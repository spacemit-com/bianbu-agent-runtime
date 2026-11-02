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


def _parse(line):
    request = json.loads(line)
    if not isinstance(request, dict) or request.get("schema_version") != 1:
        raise ValueError("expected Hook JSON protocol version 1")
    request.setdefault("metadata", {})
    return request


def _dispatch(handler, request):
    async def run():
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

    return asyncio.run(run())


def _write(response, stream):
    # ``redirect_stdout`` sends stray prints to stderr, so the transport keeps
    # a reference to the real stdout captured before entering it.
    json.dump(response, stream, ensure_ascii=False)
    stream.write("\n")
    stream.flush()


def _warm(warmup):
    """Run plugin-owned start-up imports without changing a Run.

    The Runtime spawns resident Hooks while the Worker is still starting.
    Paying the plugin's import cost there keeps the first user request as fast
    as the second one.  Failure stays best effort: the first real request will
    retry the import and report its own error through the normal path.
    """

    if warmup is None:
        return
    try:
        warmup()
    except Exception:  # noqa: BLE001 - warm-up must never break the transport
        import traceback
        traceback.print_exc(file=sys.stderr)


def main(handler, *, warmup=None):
    """Serve one frame per process, or one per line while resident.

    ``BIANBU_HOOK_RESIDENT=1`` keeps the interpreter, the imports and the
    plugin's own module state alive across invocations.  The wire format is
    unchanged: one JSON request line in, one JSON response line out.  A
    resident process must exit on EOF so a Worker restart cannot orphan it.
    """

    resident = os.environ.get("BIANBU_HOOK_RESIDENT") == "1"
    out, source = sys.stdout, sys.stdin
    with hook_memory_registration(), redirect_stdout(sys.stderr):
        if not resident:
            _write(_dispatch(handler, _parse(source.read())), out)
            return
        _warm(warmup)
        for line in source:
            if not line.strip():
                continue
            _write(_dispatch(handler, _parse(line)), out)
