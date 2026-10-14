"""Session-owned admission and delivery of user input."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping

from bianbu_agent.sessions.inbox import SessionInboxMessage


_TERMINAL = frozenset({"completed", "failed", "cancelled"})
logger = logging.getLogger(__name__)


class SessionInputConflictError(RuntimeError):
    """One stable message id was replayed with different input."""


class SessionInputBroker:
    """Serialize durable inbox admission with each Session's Run lifecycle."""

    def __init__(self, runtime: object) -> None:
        self._runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}
        self._watchers: dict[str, asyncio.Task[None]] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    @property
    def _store(self):
        store = getattr(getattr(self._runtime, "runs"), "_store", None)
        if store is None or not callable(getattr(store, "admit_input", None)):
            raise RuntimeError("Session inbox storage is unavailable")
        return store

    async def prompt(
        self,
        session_id: str,
        *,
        message_id: str,
        content: str,
        attachments: tuple[Mapping[str, object], ...] = (),
        skill_ids: tuple[str, ...] = (),
    ) -> dict[str, object]:
        owner = str(session_id).strip()
        await self._runtime.sessions.get(owner)
        message = SessionInboxMessage(
            message_id=message_id,
            content=content,
            attachments=attachments,
            skill_ids=skill_ids,
            created_at=time.time(),
        )
        async with self._lock(owner):
            try:
                await self._store.admit_input(owner, message)
            except ValueError as exc:
                raise SessionInputConflictError(str(exc)) from exc

            claimed_run = await self._store.input_run_id(owner, message.message_id)
            if claimed_run is not None:
                return self._response(message.message_id, "claimed", claimed_run)

            active = await self._active_run(owner)
            if active is not None and not message.skill_ids:
                try:
                    await self._runtime.runs.deliver_step_input(
                        active.request_id,
                    )
                except RuntimeError:
                    active = await self._active_run(owner)
                else:
                    self._watch(owner, str(active.request_id))
                    return self._response(
                        message.message_id,
                        "staged",
                        str(active.request_id),
                    )

            if active is not None:
                self._watch(owner, str(active.request_id))
                return self._response(
                    message.message_id,
                    "staged",
                    str(active.request_id),
                )

            run_id = await self._start_pending(owner)
            if run_id is not None:
                self._watch(owner, run_id)
            return self._response(message.message_id, "claimed", run_id)

    async def _active_run(self, session_id: str):
        records = await self._runtime.runs.list(session_id=session_id)
        for record in reversed(records):
            request = getattr(record, "request", {})
            metadata = (
                request.get("metadata", {})
                if isinstance(request, Mapping)
                else {}
            )
            if (
                isinstance(metadata, Mapping)
                and metadata.get("console_operation") == "generate_title"
            ):
                continue
            if str(getattr(record, "status", "")) not in _TERMINAL:
                reconciled = await self._runtime.runs.get_record(record.request_id)
                if (
                    reconciled is not None
                    and str(getattr(reconciled, "status", "")) not in _TERMINAL
                ):
                    return reconciled
        return None

    def _watch(self, session_id: str, run_id: str) -> None:
        current = self._watchers.get(session_id)
        if current is not None and not current.done():
            return
        self._watchers[session_id] = asyncio.create_task(
            self._wake_after_run(session_id, run_id),
            name=f"session-input-wake-{session_id}",
        )

    async def _wake_after_run(self, session_id: str, run_id: str) -> None:
        try:
            current_run_id: str | None = run_id
            while current_run_id is not None:
                handle = await self._runtime.runs.get(current_run_id)
                if handle is None:
                    return
                result = await handle.wait()
                async with self._lock(session_id):
                    active = await self._active_run(session_id)
                    current_run_id = (
                        str(active.request_id)
                        if active is not None
                        else await self._start_pending(session_id)
                    )
        except Exception:
            logger.exception("Session continuation stopped for %s", session_id)
        finally:
            current = self._watchers.get(session_id)
            if current is asyncio.current_task():
                self._watchers.pop(session_id, None)

    async def recover(self) -> None:
        """Wake durable pending input left idle by a prior process.

        Only Sessions the projection rows know about are woken.  A start may
        not read a transcript nobody has touched -- Harness builds a cell on
        first touch and never at startup -- so a Session without a row waits
        for the touch that folds it, which is also the touch that admits new
        input.
        """

        sessions = getattr(self._runtime, "sessions", None)
        if sessions is None or not callable(getattr(sessions, "list", None)):
            return
        agents = getattr(self._runtime, "agents", None)
        # 会话数远多于 Agent 数，而问一次 Agent 存在与否要把它的 agent.yaml 解
        # 一遍（K3 上 179 次 get 要 8s）。一次扫描里同一个 agent_id 只问一次。
        installable: dict[str, bool] = {}
        for session in await self._runtime.sessions.list():
            session_id = str(session.id)
            # 启动不读没人碰过的 transcript：没有投影行就是「还不知道」，留给
            # 首次触达去折、去恢复。丢表后的第一次启动正是这种情况——省下的
            # 是每个会话一份完整 transcript（K3 上 175 份、146s）。
            if not self._store.has_projection_row(session_id):
                logger.info(
                    "Deferring input recovery for session %s: "
                    "no projection row yet",
                    session_id,
                )
                continue
            # Sessions outlive uninstalled Agents. Keep their durable input,
            # but do not launch a nonexistent Worker during application startup.
            if agents is not None and session.agent_id:
                if session.agent_id not in installable:
                    try:
                        await agents.get(session.agent_id)
                    except KeyError:
                        installable[session.agent_id] = False
                    else:
                        installable[session.agent_id] = True
                if not installable[session.agent_id]:
                    logger.warning(
                        "Skipping input recovery for session %s: Agent %s is missing",
                        session_id, session.agent_id,
                    )
                    continue
            async with self._lock(session_id):
                active = await self._active_run(session_id)
                run_id = (
                    str(active.request_id)
                    if active is not None
                    else await self._start_pending(session_id)
                )
                if run_id is not None:
                    self._watch(session_id, run_id)

    async def close(self) -> None:
        """Stop terminal-run watchers during application shutdown."""

        tasks = tuple(self._watchers.values())
        self._watchers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _start_pending(self, session_id: str) -> str | None:
        snapshot = await self._store.pending_input(session_id)
        messages = snapshot.next_step
        if not messages:
            return None
        if messages[0].skill_ids:
            messages = messages[:1]
        else:
            skill_boundary = next(
                (
                    index for index, message in enumerate(messages)
                    if message.skill_ids
                ),
                len(messages),
            )
            messages = messages[:skill_boundary]
        session = await self._runtime.sessions.get(session_id)
        run_id = str(uuid.uuid4())
        await session.run(
            messages,
            request_id=run_id,
            idempotency_key=f"session-input:{messages[0].message_id}",
            idempotency_fingerprint=messages[0].message_id,
        )
        return run_id

    @staticmethod
    def _response(
        message_id: str,
        status: str,
        run_id: str | None,
    ) -> dict[str, object]:
        return {
            "accepted": True,
            "message_id": message_id,
            "status": status,
            "run_id": run_id,
        }


__all__ = ["SessionInputBroker", "SessionInputConflictError"]
