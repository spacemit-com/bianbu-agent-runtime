from __future__ import annotations
import asyncio
import json
import os
import signal
from bianbu_agent.tools import tool
from .launch import resolve_workdir, start


def create_tool(context):

    @tool(parse_docstring=True)
    async def terminal(
        command: str,
        background: bool = False,
        timeout: int = 180,
        workdir: str | None = None,
    ) -> str:
        """Execute a shell command inside the current Tool workspace.

        Args:
            command: Shell command to execute.
            background: Start the command in the background and return a session ID.
            timeout: Maximum seconds to wait for a foreground command.
            workdir: Optional working directory inside the Tool workspace.
        """
        if not isinstance(command, str) or not command.strip():
            return json.dumps(
                {
                    "success": False,
                    "code": "invalid_command",
                    "error": "command must be non-empty",
                }
            )
        try:
            cwd = resolve_workdir(context.workspace_root, workdir)
        except (OSError, PermissionError, ValueError) as exc:
            return json.dumps(
                {"success": False, "code": "invalid_workdir", "error": str(exc)}
            )
        if background:
            call = context.call
            handle = await asyncio.to_thread(
                start,
                command,
                context.workspace_root,
                workdir,
                context.state_root,
                call.session_id,
                call.agent_id,
            )
            return json.dumps(
                {"session_id": handle["session_id"], "status": "running"},
                ensure_ascii=False,
            )
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=max(1, timeout)
            )
        except asyncio.CancelledError:
            # Cancelling communicate() only closes the await; the command
            # otherwise survives as a detached session after its Run ends.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.communicate()
            raise
        except TimeoutError:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = await process.communicate()
        return json.dumps(
            {
                "output": output.decode(errors="replace"),
                "exit_code": process.returncode
                if process.returncode is not None
                else -1,
                "status": "timeout" if timed_out else "completed",
            },
            ensure_ascii=False,
        )

    return terminal
