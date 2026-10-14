"""Versioned JSON command hooks owned and executed by a Worker plugin."""
from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import math
import os
import signal
import sys
from pathlib import Path

from bianbu_agent.plugins.composition import plugin_config_document
from bianbu_agent.plugins.contributions import ContextPatch, HookContribution, HookPoint

logger = logging.getLogger(__name__)

EVENTS = {
    'BeforeRun': HookPoint.BEFORE_RUN,
    'AfterContext': HookPoint.AFTER_CONTEXT,
    'PreToolUse': HookPoint.BEFORE_TOOL,
    'PostToolUse': HookPoint.AFTER_TOOL,
    'BeforeModel': HookPoint.BEFORE_MODEL,
    'AfterModel': HookPoint.AFTER_MODEL,
    'AfterRun': HookPoint.AFTER_RUN,
    'OnError': HookPoint.ON_ERROR,
}
_LIMIT = 1024 * 1024
_GATES = {HookPoint.BEFORE_RUN, HookPoint.BEFORE_TOOL, HookPoint.BEFORE_MODEL}
# Per-command timeout policy.  ``fail`` keeps the fail-closed gate semantics
# (security Hook timeouts must never silently allow a Run).  ``skip`` is for
# best-effort Hooks whose own scripts already declare they must not block a
# Run (e.g. memory recall); under board-wide CPU starvation they time out
# without any policy decision, so the Run continues without their patch.
_ON_TIMEOUT = frozenset({'fail', 'skip'})


def inspect_command_hook(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
        raise ValueError('hook.json must be a regular file no larger than 1 MiB')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) - {'schema_version', 'hooks', 'priority', 'metadata', 'parallel', 'waits_for'} or 'schema_version' not in data or 'hooks' not in data or type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('hook.json requires schema_version 1 and hooks')
    if type(data.get('priority', 100)) is not int:
        raise ValueError('priority must be an integer')
    if not isinstance(data.get('metadata', {}), dict):
        raise ValueError('metadata must be an object')
    if type(data.get('parallel', False)) is not bool:
        raise ValueError('parallel must be a boolean')
    waits_for = data.get('waits_for', [])
    if not isinstance(waits_for, list) or len(waits_for) > 64:
        raise ValueError('waits_for must be a list of at most 64 globs')
    for pattern in waits_for:
        if not isinstance(pattern, str) or not pattern or len(pattern) > 512:
            raise ValueError('waits_for entries must be nonempty globs')
    if waits_for and not data.get('parallel', False):
        raise ValueError('waits_for requires parallel')
    events = data['hooks']
    if not isinstance(events, dict) or len(events) != 1 or next(iter(events)) not in EVENTS:
        raise ValueError('Each hook.json must declare exactly one supported event')
    event, groups = next(iter(events.items()))
    if not isinstance(groups, list) or not groups or len(groups) > 64:
        raise ValueError('Hook event requires 1–64 matcher groups')
    commands = []
    for group in groups:
        if not isinstance(group, dict) or set(group) - {'matcher', 'hooks'}:
            raise ValueError('Invalid Hook matcher group')
        matcher = group.get('matcher', '*')
        if not isinstance(matcher, str) or not matcher or len(matcher) > 512:
            raise ValueError('matcher must be a nonempty glob')
        handlers = group.get('hooks')
        if not isinstance(handlers, list) or not handlers or len(handlers) > 64:
            raise ValueError('Hook group requires command handlers')
        for handler in handlers:
            if not isinstance(handler, dict) or set(handler) - {'type', 'command', 'timeout', 'on_timeout'} or handler.get('type') != 'command':
                raise ValueError('Only command Hook handlers are supported')
            command = handler.get('command')
            timeout = handler.get('timeout', 5)
            on_timeout = handler.get('on_timeout', 'fail')
            if not isinstance(command, str) or not command.strip() or '\x00' in command:
                raise ValueError('command must be nonempty shell text')
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
                raise ValueError('timeout must be between 0 and 300 seconds')
            if on_timeout not in _ON_TIMEOUT:
                raise ValueError('on_timeout must be one of: fail, skip')
            commands.append((matcher, command, timeout, on_timeout))
    return event, tuple(commands), tuple(waits_for)


async def _read(stream):
    result = bytearray()
    while chunk := await stream.read(65536):
        result.extend(chunk)
        if len(result) > _LIMIT:
            raise ValueError('Hook output exceeds 1 MiB')
    return bytes(result)


