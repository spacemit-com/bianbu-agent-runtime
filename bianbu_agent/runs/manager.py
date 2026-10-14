from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from copy import deepcopy

from bianbu_agent.agents import AgentModel
from bianbu_agent._internal.change_signal import AsyncRevisionSignal
from bianbu_agent.runs.control import RunControlRegistry
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.models import ModelRegistry
from bianbu_agent.sessions.attachments import attachment_note
from bianbu_agent.plugins import (
    ContextPatch,
    DEFAULT_ACCOUNT_ID,
    HookPoint,
    RunContext,
    skill_unavailable_reason,
)
from bianbu_agent.plugins.hook_observation import observe_hooks
from bianbu_agent.runs import RunError, RunHandle, RunResult, Usage, encode_error
from bianbu_agent.sessions.inbox import SessionInboxMessage
from bianbu_agent.sessions.inbox import inbox_message_to_dict
from bianbu_agent.sessions.titles import (
    SessionTitleError,
    TITLE_TIMEOUT_SECONDS,
    fallback_session_title,
    generate_session_title,
    is_title_generation,
)
from bianbu_agent.observability import bind_log_context, log_run_terminal


EngineBuilder = Callable[..., object | Awaitable[object]]
logger = logging.getLogger(__name__)


async def _can_resume_model(engine: object, session_id: str, request_id: str, control=None) -> bool:
    probe = getattr(engine, "can_resume_model", None)
    if not callable(probe):
        return False
    pending = asyncio.create_task(probe(session_id, request_id))
    cancelled = asyncio.create_task(control.cancelled.wait()) if control is not None else None
    try:
        tasks = {pending, cancelled} if cancelled is not None else {pending}
        done, _ = await asyncio.wait(tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if cancelled is not None and control.cancelled.is_set():
            return False
        return bool(pending.result()) if pending in done else False
    except Exception:
        logger.exception("Unable to inspect model recovery checkpoint")
        return False
    finally:
        for task in (pending, cancelled):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (pending, cancelled) if task is not None),
                             return_exceptions=True)


def _agent_network_policy(agent_id: str) -> Mapping[str, object] | None:
    """Read the effective policy for an error explanation without changing execution."""
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return None
    # Mode is live (including the authenticated Worker socket); the startup
    # snapshot may still describe normal restrictions after switching to YOLO.
    try:
        from bianbu_agent.security.security_runtime import security_client_call

        mode = security_client_call("get_agent_security_mode", str(agent_id))
        if isinstance(mode, Mapping) and str(mode.get("mode", "")).lower() == "yolo":
            return None
    except Exception as exc:  # Older clients may not expose the mode method.
        logger.debug("Unable to read Agent mode for network diagnosis: %s", exc)
    if os.environ.get("BIANBU_WORKER_MANAGED") == "1":
        # Managed Workers cannot use the control-plane D-Bus connection. Even
        # if it were reachable, GetNetworkPolicy returns desired configuration,
        # which may not take effect until the next Worker start.
        import json

        try:
            value = json.loads(os.environ.get("BIANBU_WORKER_NETWORK_POLICY", ""))
        except (ValueError, TypeError):
            return None
        if isinstance(value, Mapping) and value.get("agent_id") == str(agent_id):
            return dict(value)
        return None
    try:
        from bianbu_agent.security.security_runtime import security_client_module

        value = security_client_module().get_network_policy(str(agent_id))
        return dict(value) if isinstance(value, Mapping) else None
    except Exception as exc:  # noqa: BLE001 - diagnostics must not hide the run error
        logger.info("Unable to read Agent network policy for error diagnosis: %s", exc)
        return None


def _agent_model_request(model: object) -> object:
    if isinstance(model, AgentModel):
        return {"provider": model.provider, "name": model.name}
    return model


def _selected_skill_ids(
    prompt: str | Sequence[SessionInboxMessage],
) -> tuple[str, ...]:
    if isinstance(prompt, str):
        return ()
    return tuple(
        dict.fromkeys(
            skill_id
            for message in prompt
            for skill_id in message.skill_ids
        )
    )


def _with_selected_skills(agent, generation, selected: tuple[str, ...]):
    if not selected:
        return agent
    missing = tuple(
        skill_id for skill_id in selected
        if skill_id not in generation.skills
        or skill_unavailable_reason(generation.skills[skill_id])
    )
    if missing:
        from bianbu_agent.security.security_errors import SkillUnavailableError

        raise SkillUnavailableError(missing)
    return replace(
        agent,
        skills=tuple(dict.fromkeys((*agent.skills, *selected))),
        requested_skills=tuple(dict.fromkeys(selected)),
    )


