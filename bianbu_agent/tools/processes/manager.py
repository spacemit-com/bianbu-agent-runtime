from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import signal
from uuid import uuid4

from bianbu_agent.tools.processes.contracts import (
    ProcessHandle,
    ProcessRequest,
    ProcessResult,
)


def sanitize_subprocess_environment(
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    """Remove model-provider credentials from child process environments."""
    selected = dict(os.environ if environment is None else environment)
    from bianbu_agent.tools.processes.credentials import (
        PROVIDER_SUBPROCESS_PASSTHROUGH_ENV_NAMES,
        SUPPORTED_PROVIDER_ENV_NAMES,
    )

    blocked = {
        name
        for names in SUPPORTED_PROVIDER_ENV_NAMES.values()
        for name in names
    } - set(PROVIDER_SUBPROCESS_PASSTHROUGH_ENV_NAMES)
    for name in blocked:
        selected.pop(name, None)
    return selected


@dataclass(slots=True)
class _BackgroundProcess:
    handle: ProcessHandle
    process: asyncio.subprocess.Process
    task: asyncio.Task[ProcessResult]
    output: bytearray


class ProcessManager:
    def __init__(self, *, workspace: Path) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self._background: dict[str, _BackgroundProcess] = {}

    async def run(self, request: ProcessRequest) -> ProcessResult:
        workdir = self._workdir(request.workdir)
        process = await asyncio.create_subprocess_shell(
            request.command,
            cwd=workdir,
            env=sanitize_subprocess_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            stdout, _ = await process.communicate()
            return ProcessResult(
                output=stdout.decode(errors="replace"),
                exit_code=process.returncode if process.returncode is not None else -1,
                timed_out=True,
            )
        return ProcessResult(
            output=stdout.decode(errors="replace"),
            exit_code=process.returncode if process.returncode is not None else -1,
        )

    async def start(self, request: ProcessRequest) -> ProcessHandle:
        process = await asyncio.create_subprocess_shell(
            request.command,
            cwd=self._workdir(request.workdir),
            env=sanitize_subprocess_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        handle = ProcessHandle(
            session_id=f"process-{uuid4().hex[:12]}",
            command=request.command,
        )
        output = bytearray()
        task = asyncio.create_task(self._collect(process, output))
        self._background[handle.session_id] = _BackgroundProcess(
            handle, process, task, output
        )
        return handle

    async def wait(self, session_id: str, *, timeout_seconds: int) -> ProcessResult:
        session = self._background.get(session_id)
        if session is None:
            raise KeyError(session_id)
        try:
            result = await asyncio.wait_for(
                asyncio.shield(session.task),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return ProcessResult(output="", exit_code=-1, timed_out=True)
        self._background.pop(session_id, None)
        return result

    async def list(self) -> list[ProcessHandle]:
        return [
            session.handle
            for session in self._background.values()
            if not session.task.done()
        ]

    async def poll(self, session_id: str) -> tuple[str, ProcessResult | None]:
        session = self._background.get(session_id)
        if session is None:
            raise KeyError(session_id)
        result = session.task.result() if session.task.done() else None
        return session.output.decode(errors="replace"), result

    async def log(
        self,
        session_id: str,
        *,
        offset: int | None = None,
        limit: int = 200,
    ) -> tuple[str, int]:
        output, _result = await self.poll(session_id)
        lines = output.splitlines()
        start = max(0, int(offset)) if offset is not None else max(0, len(lines) - limit)
        page = lines[start : start + max(1, int(limit))]
        return "\n".join(page), len(lines)

    async def write(self, session_id: str, data: str) -> None:
        session = self._background.get(session_id)
        if session is None:
            raise KeyError(session_id)
        stdin = session.process.stdin
        if stdin is None or stdin.is_closing():
            raise BrokenPipeError(f"process stdin is closed: {session_id}")
        stdin.write(data.encode())
        await stdin.drain()

    async def kill(self, session_id: str) -> ProcessResult:
        session = self._background.get(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.process.returncode is None:
            os.killpg(os.getpgid(session.process.pid), signal.SIGTERM)
        result = await session.task
        self._background.pop(session_id, None)
        return result

    async def close(self) -> None:
        errors: list[BaseException] = []
        for session_id in tuple(self._background):
            try:
                await self.kill(session_id)
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("Process manager close failed", errors)

    @staticmethod
    async def _collect(
        process: asyncio.subprocess.Process,
        output: bytearray,
    ) -> ProcessResult:
        stdout = process.stdout
        if stdout is not None:
            while chunk := await stdout.read(4096):
                output.extend(chunk)
        await process.wait()
        return ProcessResult(
            output=output.decode(errors="replace"),
            exit_code=process.returncode if process.returncode is not None else -1,
        )

    def _workdir(self, selected: str | None) -> Path:
        if selected is None:
            return self.workspace
        resolved = Path(selected).expanduser().resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("process workdir must be inside the Tool workspace")
        return resolved