async def _execute(command, payload, *, root, workspace, timeout, data_root):
    env = dict(os.environ)
    env['RUNTIME_PYTHON'] = sys.executable
    env['PLUGIN_ROOT'] = str(root)
    env['PLUGIN_DATA_ROOT'] = str(data_root)
    env['PLUGIN_WORKSPACE_ROOT'] = str(workspace)
    from bianbu_agent.server.hook_memory_registration import (
        REGISTRATION_SOCKET_ENV,
        registration_environment,
    )
    env.pop(REGISTRATION_SOCKET_ENV, None)
    env.update(registration_environment(os.environ))

    # Inherit the bound Worker's security context; never launch via a host
    # service or strip seccomp/NoNewPrivileges/securityd identity.
    process = await asyncio.create_subprocess_exec(
        '/bin/sh', '-c', command, cwd=workspace, env=env, start_new_session=True,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    async def send_input():
        process.stdin.write(payload)
        try:
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            process.stdin.close()

    async def exchange():
        readers = [asyncio.create_task(_read(process.stdout)),
                   asyncio.create_task(_read(process.stderr)),
                   asyncio.create_task(send_input())]
        try:
            stdout, _, _ = await asyncio.gather(*readers)
            await process.wait()
            if process.returncode:
                raise ValueError(f'Hook command exited with status {process.returncode}')
            return stdout
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
    task = asyncio.create_task(exchange())
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(task)
    except TimeoutError as exc:
        # ``str(TimeoutError())`` is empty, so the gate wrapper and the Hook
        # observation would otherwise report "Command Hook failed: " with no
        # hint of which Hook ran out of budget.
        raise TimeoutError(f'Hook command timed out after {timeout:g}s') from exc
    finally:
        # Let scripts cancel remote work before terminating the process group.
        # Keep draining output while they clean up; a full pipe must not deadlock.
        if not task.done():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=3.5)
            except (Exception, asyncio.CancelledError):
                pass
        # Includes descendants that outlive the shell, timeout and cancellation.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await process.wait()



class CommandHookDenied(PermissionError):
    """Structured denial carried across the command JSON boundary."""

    def __init__(self, reason, details):
        fields = {'tool', 'capability', 'policy', 'decision', 'reason',
                  'degraded', 'terminate_run'}
        if not isinstance(details, dict) or set(details) - fields:
            raise ValueError('Invalid structured denial fields')
        for name in fields - {'degraded', 'terminate_run'}:
            value = details.get(name, reason if name == 'reason' else '')
            if not isinstance(value, str):
                raise ValueError('Denial text fields must be strings')
            setattr(self, name, value)
        for name in ('degraded', 'terminate_run'):
            if name in details and type(details[name]) is not bool:
                raise ValueError('Denial flags must be booleans')
        if not self.capability:
            raise ValueError('Structured denial requires a tool name')
        self.degraded = details.get('degraded', False)
        # 三态模型里 deny 是明确决定，一律终止整轮 run。
        self.should_terminate_run = details.get('terminate_run', True)
        super().__init__(reason)

    def to_metadata(self):
        return {name: getattr(self, name) for name in
                ('tool', 'capability', 'policy', 'decision', 'reason', 'degraded')} | {
                    'terminate_run': self.should_terminate_run}


def _patch(stdout, point):
    data = json.loads(stdout) if stdout.strip() else {}
    if not isinstance(data, dict) or set(data) - {'context_sections', 'direct_reply', 'decision', 'reason', 'metadata', 'error', 'state'}:
        raise ValueError('Invalid Hook stdout JSON fields')
    decision = data.get('decision', 'continue')
    if decision not in ('continue', 'allow', 'deny'):
        raise ValueError('decision must be continue, allow or deny')
    if decision == 'deny':
        if point not in _GATES:
            raise ValueError('Hook is not allowed to deny at this event')
        reason = str(data.get('reason') or 'Command Hook denied execution')
        if 'error' in data:
            raise CommandHookDenied(reason, data['error'])
        raise PermissionError(reason)
    # allow is advisory only: it cannot skip subsequent hooks or securityd.
    sections = data.get('context_sections', [])
    reply = data.get('direct_reply')
    if not isinstance(sections, list) or not all(isinstance(x, str) for x in sections):
        raise ValueError('context_sections must be an array of strings')
    if reply is not None and (not isinstance(reply, str) or point is not HookPoint.BEFORE_RUN):
        raise ValueError('direct_reply is only supported at BeforeRun')
    if 'state' in data and not isinstance(data['state'], dict):
        raise ValueError('state must be an object')
    metadata = data.get('metadata', {})
    if not isinstance(metadata, dict):
        raise ValueError('metadata must be an object')
    return ContextPatch(context_sections=tuple(sections), direct_reply=reply, metadata=metadata)


