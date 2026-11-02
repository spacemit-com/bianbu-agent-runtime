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

from bianbu_agent.hooks.resident import ResidentHookError, ResidentProcess
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
# Per-command timeout policy.  Every event point is fail-closed by default:
# a Hook that cannot decide must not silently allow the Run to continue.
# ``skip`` is for best-effort Hooks whose own scripts declare they must not
# block a Run (e.g. memory recall); under board-wide CPU starvation they time
# out without any policy decision, so the Run continues without their patch.
_ON_TIMEOUT = frozenset({'fail', 'skip'})
#: ``oneshot`` starts one process per invocation; ``resident`` keeps the
#: process alive so interpreter start-up and imports are paid once per
#: generation.  Resident Hooks are torn down with their Plugin generation.
_MODES = frozenset({'oneshot', 'resident'})


def inspect_command_hook(path: Path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > _LIMIT:
        raise ValueError('hook.json must be a regular file no larger than 1 MiB')
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) - {'schema_version', 'hooks', 'priority', 'metadata'} or 'schema_version' not in data or 'hooks' not in data or type(data['schema_version']) is not int or data['schema_version'] != 1:
        raise ValueError('hook.json requires schema_version 1 and hooks')
    if type(data.get('priority', 100)) is not int:
        raise ValueError('priority must be an integer')
    if not isinstance(data.get('metadata', {}), dict):
        raise ValueError('metadata must be an object')
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
            if not isinstance(handler, dict) or set(handler) - {'type', 'command', 'timeout', 'on_timeout', 'mode'} or handler.get('type') != 'command':
                raise ValueError('Only command Hook handlers are supported')
            command = handler.get('command')
            timeout = handler.get('timeout', 5)
            on_timeout = handler.get('on_timeout', 'fail')
            mode = handler.get('mode', 'oneshot')
            if mode not in _MODES:
                raise ValueError('mode must be one of: oneshot, resident')
            if not isinstance(command, str) or not command.strip() or '\x00' in command:
                raise ValueError('command must be nonempty shell text')
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 300:
                raise ValueError('timeout must be between 0 and 300 seconds')
            if on_timeout not in _ON_TIMEOUT:
                raise ValueError('on_timeout must be one of: fail, skip')
            commands.append((matcher, command, timeout, on_timeout, mode))
    return event, tuple(commands)


async def _read(stream):
    result = bytearray()
    while chunk := await stream.read(65536):
        result.extend(chunk)
        if len(result) > _LIMIT:
            raise ValueError('Hook output exceeds 1 MiB')
    return bytes(result)


#: Hook stderr can be arbitrarily large (a traceback per retry), and the
#: operational log is bounded per record, so only the tail is kept.
_FAILED_HOOK_STDERR_BYTES = 2048


def _log_failed_hook(command: str, returncode: int, stderr: bytes) -> None:
    """Record why a Hook process exited non-zero.

    The exit status alone cannot distinguish a permission denial from a
    syntax error, so the stderr tail is the only actionable evidence.  It is
    logged rather than raised: the Run's error stays generic because Hook
    stderr is plugin-controlled text that may contain user prompt content.
    """

    tail = stderr[-_FAILED_HOOK_STDERR_BYTES:].decode('utf-8', 'replace').strip()
    logger.error(
        'Hook command exited with status %s: %s\n%s',
        returncode, command, tail or '(no stderr output)',
        extra={'event': 'hook_command_failed'},
    )


def hook_environment(root, *, workspace, data_root):
    """Environment shared by one-shot and resident command Hooks.

    Resident Hooks are spawned from the same helper so a reused process keeps
    exactly the security context, plugin roots and memory registration a
    one-shot process would have received.
    """

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
    return env


async def _execute(command, payload, *, root, workspace, timeout, data_root):
    env = hook_environment(root, workspace=workspace, data_root=data_root)

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
            stdout, stderr, _ = await asyncio.gather(*readers)
            await process.wait()
            if process.returncode:
                # A Hook that dies before printing its JSON leaves only this
                # exit status for the operator; the actual cause (an
                # unimportable script, a denied identity registration) is on
                # stderr, which was previously discarded.  Keep the last lines
                # in the operational log so a failed Run is diagnosable
                # without attaching a probe to the board.  Only the tail is
                # kept, and this text never reaches the Run's user-facing
                # error: plugin stderr may echo prompt content.
                _log_failed_hook(command, process.returncode, stderr)
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



