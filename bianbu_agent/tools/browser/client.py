from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Sequence


async def run(session_id: str, command: str, arguments: Sequence[str] = ()) -> dict[str, object]:
    executable_name = os.environ.get("BIANBU_AGENT_BROWSER_COMMAND", "agent-browser")
    executable = shutil.which(executable_name)
    if executable is None:
        return {"success": False, "code": "browser_unavailable", "error": "agent-browser is not installed"}
    process = await asyncio.create_subprocess_exec(
        executable, "--session", session_id, "--json", command,
        *(str(item) for item in arguments),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        return {"success": False, "code": "browser_timeout", "error": "browser command timed out"}
    output = stdout.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        return {"success": False, "code": "browser_command_failed", "error": stderr.decode("utf-8", errors="replace").strip() or output}
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = {"success": True, "data": {"output": output}}
    return payload if isinstance(payload, dict) else {"success": True, "data": payload}