def load_command_hook(root, *, context, owner, digest):
    event, commands, waits_for = inspect_command_hook(root / 'hook.json')
    point = EVENTS[event]
    plugin_root = root.parent.parent
    definition = json.loads((root / 'hook.json').read_text())
    context.data_root.mkdir(parents=True, exist_ok=True)
    label = f'{owner}/{root.name}'

    async def invoke(run, state_key):
        tool = str(run.metadata.get('tool', ''))
        selected = [(command, timeout, on_timeout) for matcher, command, timeout, on_timeout in commands
                    if point not in {HookPoint.BEFORE_TOOL, HookPoint.AFTER_TOOL}
                    or fnmatch.fnmatchcase(tool, matcher)
                    or fnmatch.fnmatchcase(tool.rsplit('/', 1)[-1], matcher)]
        if not selected:
            return None
        sections, reply, metadata = [], None, {}
        try:
            for command, timeout, on_timeout in selected:
                payload = json.dumps({
                    'schema_version': 1, 'hook_event_name': event,
                    'state': context.command_state.get(state_key),
                    'config': plugin_config_document(context.config),
                    'run_id': run.run_id, 'session_id': run.session_id,
                    'agent_id': run.agent_id, 'account_id': run.account_id,
                    'request': run.request, 'response': run.response, 'status': run.status,
                    # `request` 在工具事件里是工具限定名，不是用户的话；脚本事先
                    # 判断「用户在说什么」要用 prompt。
                    'prompt': str(run.metadata.get('user_prompt') or ''),
                    'metadata': dict(run.metadata),
                    'tool_name': tool, 'tool_input': dict(run.metadata.get('arguments', {})),
                }, ensure_ascii=False).encode() + b'\n'
                if len(payload) > _LIMIT:
                    raise ValueError('Hook input exceeds 1 MiB')
                try:
                    stdout = await _execute(command, payload, root=plugin_root,
                        workspace=context.workspace_root, timeout=timeout, data_root=context.data_root)
                except TimeoutError:
                    if on_timeout != 'skip':
                        raise
                    logger.warning(
                        'command Hook timed out and was skipped: %s (after %ss)',
                        label, timeout,
                    )
                    # Remaining commands of this Hook consume this one's state,
                    # so a skipped step ends the group instead of feeding them
                    # a half-built patch.
                    break
                patch = _patch(stdout, point)
                output = json.loads(stdout) if stdout.strip() else {}
                if 'state' in output:
                    context.command_state.replace(state_key, output['state'])
                sections.extend(patch.context_sections)
                metadata.update(patch.metadata)
                if patch.direct_reply is not None:
                    if reply is not None:
                        raise ValueError('Multiple command Hook direct replies')
                    reply = patch.direct_reply
        except (asyncio.CancelledError, PermissionError):
            raise
        except (OSError, ValueError, TypeError, OverflowError, RecursionError, TimeoutError) as exc:
            reason = str(exc).strip() or type(exc).__name__
            if point in _GATES:
                raise PermissionError(f'Command Hook failed: {reason} [{label}]') from exc
            raise
        return ContextPatch(context_sections=tuple(sections), direct_reply=reply, metadata=metadata)

    async def handler(run):
        # State is private to one Plugin in one Session: keying it by Plugin as
        # well keeps sibling Hooks from clobbering each other's state dict, and
        # lets order-independent Hooks of different Plugins overlap.
        key = (run.account_id, run.agent_id, run.session_id, owner)
        async with context.command_state.session(key):
            return await invoke(run, key)

    return HookContribution(plugin_id=owner, id=root.name, point=point, handler=handler,
        priority=definition.get('priority', 100),
        parallel=definition.get('parallel', False),
        waits_for=waits_for,
        timeout_seconds=sum(timeout for _, _, timeout, _ in commands) + 5,
        metadata={**definition.get('metadata', {}), 'origin': 'plugin', 'plugin_id': owner, 'content_digest': digest,
                  'definition_path': str(root / 'hook.json'), 'command_hook': True})
