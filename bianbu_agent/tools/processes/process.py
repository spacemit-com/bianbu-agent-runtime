import asyncio
import json
from bianbu_agent.tools import tool
from .records import (
    close_stdin,
    exit_code,
    output,
    record,
    records,
    terminate,
    write,
)


def create_tool(package_context):

    @tool(parse_docstring=True)
    async def process(
        action: str,
        data: str = None,
        limit: int = None,
        offset: int = None,
        session_id: str = None,
        timeout: int = None,
    ):
        """Manage background processes started with terminal(background=true). Actions: 'list' (show all), 'poll' (check status + new output), 'log' (full output with pagination), 'wait' (block until done or timeout), 'kill' (terminate), 'write' (send raw stdin data without newline), 'submit' (send data + Enter, for answering prompts), 'close' (close stdin/send EOF).

        Args:
            action: Action to perform on background processes
            data: Text to send to process stdin (for 'write' and 'submit' actions)
            limit: Max lines to return for 'log' action
            offset: Line offset for 'log' action (default: last 200 lines)
            session_id: Process session ID (from terminal background output). Required for all actions except 'list'.
            timeout: Max seconds to block for 'wait' action. Returns partial output on timeout.
        """
        arguments = {
            key: value
            for key, value in {
                "action": action,
                "data": data,
                "limit": limit,
                "offset": offset,
                "session_id": session_id,
                "timeout": timeout,
            }.items()
            if value is not None
        }
        action = str(arguments.get("action") or "")
        session = str(arguments.get("session_id") or "")
        call = package_context.call
        state_root = package_context.state_root
        if action == "list":
            handles = records(state_root, call.session_id, call.agent_id)
            return json.dumps(
                {
                    "processes": [
                        {
                            "session_id": handle["session_id"],
                            "command": handle["command"],
                            "status": "running" if handle["running"] else "completed",
                        }
                        for handle in handles
                    ]
                },
                ensure_ascii=False,
            )
        supported = {"wait", "poll", "log", "write", "submit", "kill", "close"}
        if action not in supported:
            return json.dumps(
                {
                    "success": False,
                    "code": "unsupported_action",
                    "error": f"Unsupported process action: {action}",
                },
                ensure_ascii=False,
            )
        try:
            value = record(
                state_root,
                session,
                call.session_id,
                call.agent_id,
            )
        except KeyError, ValueError:
            return json.dumps(
                {
                    "success": False,
                    "code": "process_not_found",
                    "error": "Background process not found",
                },
                ensure_ascii=False,
            )
        if action == "wait":
            deadline = asyncio.get_running_loop().time() + int(
                arguments.get("timeout") or 30
            )
            while value["running"] and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                value = record(
                    state_root,
                    session,
                    call.session_id,
                    call.agent_id,
                )
            return json.dumps(
                {
                    "output": output(state_root, value),
                    "exit_code": exit_code(state_root, value),
                    "status": "timeout" if value["running"] else "completed",
                },
                ensure_ascii=False,
            )
        if action == "poll":
            return json.dumps(
                {
                    "output": output(state_root, value),
                    "exit_code": exit_code(state_root, value),
                    "status": "running" if value["running"] else "completed",
                },
                ensure_ascii=False,
            )
        if action == "log":
            lines = output(state_root, value).splitlines()
            start = (
                int(arguments["offset"])
                if arguments.get("offset") is not None
                else max(0, len(lines) - int(arguments.get("limit") or 200))
            )
            selected = lines[start : start + int(arguments.get("limit") or 200)]
            return json.dumps(
                {"output": "\n".join(selected), "total_lines": len(lines)},
                ensure_ascii=False,
            )
        if action in {"write", "submit"}:
            data = str(arguments.get("data") or "")
            if action == "submit" and not data.endswith("\n"):
                data += "\n"
            try:
                write(state_root, value, data)
            except (BrokenPipeError, OSError) as exc:
                return json.dumps(
                    {"success": False, "code": "stdin_unavailable", "error": str(exc)}
                )
            return json.dumps({"status": "written"}, ensure_ascii=False)
        if action == "close":
            close_stdin(state_root, value)
            return json.dumps(
                {
                    "output": output(state_root, value),
                    "exit_code": exit_code(state_root, value),
                    "status": "closed",
                },
                ensure_ascii=False,
            )
        terminate(state_root, value)
        return json.dumps(
            {
                "output": output(state_root, value),
                "exit_code": exit_code(state_root, value),
                "status": "killed",
            },
            ensure_ascii=False,
        )

    return process