class HookDenied(PermissionError):
    """A Hook veto.  Always terminates the Run, at every event point.

    It exposes ``capability`` and ``reason`` so ``is_security_denial``
    recognises it and the Run reports a structured security error rather than
    a generic failure.
    """

    should_terminate_run = True

    def __init__(self, reason: str, *, tool: str = "", capability: str = "") -> None:
        self.reason = str(reason)
        self.tool = str(tool)
        self.capability = str(capability or tool)
        self.policy = "deny"
        self.decision = "DENY"
        self.degraded = False
        super().__init__(reason)

    def to_metadata(self) -> dict:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "policy": self.policy,
            "decision": self.decision,
            "reason": self.reason,
            "degraded": self.degraded,
            "terminate_run": True,
        }


class CommandHookDenied(HookDenied):
    """Structured denial carried across the command JSON boundary.

    A denial always terminates the Run.  ``terminate_run`` is still accepted
    from plugins for schema compatibility, but it is reported as ``True``
    regardless: there is no "deny but continue" state.
    """

    def __init__(self, reason, details):
        fields = {'tool', 'capability', 'policy', 'decision', 'reason',
                  'degraded', 'terminate_run'}
        if not isinstance(details, dict) or set(details) - fields:
            raise ValueError('Invalid structured denial fields')
        for name in fields - {'degraded', 'terminate_run'}:
            value = details.get(name, reason if name == 'reason' else '')
            if not isinstance(value, str):
                raise ValueError('Denial text fields must be strings')
        if 'terminate_run' in details and type(details['terminate_run']) is not bool:
            raise ValueError('Denial flags must be booleans')
        if 'degraded' in details and type(details['degraded']) is not bool:
            raise ValueError('Denial flags must be booleans')
        if not str(details.get('capability') or ''):
            raise ValueError('Structured denial requires a tool name')
        PermissionError.__init__(self, reason)
        self.reason = str(details.get('reason', reason))
        self.tool = str(details.get('tool', ''))
        self.capability = str(details['capability'])
        self.policy = str(details.get('policy', 'deny'))
        self.decision = str(details.get('decision', 'DENY'))
        self.degraded = details.get('degraded', False)

    def to_metadata(self):
        return {name: getattr(self, name) for name in
                ('tool', 'capability', 'policy', 'decision', 'reason', 'degraded')} | {
                    'terminate_run': True}


def _patch(stdout, point):
    data = json.loads(stdout) if stdout.strip() else {}
    if not isinstance(data, dict) or set(data) - {'context_sections', 'direct_reply', 'decision', 'reason', 'metadata', 'error', 'state', 'approval', 'tool_arguments', 'user_visible_text', 'stream_stand_ins'}:
        raise ValueError('Invalid Hook stdout JSON fields')
    decision = data.get('decision', 'continue')
    if decision not in ('continue', 'allow', 'deny'):
        raise ValueError('decision must be continue, allow or deny')
    if decision == 'deny':
        # Any Hook at any event may terminate the Run.  Event points describe
        # when a Hook runs, not whether it is allowed to veto.
        reason = str(data.get('reason') or 'Command Hook denied execution')
        if 'error' in data:
            raise CommandHookDenied(reason, data['error'])
        raise HookDenied(reason)
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
    tool_arguments = data.get('tool_arguments')
    if tool_arguments is not None and not isinstance(tool_arguments, dict):
        raise ValueError('tool_arguments must be an object')
    approval = data.get('approval')
    if approval is not None and (point is not HookPoint.BEFORE_RUN or reply is None):
        raise ValueError('approval requires a BeforeRun direct reply')
    user_visible_text = data.get('user_visible_text')
    if user_visible_text is not None and not isinstance(user_visible_text, str):
        raise ValueError('user_visible_text must be a string')
    stream_stand_ins = data.get('stream_stand_ins')
    if stream_stand_ins is not None and (
        not isinstance(stream_stand_ins, dict)
        or any(not isinstance(k, str) or not isinstance(v, str)
               for k, v in stream_stand_ins.items())
    ):
        raise ValueError('stream_stand_ins must map strings to strings')
    return ContextPatch(context_sections=tuple(sections), direct_reply=reply,
                        metadata=metadata, approval=approval,
                        tool_arguments=tool_arguments,
                        user_visible_text=user_visible_text,
                        stream_stand_ins=stream_stand_ins)


