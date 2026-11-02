"""Server-owned publication lifecycle for log-only Session titles."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping


logger = logging.getLogger(__name__)


class SessionTitleManager:
    """Publish title revisions as Session log events, never as Runs.

    A title is an auxiliary, log-only fact: it owns no Run, takes no lease, and
    therefore cannot occupy a concurrency slot or trigger any Hook.  The
    deterministic fallback is appended first so a new Session is never
    untitled; the model-backed revision then supersedes it when it arrives.
    Newer work supersedes older work, and only the newest revision is visible.
    """

    def __init__(self, runtime: object) -> None:
        self._runtime = runtime
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._revisions: dict[str, int] = {}
        # Requests whose log event was already written with the fallback.
        self._recorded_requests: set[str] = set()

    def new_request_id(self) -> str:
        """Identity shared by every event of one title request.

        The fallback is written before the model-facing request exists, so a
        seq cannot identify the unit.  A random id minted by the endpoint is
        stamped on the fallback, the request, the route, and the provider
        revision, which lets the Trajectory group them exactly even when
        answers arrive out of order.
        """

        return f"title-{uuid.uuid4().hex}"

    async def publish_fallback(
        self,
        session_id: str,
        *,
        fallback: str,
        request_id: str,
        message_seq: int | None = None,
        request: Mapping[str, object] | None = None,
    ) -> str:
        """Append this request's fallback title before any model call.

        A Session may be retitled many times, and each request is its own
        Trajectory unit.  The fallback is therefore always the new request's
        deterministic text, never a previous revision.
        """

        selected = str(fallback or "").strip()
        if not selected:
            await self._runtime.sessions.get(session_id)
            return ""
        seqs = () if message_seq is None else (int(message_seq),)
        writer = getattr(self._runtime.sessions, "set_title_with_request", None)
        if request is not None and callable(writer):
            # One durable batch: the trajectory must not show the fallback
            # waiting for an unrelated Run before its own request appears.
            await writer(
                session_id,
                selected,
                request=request,
                message_seqs=seqs,
                source_kind="fallback",
                request_id=str(request_id),
            )
            self._recorded_requests.add(str(request_id))
            return selected
        await self._runtime.sessions.set_title(
            session_id,
            selected,
            message_seqs=seqs,
            source_kind="fallback",
            durable=True,
            request_id=str(request_id),
        )
        return selected

    def track(
        self,
        session_id: str,
        *,
        request_id: str,
        prompt: str,
        fallback: str,
        message_seq: int | None = None,
    ) -> None:
        """Start one detached model-backed revision for this Session."""

        owner = str(session_id)
        revision = self._revisions.get(owner, 0) + 1
        self._revisions[owner] = revision
        existing = self._tasks.get(owner)
        if existing is not None and not existing.done():
            existing.cancel()
        task = asyncio.create_task(
            self._publish(
                owner, revision, str(request_id), prompt, fallback, message_seq,
            ),
            name=f"session-title-{owner}-{revision}",
        )
        self._tasks[owner] = task

    async def _publish(
        self,
        session_id: str,
        revision: int,
        request_id: str,
        prompt: str,
        fallback: str,
        message_seq: int | None,
    ) -> None:
        try:
            runtime = self._runtime
            session = await runtime.sessions.get(session_id)
            # Record the request before dispatch: a title that times out or
            # fails still explains itself in the trajectory.
            log = getattr(runtime, "_run_store", None)
            request_event = None
            seqs = () if message_seq is None else (int(message_seq),)
            recorded = request_id in self._recorded_requests
            self._recorded_requests.discard(request_id)
            if not recorded and callable(getattr(log, "append_title_request", None)):
                from bianbu_agent.sessions.titles import title_request
                try:
                    request = title_request(prompt)
                except Exception:  # noqa: BLE001 - the generator reports it
                    request = {"system": "", "input": str(prompt)}
                request_event = await log.append_title_request(
                    session_id,
                    request,
                    message_seqs=seqs,
                    request_id=request_id,
                )
            result = await runtime.generate_session_title(
                prompt,
                agent_id=session.agent_id,
            )
            if request_event is not None and callable(
                getattr(log, "append_title_route", None)
            ):
                await log.append_title_route(
                    session_id,
                    dict(result.get("route") or {}),
                    request_id=request_id,
                )
            if revision != self._revisions.get(session_id):
                return
            title = str(result.get("title") or "").strip()
            if not title:
                logger.warning(
                    "Session title generation returned no text session=%s",
                    session_id,
                )
                return
            # A user rename after this request started pins the title.
            current = await runtime.sessions.title(session_id)
            if current and current != fallback:
                return
            await runtime.sessions.set_title(
                session_id,
                title,
                message_seqs=seqs,
                source_kind="provider",
                request_id=request_id,
            )
        except asyncio.CancelledError:
            raise
        except KeyError:
            # The Session was deleted while its title request was in flight.
            return
        except Exception:
            logger.exception(
                "Session title publication failed session=%s", session_id,
            )
        finally:
            if self._tasks.get(session_id) is asyncio.current_task():
                self._tasks.pop(session_id, None)

    async def cancel_session(self, session_id: str) -> None:
        selected = str(session_id)
        self._revisions.pop(selected, None)
        task = self._tasks.pop(selected, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        self._revisions.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = ["SessionTitleManager"]
