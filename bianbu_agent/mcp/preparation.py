"""Prepare MCP servers in the background so nothing waits on a slow first start.

Registering an MCP server used to mean vendoring its npm tree and starting it
once, synchronously, inside ``mcp add``.  That made the command an npm client:
on this board the first install of ``bilibili-mcp-js`` downloads 116MB and took
over a minute, while AgentHub kills any Agent CLI call after 30 seconds.  The
kill left a half-made staging directory and an unfinished install transaction
behind, and from then on every later install was refused.

Deciding whether a server can start belongs to the Runtime, which does it on
every Worker start with no caller waiting.  This manager owns that work: a
server whose package still has to be downloaded is reported as ``preparing``,
installed and started in the background, and becomes part of the next
Generation once it answers.

Starting a server is also how its tools become known, and the Control plane --
which never runs third-party code -- has to be able to show them for a server
no Agent selected yet.  A successful probe therefore leaves a record next to
the vendored tree, keyed by the launcher it proved; a later process answers
``ready`` with that tool list instead of starting the server again.  Only a
declaration that changed, or one that never answered, is started again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from bianbu_agent.home import AgentHome
from bianbu_agent.mcp.config import MCPServerConfig
from bianbu_agent.mcp.install import pin_npm_dependency, vendored_pinned_definition
from bianbu_agent.mcp.lifecycle import MCP_PHASE_INSTALLING, MCP_PHASE_STARTING
from bianbu_agent.mcp.managed import (
    ManagedMCPDefinition,
    load_managed_mcp_config,
    npx_package_invocation,
)
from bianbu_agent.mcp.readiness import (
    MCPProbeResult,
    failure_detail,
    probe_mcp_server,
)
from bianbu_agent.mcp.vendor import remove_vendored_server


logger = logging.getLogger(__name__)


#: The server is being vendored and/or started for the first time.
MCP_PREPARING = "preparing"
#: The server answered a handshake; its tools are known.
MCP_PREPARED = "ready"
#: Preparation failed; ``detail`` is the reason an operator can act on.
MCP_PREPARATION_FAILED = "failed"

#: A server that is being prepared by a reload can wait this long for the other
#: servers of the same start to finish, so one generation covers all of them.
RELOAD_DEBOUNCE_SECONDS = 0.2

#: Retries inside one preparation. A transport that never opened is retried; a
#: server that starts and answers with an error is not.
_PROBE_ATTEMPTS = 3

#: What a successful probe left behind, so the next process can answer for this
#: server without starting it: the launcher it proved, the tools it listed and
#: when that happened.
_RECORD_FILE = "prepared.json"


@dataclass(frozen=True, slots=True)
class _PreparedRecord:
    signature: str
    tools: tuple[str, ...]
    checked_at: float


@dataclass(frozen=True, slots=True)
class MCPPreparationState:
    mcp_id: str
    status: str
    detail: str = ""
    tools: tuple[str, ...] = ()
    vendored: bool = False
    #: What preparation is doing while ``status`` is ``preparing``, so a UI can
    #: say "installing dependencies" instead of only "preparing" for minutes.
    phase: str = ""
    #: When the handshake behind this state happened, for a state that came out
    #: of a record rather than out of this process.
    checked_at: float | None = None
    #: Whether a launcher for this declaration exists right now.  A Generation
    #: only has to wait for a declaration that still has nothing to run; a
    #: declaration whose background probe failed is still connectable, and the
    #: Generation that tries it reports its own, better-informed verdict.
    launchable: bool = False

    @property
    def ready(self) -> bool:
        return self.status == MCP_PREPARED

    @property
    def preparing(self) -> bool:
        return self.status == MCP_PREPARING


class MCPPreparationManager:
    """Own one background preparation per declared MCP server."""

    #: Name this manager is published under as a Plugin service.
    service_name = "mcp_preparation"

    def __init__(
        self,
        home: AgentHome,
        *,
        reload: Callable[[], Awaitable[object]] | None = None,
        verify: bool = True,
        prepare: (
            Callable[[ManagedMCPDefinition], tuple[ManagedMCPDefinition, Path | None]]
            | None
        ) = None,
        probe: Callable[[MCPServerConfig], Awaitable[MCPProbeResult]] | None = None,
        reload_debounce_seconds: float = RELOAD_DEBOUNCE_SECONDS,
    ) -> None:
        self._home = home
        self._reload = reload
        self._verify = bool(verify)
        self._prepare_definition = prepare or self._pin_definition
        self._probe_config = probe or self._probe_server
        self._reload_debounce_seconds = float(reload_debounce_seconds)
        self._states: dict[str, MCPPreparationState] = {}
        self._launchers: dict[str, ManagedMCPDefinition] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._reload_task: asyncio.Task[None] | None = None
        self._reload_pending = False
        self._closed = False

    # -- reads -------------------------------------------------------------

    def state(self, mcp_id: str) -> MCPPreparationState | None:
        return self._states.get(str(mcp_id))

    def states(self) -> dict[str, MCPPreparationState]:
        return dict(self._states)

    def config_for(self, mcp_id: str, config: MCPServerConfig) -> MCPServerConfig:
        """``config`` with the launcher of its prepared local installation.

        A declaration stays an ``npx`` declaration on disk: what this Runtime
        runs is a local launch of the package it names, and only this manager
        knows which tree that is.
        """

        launcher = self._launchers.get(str(mcp_id))
        if launcher is None or not launcher.executable:
            return config
        return replace(config, command=launcher.executable, args=launcher.args)

    # -- control -----------------------------------------------------------

    def ensure(
        self,
        mcp_id: str,
        config: MCPServerConfig,
        *,
        source: Path | None = None,
        probe: bool = True,
    ) -> MCPPreparationState:
        """Start preparing ``config`` in the background and report its state.

        Calling this twice for one server returns the state of the first call:
        one server is prepared once per process.  The caller is expected to ask
        again after the reload this manager triggers when the work settles.

        A server that answered a handshake before -- the state a record keeps
        -- is ``ready`` with its tools immediately and is not started again.
        ``probe`` starts a server that has never answered in the background so
        its tools are known before a Generation connects it; a caller that is
        about to connect it itself passes ``probe=False``.

        ``source`` is the declaration this server was loaded from.  A
        preparation that outlives its declaration drops what it downloaded
        instead of publishing a tree nothing points at.
        """

        key = str(mcp_id)
        existing = self._states.get(key)
        if existing is not None:
            if not existing.ready or self._launcher_exists(key):
                return existing
            # The installation this server was resolved to is gone (an
            # uninstall removed it, or an upgrade replaced it): resolve again
            # rather than starting a path that no longer exists.
            self.forget(key)
        definition = _managed_definition(key, config)
        pinned: ManagedMCPDefinition | None = None
        vendored = False
        if definition.npm_package:
            resolved = vendored_pinned_definition(definition, self._home)
            if resolved is not None:
                pinned, _root = resolved
                vendored = True
                self._launchers[key] = pinned
        if pinned is None and definition.npm_package:
            # Nothing local to launch yet: installing the package is the whole
            # wait, so the server stays ``preparing`` until it can be started.
            return self._start_background(key, definition, source)
        # A record only answers for a declaration that can actually start: an
        # npm declaration whose tree disappeared still has to be installed, so
        # its record (if any) describes an installation that is gone.
        record = self._read_record(key, definition)
        state = MCPPreparationState(
            mcp_id=key,
            status=MCP_PREPARED,
            tools=() if record is None else record.tools,
            vendored=vendored,
            checked_at=None if record is None else record.checked_at,
            launchable=True,
        )
        self._states[key] = state
        if record is None and probe:
            # Nothing has proved this launcher starts -- it is new, or the
            # declaration changed.  Start it once, off the critical path, so the
            # tools of a Server no Generation connects yet are still known.
            #
            # A caller that is about to connect the server itself passes
            # ``probe=False``: a second instance would fight the first one, and
            # that Generation is a better witness than a probe can be.
            self._start_probe(
                key, definition, source, pinned if pinned is not None else definition
            )
        return state

    def _record_path(self, mcp_id: str) -> Path:
        return self._home.mcp_runtime_dir / str(mcp_id) / _RECORD_FILE

    def _definition_signature(self, definition: ManagedMCPDefinition) -> str:
        """What a record has to match to still describe this declaration."""

        payload = "\n".join((
            str(definition.transport),
            str(definition.executable),
            *definition.args,
            str(definition.url),
        ))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _read_record(
        self,
        mcp_id: str,
        definition: ManagedMCPDefinition,
    ) -> _PreparedRecord | None:
        try:
            raw = json.loads(self._record_path(mcp_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, Mapping):
            return None
        if str(raw.get("signature") or "") != self._definition_signature(definition):
            # The declaration changed since that handshake (an upgrade, or the
            # same name pointed at another package): prove it again.
            return None
        tools = raw.get("tools")
        if not isinstance(tools, list) or not all(isinstance(item, str) for item in tools):
            return None
        try:
            checked_at = float(raw.get("checked_at") or 0.0)
        except (TypeError, ValueError):
            checked_at = 0.0
        return _PreparedRecord(
            signature=str(raw["signature"]),
            tools=tuple(str(item) for item in tools),
            checked_at=checked_at,
        )

    def _write_record(
        self,
        mcp_id: str,
        definition: ManagedMCPDefinition,
        tools: tuple[str, ...],
    ) -> None:
        path = self._record_path(mcp_id)
        # 每个 Worker 各写自己的临时名：同一台 Server 可能在多个 Worker 里同时
        # 完成握手，共用 .tmp 会让两份记录互相截断。
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({
                    "signature": self._definition_signature(definition),
                    "tools": list(tools),
                    "checked_at": time.time(),
                }),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError as exc:
            # A record is a cache; failing to write one must not fail a server
            # that already answered its handshake.
            logger.warning("MCP preparation record for %s is not writable: %s", mcp_id, exc)

    def _launcher_exists(self, mcp_id: str) -> bool:
        """Whether the local installation behind one server is still there."""

        launcher = self._launchers.get(str(mcp_id))
        if launcher is None or not launcher.args:
            return True
        return Path(launcher.args[0]).is_file()

    def _start_background(
        self,
        mcp_id: str,
        definition: ManagedMCPDefinition,
        source: Path | None = None,
    ) -> MCPPreparationState:
        state = MCPPreparationState(
            mcp_id=mcp_id,
            status=MCP_PREPARING,
            phase=MCP_PHASE_INSTALLING,
        )
        self._states[mcp_id] = state
        if self._closed:
            return state
        self._track(
            mcp_id,
            self._prepare_and_settle(mcp_id, definition, source),
            prefix="mcp-prepare",
        )
        return state

    def _start_probe(
        self,
        mcp_id: str,
        definition: ManagedMCPDefinition,
        source: Path | None,
        pinned: ManagedMCPDefinition,
    ) -> None:
        """Prove one already-launchable declaration, without changing its state.

        The server keeps reporting ``ready`` while this runs: a Generation is
        free to connect it in the meantime.  What this adds is the handshake --
        the tools it lists and the record of them -- so a UI can show a Server
        no Generation connects yet.  A failure moves it to ``failed``, which is
        the point at which it stops being connected directly and reports why.
        """

        if self._closed:
            return
        self._track(
            mcp_id,
            self._prepare_and_settle(mcp_id, definition, source, pinned=pinned),
            prefix="mcp-probe",
        )

    def _track(
        self,
        mcp_id: str,
        coroutine: object,
        *,
        prefix: str,
    ) -> None:
        task = asyncio.create_task(
            coroutine,  # type: ignore[arg-type]
            name=f"{prefix}-{mcp_id}",
        )
        self._tasks[mcp_id] = task
        task.add_done_callback(
            lambda _task, _key=mcp_id: self._tasks.pop(_key, None)
        )

    def forget(self, mcp_id: str) -> None:
        """Drop a settled state so the next ``ensure`` prepares the server again.

        A server that failed once is not retried on its own: a transient
        network failure during startup would otherwise loop, reloading the
        Runtime every few seconds.  An explicit refresh clears the state.
        """

        key = str(mcp_id)
        state = self._states.get(key)
        if state is None or state.preparing:
            return
        self._states.pop(key, None)
        self._drop_record(key)

    def discard(self, mcp_id: str) -> None:
        """Forget one server and cancel work that is still running for it."""

        key = str(mcp_id)
        self._states.pop(key, None)
        self._launchers.pop(key, None)
        self._drop_record(key)
        task = self._tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()

    def _drop_record(self, mcp_id: str) -> None:
        """Forget what a previous handshake proved about one server.

        The record describes an installation that is about to be replaced or
        removed, so leaving it behind would answer for a server that is not
        there any more.
        """

        try:
            self._record_path(mcp_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("MCP preparation record for %s is not removable: %s", mcp_id, exc)

    def invalidate_all(self) -> None:
        """Forget every settled result so the next build prepares servers again.

        Used by an explicit health refresh: it is the only way a server that
        failed once is tried again without restarting the Runtime.
        """

        for mcp_id, state in list(self._states.items()):
            if not state.preparing:
                self._states.pop(mcp_id, None)
                self._launchers.pop(mcp_id, None)

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._tasks.values()]
        self._tasks.clear()
        reload_task, self._reload_task = self._reload_task, None
        if reload_task is not None:
            tasks.append(reload_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- background work ---------------------------------------------------

    async def _prepare_and_settle(
        self,
        mcp_id: str,
        definition: ManagedMCPDefinition,
        source: Path | None = None,
        *,
        pinned: ManagedMCPDefinition | None = None,
        vendored: bool = False,
    ) -> None:
        if pinned is None:
            try:
                resolved, vendor_root = await asyncio.to_thread(
                    self._prepare_definition,
                    definition,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - preparation reports any failure
                self._settle(
                    mcp_id,
                    MCPPreparationState(
                        mcp_id,
                        MCP_PREPARATION_FAILED,
                        detail=failure_detail(exc),
                    ),
                )
                return
            if source is not None and not Path(source).is_file():
                # The declaration was uninstalled while its package was still
                # downloading. Publishing the tree now would leave a directory no
                # declaration owns on a board that is short on disk.
                if vendor_root is not None:
                    await asyncio.to_thread(remove_vendored_server, vendor_root)
                self._states.pop(mcp_id, None)
                self._launchers.pop(mcp_id, None)
                return
            pinned = resolved
            vendored = vendor_root is not None
            # The package now exists locally: what is left is starting it and
            # answering the handshake, which is a different wait for the operator.
            self._advance_phase(mcp_id, MCP_PHASE_STARTING)
        if not self._verify:
            self._settle(mcp_id, MCPPreparationState(
                mcp_id, MCP_PREPARED, vendored=vendored, launchable=True))
            self._launchers[mcp_id] = pinned
            return
        try:
            result = await self._probe_config(
                load_managed_mcp_config(pinned, workspace=self._home.workspace_dir)
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - preparation reports any failure
            self._settle(
                mcp_id,
                MCPPreparationState(
                    mcp_id,
                    MCP_PREPARATION_FAILED,
                    detail=failure_detail(exc),
                    vendored=vendored,
                    # The launcher is there: a failure to start it once is this
                    # manager's verdict, not a reason to keep a Generation from
                    # connecting a server that may well answer for it.
                    launchable=True,
                ),
            )
            return
        if result.verified and not result.ready:
            self._settle(
                mcp_id,
                MCPPreparationState(
                    mcp_id,
                    MCP_PREPARATION_FAILED,
                    detail=result.detail or "MCP server did not answer the handshake",
                    vendored=vendored,
                    launchable=True,
                ),
            )
            return
        self._launchers[mcp_id] = pinned
        tools = tuple(sorted(result.tools))
        await asyncio.to_thread(self._write_record, mcp_id, definition, tools)
        self._settle(
            mcp_id,
            MCPPreparationState(
                mcp_id,
                MCP_PREPARED,
                tools=tools,
                vendored=vendored,
                checked_at=time.time(),
                launchable=True,
            ),
        )

    def _advance_phase(self, mcp_id: str, phase: str) -> None:
        """Move an in-flight preparation to its next stage.

        ``phase`` only ever describes work in flight: ``_settle`` writes a
        fresh state, so a server that answered carries no stale "installing".
        """

        if self._closed:
            return
        state = self._states.get(str(mcp_id))
        if state is None or state.status != MCP_PREPARING:
            return
        self._states[str(mcp_id)] = replace(state, phase=phase)

    def _settle(self, mcp_id: str, state: MCPPreparationState) -> None:
        if self._closed:
            return
        previous = self._states.get(mcp_id)
        self._states[mcp_id] = state
        if previous is not None and previous.ready and state.launchable:
            # The Server was connectable and still is: a fresher tool list, a
            # handshake time or a failed probe is not worth rebuilding every
            # Generation for.
            return
        self._schedule_reload()

    def _schedule_reload(self) -> None:
        if self._closed or self._reload is None:
            return
        self._reload_pending = True
        if self._reload_task is None or self._reload_task.done():
            self._reload_task = asyncio.create_task(
                self._reload_loop(),
                name="mcp-preparation-reload",
            )

    async def _reload_loop(self) -> None:
        try:
            while self._reload_pending and not self._closed:
                self._reload_pending = False
                await asyncio.sleep(self._reload_debounce_seconds)
                if self._closed or self._reload is None:
                    return
                try:
                    await self._reload()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - a reload must not kill the manager
                    logger.warning(
                        "MCP preparation reload failed (%s)",
                        type(exc).__name__,
                    )
        finally:
            self._reload_task = None

    def _pin_definition(
        self,
        definition: ManagedMCPDefinition,
    ) -> tuple[ManagedMCPDefinition, Path | None]:
        return pin_npm_dependency(definition, self._home)

    async def _probe_server(self, config: MCPServerConfig) -> MCPProbeResult:
        return await probe_mcp_server(
            workspace=self._home.workspace_dir,
            config=config,
            attempts=_PROBE_ATTEMPTS,
        )


def _managed_definition(
    mcp_id: str,
    config: MCPServerConfig,
) -> ManagedMCPDefinition:
    """The launcher view of one declaration, for the vendoring step."""

    invocation = (
        npx_package_invocation(config.command, config.args)
        if config.transport == "stdio"
        else None
    )
    return ManagedMCPDefinition(
        plugin_id=str(mcp_id),
        display_name=config.display_name or str(mcp_id),
        executable=config.command,
        args=tuple(config.args),
        transport=config.transport,
        url=config.url,
        npm_package="" if invocation is None else invocation[0],
        npm_arguments=() if invocation is None else invocation[1],
    )


__all__ = [
    "MCP_PREPARED",
    "MCP_PREPARATION_FAILED",
    "MCP_PREPARING",
    "MCPPreparationManager",
    "MCPPreparationState",
    "RELOAD_DEBOUNCE_SECONDS",
]