class RunManager:
    def __init__(self, *, plugin_manager: object, agent_manager: object,
                 model_registry: ModelRegistry, engine_builder: EngineBuilder,
                 store: object, controls: RunControlRegistry | None = None,
                 max_concurrent_runs: int | None = None) -> None:
        if max_concurrent_runs is not None and (
            isinstance(max_concurrent_runs, bool)
            or not isinstance(max_concurrent_runs, int)
            or max_concurrent_runs <= 0
        ):
            raise ValueError("max_concurrent_runs must be positive or None")
        self._plugins = plugin_manager
        self._agents = agent_manager
        self._models = model_registry
        self._engine_builder = engine_builder
        self._store = store
        self._controls = controls or RunControlRegistry()
        self._active: dict[str, RunHandle] = {}
        self._run_controls: dict[str, object] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._waiting: set[str] = set()
        self._delete_lock = asyncio.Lock()
        self._thread_execution_locks: dict[str, asyncio.Lock] = {}
        # A null limit is intentional: each Session has its own thread lock,
        # but independent Sessions must not wait behind a Worker-wide gate.
        # Deployments may still opt into a finite cap when the provider or
        # device has a known capacity.
        self._run_slots = (
            asyncio.Semaphore(max_concurrent_runs)
            if max_concurrent_runs is not None
            else None
        )
        self._closing = False
        self._changes = AsyncRevisionSignal()

    @property
    def change_revision(self) -> int:
        return self._changes.revision

    async def wait_for_change(self, after: int) -> int:
        return await self._changes.wait(after)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    async def run(
        self,
        session_id: str,
        prompt: str | Sequence[SessionInboxMessage],
        *,
                  agent_id: str | None = None,
                  account_id: str = DEFAULT_ACCOUNT_ID,
                  model: object | str | None = None,
                  request_id: str | None = None,
                  idempotency_key: str | None = None,
                  idempotency_fingerprint: str | None = None,
                  metadata: Mapping[str, object] | None = None,
                  thread_id: str | None = None) -> RunHandle:
        if self._closing:
            raise RuntimeError("RunManager is closing")
        if model is not None and (
            not isinstance(model, str) or bool(model.strip())
        ):
            raise ValueError("per-Run model override is not allowed")
        selected_account_id = str(account_id).strip()
        if not selected_account_id:
            raise ValueError("account_id must not be empty")
        metadata = dict(metadata or {})
        run_id = request_id or str(uuid.uuid4())

        lease = self._plugins.lease()
        generation = await lease.__aenter__()
        try:
            agent = await self._agents.materialize(
                agent_id, generation, allow_capability_gap=True
            )
            selected_model = self._models.resolve(agent.model or None)
            run_control = await self._controls.register(run_id, session_id)
            pending_input = getattr(self._store, "pending_input", None)

            async def load_step_input() -> tuple[SessionInboxMessage, ...]:
                if not callable(pending_input):
                    return ()
                messages = (await pending_input(session_id)).next_step
                ordinary_prefix = []
                for message in messages:
                    if message.skill_ids:
                        break
                    ordinary_prefix.append(message)
                return tuple(ordinary_prefix)

            run_control.bind_step_input_loader(load_step_input)
            await run_control.close_step_input_admission()
        except BaseException as exc:
            await lease.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        decisions: asyncio.Queue[tuple[str, Mapping[str, object]]] = asyncio.Queue()
        pending: dict[str, str] = {}

        async def route_control(action: str, payload: Mapping[str, object]) -> object:
            if action == "cancel":
                return await run_control.cancel()
            expected = pending.get("id", "")
            supplied = str(
                payload.get("approval_id")
                or payload.get("question_id")
                or ""
            )
            if action in {"approve", "reject", "answer"}:
                if not expected or supplied != expected:
                    return False
                await decisions.put((action, dict(payload)))
                return True
            return False

        handle = RunHandle(request_id=run_id, session_id=session_id,
                           store=self._store, control=route_control,
                           agent_id=agent.id, generation_id=generation.id,
                           on_committed=self._changes.notify)
        try:
            request = {
                "prompt": _prompt_projection(prompt),
                "idempotency_key": str(idempotency_key or ""),
                "idempotency_fingerprint": str(idempotency_fingerprint or ""),
                "agent_id": agent.id,
                "account_id": selected_account_id,
                "model": _agent_model_request(agent.model),
                "metadata": dict(metadata or {}),
                "skill_ids": list(_selected_skill_ids(prompt)),
                "generation_id": generation.id,
                "thread_id": str(thread_id or session_id),
            }
            if not isinstance(prompt, str):
                request["input_messages"] = [
                    inbox_message_to_dict(message) for message in prompt
                ]
            await handle.start(request)
        except BaseException:
            await self._controls.complete(run_id)
            await lease.__aexit__(None, None, None)
            raise
        self._active[run_id] = handle
        self._run_controls[run_id] = run_control
        with bind_log_context(
            agent_id=agent.id,
            session_id=session_id,
            run_id=run_id,
            plugin_generation=generation.id,
        ):
            task = asyncio.create_task(
                self._execute(
                    handle,
                    prompt,
                    agent=agent,
                    selected_model=selected_model,
                    generation=generation,
                    lease=lease,
                    control=run_control,
                    decisions=decisions,
                    pending=pending,
                    metadata=metadata,
                    account_id=selected_account_id,
                    thread_id=str(thread_id or session_id),
                ),
                name=f"bianbu-run-{run_id}",
            )
        self._tasks[run_id] = task
        return handle

    async def get(self, request_id: str) -> RunHandle | None:
        active = self._active.get(request_id)
        if active is not None:
            return active
        stored = await self._store.get(request_id)
        if stored is None:
            return None
        return await RunHandle.restore(stored, store=self._store)

    async def deliver_step_input(
        self,
        run_id: str,
    ) -> bool:
        control = self._run_controls.get(str(run_id))
        if control is None or str(run_id) not in self._active:
            raise RuntimeError("Run is not active")
        await control.notify_step_input()
        return True

    async def get_record(self, request_id: str) -> object | None:
        """Load one record and reconcile an unfinished orphan after restart."""

        selected = str(request_id)
        if selected in self._active:
            return await self._store.get(selected)
        stored = await self._store.get(selected)
        if stored is None:
            return None
        if getattr(stored, "result", None) is None:
            await RunHandle.restore(stored, store=self._store)
            return await self._store.get(selected)
        return stored

    async def list(self, session_id: str | None = None) -> list[object]:
        return await self._store.list(session_id=session_id)

    async def delete(self, request_id: str) -> bool:
        return await self.delete_many([request_id])

    async def delete_many(self, request_ids: list[str] | tuple[str, ...]) -> bool:
        """Atomically reject undeletable groups before removing any persisted Run."""

        selected = tuple(dict.fromkeys(str(item).strip() for item in request_ids))
        if not selected or any(not item for item in selected):
            return False
        async with self._delete_lock:
            if any(request_id in self._active for request_id in selected):
                return False
            records = await asyncio.gather(
                *(self._store.get(request_id) for request_id in selected)
            )
            if any(record is None for record in records):
                return False
            for request_id in selected:
                if not await self._store.delete(request_id):
                    return False
            self._changes.notify()
            return True

    async def delete_session(self, session_id: str) -> int:
        async with self._delete_lock:
            await self._controls.cancel_session(session_id)
            tasks = [
                task
                for request_id, task in self._tasks.items()
                if self._active[request_id].session_id == session_id
            ]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            deleted = await self._store.delete_session(session_id)
            if deleted:
                self._changes.notify()
            return deleted

    async def close(self) -> None:
        self._closing = True
        for request_id in tuple(self._active):
            await self._controls.cancel_request(request_id)
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks.values()), return_exceptions=True)

    async def _execute(self, handle: RunHandle, prompt: str | Sequence[SessionInboxMessage], *,
                       agent, selected_model, generation, lease, control,
                       decisions: asyncio.Queue[tuple[str, Mapping[str, object]]],
                       pending: dict[str, str],
                       metadata: Mapping[str, object] | None,
                       account_id: str,
                       thread_id: str) -> None:
        async def observe_hook(event: dict[str, object]) -> None:
            phase = str(event.get("phase") or "")
            kind = {
                "invoked": "hook_invoked",
                "result": "hook_result",
            }.get(phase)
            if kind is not None:
                await handle.publish(RunEvent(
                    kind,
                    {key: value for key, value in event.items() if key != "phase"},
                ))

        async def execute_admitted() -> None:
            with observe_hooks(observe_hook):
                await self._execute_admitted(
                    handle,
                    prompt,
                    agent=agent,
                    selected_model=selected_model,
                    generation=generation,
                    lease=lease,
                    control=control,
                    decisions=decisions,
                    pending=pending,
                    metadata=metadata,
                    account_id=account_id,
                    thread_id=thread_id,
                )

        if self._run_slots is None:
            await execute_admitted()
            return

        admission = asyncio.create_task(self._run_slots.acquire())
        cancelled = asyncio.create_task(control.cancelled.wait())
        done, _ = await asyncio.wait(
            {admission, cancelled},
            return_when=asyncio.FIRST_COMPLETED,
        )
        slot_acquired = admission in done and bool(admission.result())
        if admission not in done:
            cancellation_requested = admission.cancel()
            await asyncio.gather(admission, return_exceptions=True)
            if not cancellation_requested and not admission.cancelled():
                slot_acquired = bool(admission.result())
        admitted = slot_acquired and not control.cancelled.is_set()
        if slot_acquired and not admitted:
            self._run_slots.release()
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        try:
            await execute_admitted()
        finally:
            if admitted:
                self._run_slots.release()

    async def _execute_admitted(self, handle: RunHandle, prompt: str | Sequence[SessionInboxMessage], *,
                       agent, selected_model, generation, lease, control,
                       decisions: asyncio.Queue[tuple[str, Mapping[str, object]]],
                       pending: dict[str, str],
                       metadata: Mapping[str, object] | None,
                       account_id: str,
                       thread_id: str) -> None:
        run_id, session_id = handle.request_id, handle.session_id
        result: RunResult | None = None
        after_run_invoked = False
        hook_metadata = dict(metadata or {})
        # Pass the immutable role-level Hook snapshot to every Hook.  Memory
        # Hooks use this to distinguish an explicitly disabled role from a
        # direct/unit-test invocation where the legacy environment switch is
        # still authoritative.
        hook_metadata["hook_ids"] = tuple(agent.hooks)
        prompt_text = _prompt_projection(prompt)
        thread_lock = self._thread_execution_locks.setdefault(
            thread_id,
            asyncio.Lock(),
        )
        thread_lock_acquired = False

        def public_metadata() -> dict[str, object]:
            """Keep the internal Hook-selection snapshot out of Run results."""

            return {
                key: value for key, value in hook_metadata.items()
                if key != "hook_ids"
            }

        def hook_context(
            request: str = prompt_text,
            *,
            response: str = "",
            status: str = "",
        ) -> RunContext:
            return RunContext(
                run_id=run_id,
                session_id=session_id,
                agent_id=agent.id,
                request=request,
                metadata=hook_metadata,
                response=response,
                status=status,
                account_id=account_id,
            )

        from bianbu_agent.skills.versions import task_skills
        from bianbu_agent.agents.selection import task_agent
        agent_token = task_agent.set(agent)
        skill_token = task_skills.set(generation.skills)
        engine = None
        try:
            agent = _with_selected_skills(
                agent,
                generation,
                _selected_skill_ids(prompt),
            )
            task_skills.set({name: generation.skills[name] for name in agent.skills
                             if name in generation.skills})
            if control.cancelled.is_set():
                result = RunResult(request_id=run_id, status="cancelled")
                return
            if is_title_generation(dict(metadata or {})):
                try:
                    async with asyncio.timeout(TITLE_TIMEOUT_SECONDS):
                        for other_id, other_handle in tuple(self._active.items()):
                            if other_id == run_id:
                                continue
                            other_record = await self._store.get(other_id)
                            other_request = dict(
                                getattr(other_record, "request", {}) or {}
                            )
                            if is_title_generation(
                                other_request.get("metadata")
                            ):
                                continue
                            await other_handle.wait()
                            if control.cancelled.is_set():
                                result = RunResult(
                                    request_id=run_id,
                                    status="cancelled",
                                )
                                return
                        title = await generate_session_title(
                            selected_model,
                            prompt_text,
                            timeout_seconds=TITLE_TIMEOUT_SECONDS,
                        )
                except (SessionTitleError, TimeoutError) as exc:
                    # The title is an auxiliary convenience, never a reason to
                    # fail a Run: reasoning-only or truncated title answers and
                    # unresponsive title models keep the deterministic title the
                    # Console already shows.
                    logger.warning(
                        "session title fell back to the message text: %s",
                        exc or "session title generation timed out",
                    )
                    title = fallback_session_title(prompt_text)
                if control.cancelled.is_set():
                    result = RunResult(request_id=run_id, status="cancelled")
                    return
                await handle.publish(RunEvent("token", {"text": title}))
                result = RunResult(
                    request_id=run_id,
                    status="completed",
                    content=title,
                    metadata=public_metadata(),
                )
                return
            await thread_lock.acquire()
            thread_lock_acquired = True
            # BEFORE_RUN Hooks only patch the request (context sections,
            # metadata, direct replies); they never change the engine's system
            # prompt, tools, or graph, so the build overlaps with them instead
            # of queueing behind them.  _build_engine keeps that promise by
            # keeping a synchronous builder off the loop: run inline it owned
            # the loop thread for its whole duration, and the Hook tasks
            # created just below could not even start until it returned.  A
            # denying gate Hook still stops the Run before the first token and
            # the in-flight build is discarded.
            engine_task = asyncio.ensure_future(
                _build_engine(
                    self._engine_builder, agent, generation, selected_model,
                )
            )
            try:
                before_run = await _run_hooks(
                    generation,
                    HookPoint.BEFORE_RUN, hook_context(), agent.hooks
                )
            except BaseException:
                engine_task.cancel()
                await asyncio.gather(engine_task, return_exceptions=True)
                raise
            hook_metadata.update(before_run.metadata)
            engine = await engine_task
            await self._publish_request_context(
                handle,
                engine=engine,
                agent_id=agent.id,
                model=selected_model,
            )
            if before_run.direct_reply is not None:
                if control.cancelled.is_set():
                    result = RunResult(
                        request_id=run_id,
                        status="cancelled",
                    )
                    return
                await handle.publish(
                    RunEvent("token", {"text": before_run.direct_reply})
                )
                result = RunResult(
                    request_id=run_id,
                    status="completed",
                    content=before_run.direct_reply,
                    metadata=public_metadata(),
                )
                await _run_hooks(
                    generation,
                    HookPoint.AFTER_RUN,
                    hook_context(
                        response=before_run.direct_reply,
                        status="completed",
                    ),
                    agent.hooks,
                )
                after_run_invoked = True
                return
            after_context = await _run_hooks(
                generation,
                HookPoint.AFTER_CONTEXT, hook_context(), agent.hooks
            )
            hook_metadata.update(after_context.metadata)
            sections = tuple(
                section
                for patch in (before_run, after_context)
                for section in patch.context_sections
            )
            prepared = _prepare_prompt(prompt, [part for part in sections if part])

            async def filter_model_input(text: str) -> str:
                patch = await _run_hooks(
                    generation, HookPoint.BEFORE_MODEL,
                    hook_context(text), agent.hooks,
                )
                hook_metadata.update(patch.metadata)
                from bianbu_agent.security.security_middleware import extract_security_metadata
                security = extract_security_metadata({"metadata": patch.metadata})
                masked = security.get("dlp_masked_prompt")
                if masked is not None:
                    if not isinstance(masked, str):
                        raise PermissionError("DLP model input replacement must be text")
                    text = masked
                return "\n\n".join([*patch.context_sections, text])

            # Filter each immutable inbox item independently, preserving IDs,
            # attachments and skill references used by checkpoint deduplication.
            if isinstance(prepared, str):
                prepared = await filter_model_input(prepared)
            else:
                prepared = [replace(item, content=await filter_model_input(item.content))
                            for item in prepared]
            prepared_text = _prompt_projection(prepared)
            content: list[str] = []
            model_content_start = 0
            input_tokens = 0
            output_tokens = 0
            usage_events = 0
            next_prompt: str | Sequence[SessionInboxMessage] | None = prepared
            stream_parameters = inspect.signature(engine.stream).parameters
            stream_accepts_account_id = (
                "account_id" in stream_parameters
                or any(
                    parameter.kind is inspect.Parameter.VAR_KEYWORD
                    for parameter in stream_parameters.values()
                )
            )
            raw_model_timeout = getattr(
                selected_model,
                "request_timeout_seconds",
                None,
            )
            model_event_timeout = (
                float(raw_model_timeout)
                if isinstance(raw_model_timeout, (int, float))
                and not isinstance(raw_model_timeout, bool)
                and float(raw_model_timeout) > 0
                else None
            )
            # Approval resumes may execute already-announced tool calls
            # without emitting a second tool_start event.
            active_tool_calls: set[str] = set()
            while result is None:
                await control.open_step_input_admission()
                flush_session = getattr(self._store, "flush_session", None)
                if callable(flush_session):
                    await flush_session(session_id)
                stream_kwargs = {
                    "session_id": thread_id,
                    "product_session_id": session_id,
                    "control": control,
                }
                if stream_accepts_account_id:
                    stream_kwargs["account_id"] = account_id
                event_stream = engine.stream(next_prompt, **stream_kwargs)
                event_iterator = event_stream.__aiter__()
                model_response_pending = not active_tool_calls
                model_timed_out = False
                try:
                    while True:
                        model_deadline = None
                        try:
                            if (
                                model_event_timeout is not None
                                and model_response_pending
                            ):
                                async with control.model_deadline(model_event_timeout) as model_deadline:
                                    event = await anext(event_iterator)
                            else:
                                event = await anext(event_iterator)
                        except StopAsyncIteration:
                            break
                        except TimeoutError as exc:
                            if model_deadline is None or not model_deadline.expired():
                                # A tool/provider owns its own timeout; do not
                                # relabel it as expiry of the model idle clock.
                                raise
                            model_timed_out = True
                            break
                        await handle.publish(event)
                        if callable(flush_session) and event.kind in {
                            "tool_start", "tool_end",
                        }:
                            # Match Harness semantic checkpoints: the call is durable
                            # before its effect may begin, and its result is durable
                            # before the graph advances to another model step.
                            await flush_session(session_id)
                        if event.kind == "token":
                            content.append(str(event.payload.get("text") or ""))
                        elif event.kind == "usage":
                            input_tokens += max(
                                0, int(event.payload.get("prompt_tokens") or 0)
                            )
                            output_tokens += max(
                                0, int(event.payload.get("completion_tokens") or 0)
                            )
                            usage_events += 1
                        if event.kind == "tool_start":
                            active_tool_calls.add(str(event.payload.get("call_id") or ""))
                            model_response_pending = False
                        elif event.kind == "tool_end":
                            active_tool_calls.discard(str(event.payload.get("call_id") or ""))
                            model_response_pending = not active_tool_calls
                            if not active_tool_calls:
                                model_content_start = len(content)
                        elif event.kind in {"token", "reasoning", "usage"}:
                            model_response_pending = not active_tool_calls
                        elif event.kind in {"approval_required", "done"}:
                            model_response_pending = False
                        if event.kind == "approval_required":
                            if control.approval_pending():
                                # 引擎把 Run 停在审批中间件里，决定在当前 Run 内解析。
                                if await _resolve_tool_approval(
                                    event,
                                    decisions=decisions,
                                    pending=pending,
                                    control=control,
                                    waiting=self._waiting,
                                    handle=handle,
                                    run_id=run_id,
                                    session_id=session_id,
                                ) == "cancel":
                                    result = RunResult(
                                        request_id=run_id, status="cancelled"
                                    )
                                    break
                            else:
                                # A tool approval is resolved on the Run that
                                # asked for it; an engine that ends the Run
                                # there has nowhere to hand the decision back.
                                raise RuntimeError(
                                    "engine ended a tool approval without "
                                    "parking the Run"
                                )
                        if control.cancelled.is_set():
                            result = RunResult(
                                request_id=run_id,
                                status="cancelled",
                            )
                            break
                finally:
                    close_stream = getattr(event_iterator, "aclose", None)
                    if callable(close_stream):
                        await close_stream()
                if result is None and control.cancelled.is_set():
                    result = RunResult(request_id=run_id, status="cancelled")
                if model_timed_out and result is None:
                    if (not active_tool_calls
                            and control.model_recovery.get("attempts", 0) < 2
                            and await _can_resume_model(engine, thread_id, run_id, control)):
                        attempt = control.model_recovery.get("attempts", 0) + 1
                        control.model_recovery["attempts"] = attempt
                        discarded_text = "".join(content[model_content_start:])
                        del content[model_content_start:]
                        await handle.publish(RunEvent("progress", {
                            "phase": "recovering", "reason": "model_timeout",
                            "discarded_text": discarded_text,
                            "attempt": attempt, "max_attempts": 2,
                            "message": "模型响应超时，正在从已保存的步骤分步继续；不会重放已完成的工具。",
                        }))
                        next_prompt = None
                        continue
                    if control.cancelled.is_set():
                        raise asyncio.CancelledError
                    if control.model_recovery.get("attempts", 0) >= 2:
                        from bianbu_agent.runs.recovery import ModelRecoveryExhausted
                        # diagnostic 会出现在用户看到的失败消息里，用中文说清
                        # 「谁没响应、等了多久」；上面那句英文只作为分类标记。
                        raise ModelRecoveryExhausted("model_timeout",
                            control.model_recovery["attempts"],
                            f"模型服务在 {model_event_timeout:g} 秒内没有响应")
                    raise RuntimeError(f"model response timed out after {model_event_timeout:g}s")
                break
            if result is None:
                text = "".join(content)
                after_model = await _run_hooks(
                    generation,
                    HookPoint.AFTER_MODEL, hook_context(prepared_text), agent.hooks
                )
                hook_metadata.update(after_model.metadata)
                result = RunResult(
                    request_id=run_id,
                    status="completed",
                    content=text,
                    usage=(
                        Usage(
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            total_tokens=input_tokens + output_tokens,
                            metadata={"events": usage_events},
                        )
                        if usage_events
                        else None
                    ),
                    metadata=public_metadata(),
                )
                await _run_hooks(
                    generation,
                    HookPoint.AFTER_RUN,
                    hook_context(
                        request=prompt_text,
                        response=text,
                        status="completed",
                    ),
                    agent.hooks,
                )
                after_run_invoked = True
        except asyncio.CancelledError:
            await control.close_step_input_admission()
            result = RunResult(request_id=run_id, status="cancelled")
        except Exception as exc:
            await control.close_step_input_admission()
            hook_metadata["error"] = str(exc)
            try:
                await _run_hooks(
                    generation, HookPoint.ON_ERROR, hook_context(), agent.hooks
                )
            except Exception:
                pass
            # 区分「被 securityd 拦下」与「跑挂了」：前者一律 run_failed 时，
            # 前端只能显示通用文案「发送失败，请稍后再试」，让用户以为是系统
            # 故障、重试可能会好 —— 实际是没有授权，重试一万次也一样，
            # 而且不知道缺哪条能力、该去哪里授权。
            from bianbu_agent.security.security_errors import classify_run_exception

            err = classify_run_exception(
                exc,
                agent_id=agent.id,
                provider_base_url=getattr(selected_model, "base_url", ""),
                network_policy=_agent_network_policy(agent.id),
            )
            from bianbu_agent.runs.recovery import ModelRecoveryExhausted
            if isinstance(exc, ModelRecoveryExhausted):
                try:
                    resumable = await _can_resume_model(engine, thread_id, run_id, control)
                except asyncio.CancelledError:
                    result = RunResult(request_id=run_id, status="cancelled")
                    return
                err = exc.error_payload(resumable=resumable)
            if control.cancelled.is_set():
                result = RunResult(request_id=run_id, status="cancelled")
                return
            error = RunError(code=err["code"], message=err["message"],
                             details=err["details"],
                             retryable=bool(err.get("retryable", True)))
            result = RunResult(request_id=run_id, status="failed", error=error)
            try:
                # 失败过程中的诊断与终态跑同一个编码，客户端不必处理两种形状。
                await handle.publish(RunEvent("error", encode_error(error)))
            except Exception:
                pass
        finally:
            try:
                result = result or RunResult(
                    request_id=run_id, status="failed",
                    error=RunError(
                        code="run_incomplete",
                        message="Run ended without result",
                    ),
                )
                if not after_run_invoked and result.status == "completed":
                    try:
                        await _run_hooks(
                            generation,
                            HookPoint.AFTER_RUN,
                            hook_context(
                                request=prompt_text,
                                response=result.content,
                                status=result.status,
                            ),
                            agent.hooks,
                        )
                    except Exception:
                        # Memory persistence is best-effort and cannot alter
                        # the terminal Runtime result.
                        pass
                await control.close_step_input_admission()
                await handle.finish(result)
                if os.environ.get("BIANBU_WORKER_MANAGED") != "1":
                    log_run_terminal(
                        logger,
                        status=result.status,
                        error_code=(result.error.code if result.error is not None else ""),
                    )
                await self._controls.complete(run_id)
                await lease.__aexit__(None, None, None)
                self._active.pop(run_id, None)
                self._run_controls.pop(run_id, None)
                self._tasks.pop(run_id, None)
            finally:
                task_agent.reset(agent_token)
                task_skills.reset(skill_token)
                if thread_lock_acquired:
                    thread_lock.release()

    async def _publish_request_context(
        self,
        handle: RunHandle,
        *,
        engine: object,
        agent_id: str,
        model: object,
    ) -> None:
        """Log only changed model-visible request context for reconstruction."""

        system_prompt = getattr(engine, "system_prompt", None)
        if system_prompt is None:
            return
        current: dict[str, object] = {
            "agent_id": str(agent_id),
            "provider": str(getattr(model, "provider_id", "") or ""),
            "model": str(getattr(model, "model", "") or ""),
            "system_prompt": str(system_prompt),
        }
        previous: dict[str, object] | None = None
        reader = getattr(self._store, "read_session_events", None)
        if callable(reader):
            for event in reversed(await reader(handle.session_id)):
                if getattr(event, "type", "") == "request/context":
                    previous = {
                        key: value
                        for key, value in dict(event.data).items()
                        if key != "reason"
                    }
                    break
        if previous == current:
            return
        await handle.publish(RunEvent(
            "request_context",
            {
                "reason": "initial" if previous is None else "change",
                **current,
            },
        ))