def load_command_hook(root, *, context, owner, digest):
    event, commands = inspect_command_hook(root / 'hook.json')
    point = EVENTS[event]
    plugin_root = root.parent.parent
    definition = json.loads((root / 'hook.json').read_text())
    context.data_root.mkdir(parents=True, exist_ok=True)
    label = f'{owner}/{root.name}'

    def spawn(command):
        return ResidentProcess(
            command, root=plugin_root,
            workspace=context.workspace_root,
            data_root=context.data_root, digest=digest)

    async def invoke(run, state_key):
        tool = str(run.metadata.get('tool', ''))
        selected = [(command, timeout, on_timeout, mode)
                    for matcher, command, timeout, on_timeout, mode in commands
                    if point not in {HookPoint.BEFORE_TOOL, HookPoint.AFTER_TOOL}
                    or fnmatch.fnmatchcase(tool, matcher)
                    or fnmatch.fnmatchcase(tool.rsplit('/', 1)[-1], matcher)]
        if not selected:
            return None
        sections, reply, metadata, tool_arguments = [], None, {}, None
        approval = None
        user_visible_text = None
        stream_stand_ins: dict[str, str] = {}
        try:
            for command, timeout, on_timeout, mode in selected:
                payload = json.dumps({
                    'schema_version': 1, 'hook_event_name': event,
                    'state': context.command_state.get(state_key),
                    'config': plugin_config_document(context.config),
                    'run_id': run.run_id, 'session_id': run.session_id,
                    'agent_id': run.agent_id, 'account_id': run.account_id,
                    'request': run.request, 'response': run.response, 'status': run.status,
                    # Session termination is distinct from a Run ending.  The
                    # security Hook clears private stand-in state only for the
                    # explicit reasons below.
                    'exit_reason': getattr(run, 'exit_reason', ''),
                    # `request` 在工具事件里是工具限定名，不是用户的话；脚本事先
                    # 判断「用户在说什么」要用 prompt。
                    'prompt': str(run.metadata.get('user_prompt') or ''),
                    'metadata': dict(run.metadata),
                    # Private Session map, passed to the trusted Hook process
                    # only: stream deltas must be restored before publication
                    # without putting plaintext into run metadata.
                    'tool_name': tool, 'tool_input': dict(run.metadata.get('arguments', {})),
                }, ensure_ascii=False).encode() + b'\n'
                if len(payload) > _LIMIT:
                    raise ValueError('Hook input exceeds 1 MiB')
                try:
                    if mode == 'resident':
                        stdout = await context.command_processes.call(
                            (str(plugin_root), command, digest),
                            lambda command=command: spawn(command),
                            payload, timeout,
                        )
                    else:
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
                if patch.tool_arguments is not None:
                    if tool_arguments is not None:
                        raise ValueError('Multiple command Hook tool argument rewrites')
                    tool_arguments = dict(patch.tool_arguments)
                if patch.direct_reply is not None:
                    if reply is not None:
                        raise ValueError('Multiple command Hook direct replies')
                    reply = patch.direct_reply
                    approval = patch.approval
                if patch.user_visible_text is not None:
                    user_visible_text = patch.user_visible_text
                if patch.stream_stand_ins is not None:
                    stream_stand_ins.update(patch.stream_stand_ins)
        except (asyncio.CancelledError, PermissionError):
            raise
        except (OSError, ValueError, TypeError, OverflowError, RecursionError,
                TimeoutError, ResidentHookError) as exc:
            reason = str(exc).strip() or type(exc).__name__
            # A Hook that cannot produce a decision fails closed at every event
            # point: a broken or timed-out Hook must not silently allow a Run.
            raise HookDenied(f'Command Hook failed: {reason} [{label}]') from exc
        return ContextPatch(context_sections=tuple(sections), direct_reply=reply, metadata=metadata,
                            approval=approval if reply is not None else None,
                            tool_arguments=tool_arguments,
                            user_visible_text=user_visible_text,
                            stream_stand_ins=stream_stand_ins or None)

    async def handler(run):
        # State is private to one Plugin in one Session: keying it by Plugin as
        # well keeps sibling Hooks from clobbering each other's state dict, and
        # lets order-independent Hooks of different Plugins overlap.
        key = (run.account_id, run.agent_id, run.session_id, owner)
        async with context.command_state.session(key):
            return await invoke(run, key)

    async def prewarm():
        if context.command_processes.closed:
            return
        for _matcher, command, _timeout, _on_timeout, mode in commands:
            if mode != 'resident':
                continue
            await context.command_processes.prewarm(
                (str(plugin_root), command, digest),
                lambda command=command: spawn(command),
            )

    return HookContribution(plugin_id=owner, id=root.name, point=point, handler=handler,
        priority=definition.get('priority', 100),
        timeout_seconds=sum(timeout for _, _, timeout, _, _ in commands) + 5,
        prewarm=prewarm,
        metadata={**definition.get('metadata', {}), 'origin': 'plugin', 'plugin_id': owner, 'content_digest': digest,
                  'definition_path': str(root / 'hook.json'), 'command_hook': True})
