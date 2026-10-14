from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Sequence


class BrowserManager:
    """Shared lifecycle and subprocess transport for directory-owned browser Tools."""

    def __init__(self, executable: str | None = None) -> None:
        self._executable = executable or os.environ.get("BIANBU_AGENT_BROWSER_COMMAND") or "agent-browser"

    async def run(
        self,
        session_id: str,
        command: str,
        arguments: Sequence[str] = (),
    ) -> dict[str, object]:
        executable = shutil.which(self._executable)
        if executable is None:
            return {
                "success": False,
                "code": "browser_unavailable",
                "error": (
                    "agent-browser is not installed; install it and its Chromium "
                    "runtime before using browser Tools"
                ),
            }
        process = await asyncio.create_subprocess_exec(
            executable,
            "--session",
            str(session_id),
            "--json",
            str(command),
            *(str(item) for item in arguments),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
        except TimeoutError:
            process.kill()
            await process.wait()
            return {"success": False, "code": "browser_timeout", "error": "browser command timed out"}
        output = stdout.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            return {
                "success": False,
                "code": "browser_command_failed",
                "error": stderr.decode("utf-8", errors="replace").strip() or output,
            }
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            payload = {"success": True, "data": {"output": output}}
        return payload if isinstance(payload, dict) else {"success": True, "data": payload}


__all__ = ["BrowserManager"]