async def _run_hooks(
    generation,
    point: HookPoint,
    context: RunContext,
    hook_ids: tuple[str, ...],
) -> ContextPatch:
    runner = getattr(generation, "run_hooks", None)
    if not callable(runner):
        return ContextPatch()
    if "hook_ids" in inspect.signature(runner).parameters:
        return await runner(point, context, hook_ids=hook_ids)
    return await runner(point, context)


async def _build_engine(
    builder: Callable[..., object],
    agent: object,
    generation: object,
    model: object,
) -> object:
    """Build the engine as its own task, off the event loop when it is sync.

    ``build_default_engine`` is synchronous: skill DLP over the securityd UDS,
    tool adaptation and graph construction.  Running that on the loop thread
    starves every other task for its whole duration, so the BEFORE_RUN Hook
    tasks — which mostly *wait* on Hook subprocesses and therefore make
    progress alongside a worker thread — only started once the build returned.
    Handing a sync builder to a worker thread makes the overlap real; async
    builders stay on the loop and are awaited exactly as before.
    """

    if inspect.iscoroutinefunction(builder):
        built = await builder(agent, generation, model)
    else:
        built = await asyncio.to_thread(builder, agent, generation, model)
    return await built if inspect.isawaitable(built) else built


async def _wait_for_resume(
    event: RunEvent,
    *,
    decisions: asyncio.Queue[tuple[str, Mapping[str, object]]],
    pending: dict[str, str],
    control,
    waiting: set[str],
    run_id: str,
    timeout_seconds: float | None = None,
) -> tuple[str, Mapping[str, object]]:
    key = "approval_id"
    pending_id = str(event.payload.get(key) or "")
    if not pending_id:
        raise ValueError(f"{event.kind} event is missing {key}")
    pending["id"] = pending_id
    waiting.add(run_id)
    try:
        decision_task = asyncio.create_task(decisions.get())
        cancelled_task = asyncio.create_task(control.cancelled.wait())
        wait_set = {decision_task, cancelled_task}
        done, remaining = await asyncio.wait(
            wait_set,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        pending.pop("id", None)
        pending["resolved_id"] = pending_id
        if not done:
            return "timeout", {
                "reason": f"approval timed out after {timeout_seconds:g}s"
                if timeout_seconds is not None
                else "approval timed out"
            }
        if cancelled_task in done:
            return "cancel", {}
        return decision_task.result()
    finally:
        waiting.discard(run_id)


async def _resolve_tool_approval(
    event: RunEvent,
    *,
    decisions: "asyncio.Queue[tuple[str, Mapping[str, object]]]",
    pending: dict[str, str],
    control,
    waiting: set[str],
    handle,
    run_id: str,
    session_id: str,
) -> str:
    """Hand one tool-approval decision to the graph parked on this Run.

    The Run is not restarted.  The engine is blocked inside its approval
    middleware, so resolving the decision resumes the same graph -- which is
    exactly why no checkpoint has to be written down.
    """

    action, payload = await _wait_for_resume(
        event,
        decisions=decisions,
        pending=pending,
        control=control,
        waiting=waiting,
        run_id=run_id,
    )
    if action == "cancel":
        return "cancel"
    actions = event.payload.get("actions")
    if action == "approve":
        _record_securityd_approvals(actions, session_id=session_id, run_id=run_id)
    await handle.publish(
        RunEvent(
            "approval_resolved",
            {
                "approval_id": pending.pop("resolved_id", ""),
                "decision": "approve" if action == "approve" else "reject",
            },
        )
    )
    if not control.resolve_approval(
        _tool_approval_decisions(actions, action, payload)
    ):
        raise RuntimeError("tool approval was no longer pending on this Run")
    return "resolved"


def _tool_approval_decisions(
    actions: object,
    action: str,
    payload: Mapping[str, object],
) -> list[dict[str, object]]:
    """The per-call decisions the approval middleware expects."""

    item: dict[str, object] = {
        "type": "approve" if action == "approve" else "reject"
    }
    reason = str(payload.get("reason") or "").strip()
    if reason:
        item["message"] = reason
    count = len(actions) if isinstance(actions, list) else 1
    return [dict(item) for _ in range(max(count, 1))]


def _record_securityd_approvals(
    actions: object,
    *,
    session_id: str,
    run_id: str,
) -> None:
    """Remember the human's approval so an identical call is not re-asked."""

    if not isinstance(actions, list):
        return
    try:
        from bianbu_agent.security.security_middleware import (
            securityd_record_approval,
        )

        for approved_action in actions:
            if not isinstance(approved_action, Mapping):
                continue
            arguments = approved_action.get("arguments")
            tool_metadata = approved_action.get("tool_metadata")
            securityd_record_approval(
                tool_name=str(approved_action.get("name") or ""),
                args=(arguments if isinstance(arguments, Mapping) else {}),
                session_id=session_id,
                run_id=run_id,
                tool_metadata=(
                    tool_metadata if isinstance(tool_metadata, Mapping) else {}
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("记录 HITL 批准失败：%s", exc)


def _prompt_projection(prompt: str | Sequence[SessionInboxMessage]) -> str:
    if isinstance(prompt, str):
        return prompt
    return "\n\n".join(item.content for item in prompt)


def _prepare_prompt(
    prompt: str | Sequence[SessionInboxMessage],
    context_sections: Sequence[str],
) -> str | list[SessionInboxMessage]:
    if isinstance(prompt, str):
        return "\n\n".join(
            [*context_sections, f"## 当前请求\n{prompt}"]
        )
    prepared = list(prompt)
    for index, item in enumerate(prepared):
        note = attachment_note(item.attachments)
        if note:
            prepared[index] = replace(
                item,
                content="\n\n".join([item.content, note]),
            )
    if context_sections and prepared:
        prepared[0] = replace(
            prepared[0],
            content="\n\n".join([*context_sections, prepared[0].content]),
        )
    return prepared


__all__ = ["RunManager"]
