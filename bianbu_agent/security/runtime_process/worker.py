"""默认 Agent Worker 进程入口。

Worker 不监听 HTTP，只加载一个 Agent Runtime 并通过私有 UDS 接受控制面
的 Run/审批/取消命令。安全模式和租约由控制面启动时注入；Worker
只回显证明，不能修改策略。
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.mcp.lifecycle import (
    MCP_STATE_FAILED,
    MCP_STATE_PREPARING,
    MCP_STATE_READY,
    MCP_STATE_UNKNOWN,
    mcp_lifecycle_state,
    mcp_phase,
    mcp_state_ready,
)
from bianbu_agent.mcp.preparation import (
    MCP_PREPARED,
    MCP_PREPARATION_FAILED,
    MCP_PREPARING,
)
from bianbu_agent.runs import RunHandle
from bianbu_agent.runs.events import RunEvent
from bianbu_agent.plugins.contributions import skill_unavailable_reason
from bianbu_agent.sessions.inbox import (
    SessionInboxMessage,
    SessionInboxSnapshot,
    inbox_message_from_dict,
)
def create_runtime(config: RuntimeConfig, **kwargs: Any):
    """Lazy runtime factory; plugin imports must not delay Worker Hello."""
    from bianbu_agent.server.factory import create_runtime as factory
    return factory(config, **kwargs)
from bianbu_agent.security.runtime_process.worker_rpc import WorkerRPCServer
from bianbu_agent.inventory import CapabilityKind
from bianbu_agent.mcp.sources import runtime_mcp_id


def _is_submitted_lease(value: str) -> bool:
    """Return whether a Worker identity came from securityd Runtime."""
    lease_id = str(value or "").strip()
    return bool(lease_id) and lease_id != "dev"


def _agent_security_subject(agent_id: str) -> str:
    return f"com.bianbu.agent_id:{agent_id}"


def _preparation_state(state: object) -> str:
    """The lifecycle word for what the preparation manager just reported."""

    return {
        MCP_PREPARED: MCP_STATE_READY,
        MCP_PREPARING: MCP_STATE_PREPARING,
        MCP_PREPARATION_FAILED: MCP_STATE_FAILED,
    }.get(str(getattr(state, "status", "") or ""), MCP_STATE_UNKNOWN)


def _mcp_status_entries(
    catalog: object,
    generation: object,
    preparation: object | None,
    installed: Iterable[str] = (),
) -> dict[str, dict[str, object]]:
    """One lifecycle entry per installed MCP Server, for the Control plane.

    A Generation only speaks for the servers an Agent selected, and its
    capability catalog drops the rest -- an installed server nobody selected is
    not in it at all.  Enumerating that catalog therefore answered for the
    selected servers only and left every other installed one at ``not_loaded``
    forever, which is exactly the word the Control plane could never explain.
    The installed catalog is the honest list of what exists, and the
    preparation manager is the layer that knows whether each of them starts and
    which tools it lists.
    """

    statuses = getattr(generation, "statuses", {})
    entries: dict[str, dict[str, object]] = {}
    selected = {
        source.capability_id: runtime_id
        for runtime_id, source in getattr(catalog, "mcp_sources", {}).items()
    }
    # ``installed`` first, then anything the Generation knows about that the
    # installed catalog did not list (a Plugin-owned server has no package of
    # its own).  A dict preserves that order and dedupes.
    for capability_id in dict.fromkeys((*installed, *selected)):
        # A selected server is keyed by the Runtime ID the Generation and the
        # inventory agree on; an installed one only has its public ID, which
        # hashes to the same Runtime ID the inventory would give it.
        runtime_id = selected.get(capability_id) or runtime_mcp_id(capability_id)
        status = statuses.get(runtime_id)
        diagnostics = () if status is None else status.diagnostics
        # The lifecycle (not the Plugin's own state enum) is what every UI
        # renders, and the phase explains a preparation that is still
        # downloading or still handshaking.
        raw_status = "not_loaded" if status is None else status.health.status
        entry: dict[str, object] = {
            "state": mcp_lifecycle_state(raw_status),
            "phase": "",
            "reason": "" if not diagnostics else diagnostics[0].message,
        }
        prepared = (
            None if preparation is None
            else preparation.state(capability_id)
        )
        # A Generation that actually started this server is the better witness:
        # its verdict stands, and the preparation only answers for a server the
        # Generation never loaded or is still waiting on.
        deferred = status is None or raw_status in ("not_loaded", MCP_STATE_PREPARING)
        if prepared is not None and deferred:
            state = _preparation_state(prepared)
            entry["state"] = state
            entry["phase"] = (
                mcp_phase(prepared.phase) if state == MCP_STATE_PREPARING else ""
            )
            entry["tools"] = [
                {"id": name, "name": name, "display_name": name}
                for name in prepared.tools
            ]
            entry["vendored"] = bool(prepared.vendored)
            if prepared.checked_at is not None:
                entry["prepared_at"] = float(prepared.checked_at)
            if prepared.detail:
                entry["detail"] = str(prepared.detail)
        entries[capability_id] = entry
    return entries


class ControlOwnedSessionStore:
    """Worker guard that prevents a second process from owning Session rows."""

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def backfill_agent_id(self, _agent_id: str) -> None:
        return None

    @staticmethod
    def _deny() -> None:
        raise RuntimeError("Session metadata is owned by the control process")

    async def create(self, *args: Any, **kwargs: Any) -> object:
        self._deny()

    async def get(self, *args: Any, **kwargs: Any) -> object:
        self._deny()

    async def list(self, *args: Any, **kwargs: Any) -> list[object]:
        self._deny()

    async def delete(self, *args: Any, **kwargs: Any) -> bool:
        self._deny()

    async def update_title(self, *args: Any, **kwargs: Any) -> object:
        self._deny()

    async def rotate_thread(self, *args: Any, **kwargs: Any) -> bool:
        self._deny()


class AgentWorker:
    def __init__(
        self,
        config: RuntimeConfig,
        *,
        instance_id: str,
        socket_path: str | Path,
        lease_id: str = "",
        effective_mode: str = "host",
        backend: str = "host-process",
        sandboxed: bool = False,
        container_id: str = "",
    ) -> None:
        self.config = config
        self.instance_id = str(instance_id)
        self.socket_path = Path(socket_path)
        self.lease_id = str(lease_id)
        self.effective_mode = str(effective_mode)
        self.backend = str(backend)
        self.sandboxed = bool(sandboxed)
        self.container_id = str(container_id)
        self.rpc_token = ""
        # Runtime composition discovers and imports every installed plugin.
        # On a slow RISC-V worker this can take longer than the supervisor's
        # handshake deadline.  Keep construction out of ``__init__`` so the
        # RPC socket can be published first and the control plane can attest
        # this process before the heavier plugin load begins.
        self.runtime = None
        self._runtime_task: asyncio.Task[Any] | None = None
        self._server: WorkerRPCServer | None = None
        self._shutdown = asyncio.Event()
        self._active: dict[str, RunHandle] = {}
        self._activated = False

    async def start(self) -> None:
        os.environ["BIANBU_RUNTIME_ROLE"] = "agent-worker"
        agent_id = (
            os.environ.get("BIANBU_WORKER_AGENT_ID")
            or os.environ.get("COM_BIANBU_AGENT_ID")
        )
        agent_id = str(agent_id or "").strip()
        if not agent_id:
            raise RuntimeError("Worker requires COM_BIANBU_AGENT_ID")
        os.environ["BIANBU_WORKER_AGENT_ID"] = agent_id
        os.environ["COM_BIANBU_AGENT_ID"] = agent_id
        os.environ["BIANBU_SECURITY_SUBJECT"] = _agent_security_subject(agent_id)
        # The parent coordinator owns Prepare/Commit.  A Worker may still use
        # the normal capability/DLP client for each tool call, but the client
        # must use the lease-bound UDS path rather than D-Bus agent binding.
        os.environ["BIANBU_RUNTIME_WORKER_INSTANCE_ID"] = self.instance_id
        # ``dev`` is the supervisor's synthetic marker for the explicit
        # development unmanaged fallback. It is not a securityd-submitted
        # Runtime lease, so this Worker must use the normal D-Bus caller
        # binding instead of presenting ``dev`` to the lease-bound UDS.
        lease_id = self.lease_id or os.environ.get("BIANBU_WORKER_LEASE_ID", "")
        managed = _is_submitted_lease(lease_id)
        os.environ["BIANBU_WORKER_MANAGED"] = "1" if managed else "0"
        self.lease_id = self.lease_id or os.environ.get("BIANBU_WORKER_LEASE_ID", "")
        self.effective_mode = os.environ.get(
            "BIANBU_WORKER_EFFECTIVE_MODE", self.effective_mode)
        self.backend = os.environ.get("BIANBU_WORKER_BACKEND", self.backend)
        self.sandboxed = self.sandboxed or os.environ.get(
            "BIANBU_SANDBOX_ACTIVE", "") == "1"
        # 无特权进程只有在 no_new_privs 置位时才被内核允许装载 seccomp 过滤器，
        # 而 no_new_privs 一旦置位不可撤销：它会永久剥夺本进程的 setuid 提权
        # 能力，sudo/pkexec/su 全部失效。host Worker 以登录用户身份运行，必须
        # 保留正常 PAM/sudoers 语义（见 debian/bianbu-agent-runtime.service），
        # 因此只有 sandbox Worker 装载审计过滤器——此时 bwrap 已经装载同一份
        # securityd 版本化过滤器，这里只叠加 SECCOMP_FILTER_FLAG_LOG。sandbox
        # 分支仍是 fail-closed：装载失败即拒绝启动，不得以无审计方式继续。
        audit_filter = os.environ.get("BIANBU_SECCOMP_AUDIT_FILTER", "")
        if audit_filter and self.sandboxed:
            from bianbu_agent.security.seccomp_audit import enable_filter_logging
            enable_filter_logging(Path(audit_filter))
        elif audit_filter:
            logging.getLogger(__name__).warning(
                "host Worker 不装载 seccomp 过滤器以保留 sudo/setuid 语义：%s",
                audit_filter,
            )
        self.container_id = self.container_id or os.environ.get(
            "BIANBU_CONTAINER_ID", "")
        self.rpc_token = os.environ.get("BIANBU_WORKER_RPC_TOKEN", "").strip()
        if not self.rpc_token:
            if managed:
                raise RuntimeError("managed Worker 缺少 supervisor-issued RPC token")
            # Direct worker invocation remains useful for protocol development;
            # there is no controller to authenticate in that mode.
            self.rpc_token = secrets.token_urlsafe(32)
        self._server = WorkerRPCServer(
            self.socket_path, self.instance_id, self.dispatch,
            token=self.rpc_token,
        )
        await self._server.start()
        # Do the expensive plugin discovery off the event loop.  This keeps
        # the socket responsive to the supervisor's Hello request while the
        # worker is preparing its runtime.
        self._runtime_task = asyncio.create_task(
            asyncio.to_thread(
                create_runtime,
                self.config,
                role="worker",
                agent_id=agent_id,
                run_store=MemoryRunStore(),
                session_store=ControlOwnedSessionStore(),
            ),
            name="bianbu-worker-runtime-load",
        )

    async def close(self) -> None:
        for handle in tuple(self._active.values()):
            try:
                await handle.cancel()
            except Exception:
                pass
        self._active.clear()
        server, self._server = self._server, None
        if server is not None:
            await server.close()
        task, self._runtime_task = self._runtime_task, None
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.runtime is not None:
            await self.runtime.close()

    async def wait(self) -> None:
        await self._shutdown.wait()

    async def dispatch(self, kind: str, message: Mapping[str, Any]) -> Mapping[str, Any] | RunHandle | None:
        if kind == "hello":
            return {
                "agent_id": os.environ.get("BIANBU_WORKER_AGENT_ID") or
                            os.environ.get("BIANBU_AGENT_ID", "com.bianbu.agent"),
                "instance_id": self.instance_id,
                "lease_id": self.lease_id,
                "pid": os.getpid(),
                "sandboxed": self.sandboxed,
                "container_id": self.container_id,
                "effective_mode": self.effective_mode,
                "backend": self.backend,
                "status": "starting" if not self._activated else "running",
                # Hello is sent before activation/CommitStart, so the initial
                # declaration is intentionally empty. The active generation
                # is exposed through worker.catalog after the lease commits.
                "capabilities": [],
                "generation_id": (self.runtime.plugin_runtime.current_generation.id
                                   if self._activated else 0),
            }
        if kind == "worker.activate":
            if self._activated:
                assert self.runtime is not None
                return {"status": "active", "generation_id": self.runtime.plugin_runtime.current_generation.id}
            if self.runtime is None and self._runtime_task is not None:
                self.runtime = await self._runtime_task
                self._runtime_task = None
            if self.runtime is None:
                raise RuntimeError("Worker runtime is not initialized")
            await self.runtime.start()
            self._activated = True
            generation_id = int(self.runtime.plugin_runtime.current_generation.id)
            return {"status": "active", "generation_id": generation_id}
        if kind == "worker.reload":
            if not self._activated:
                raise RuntimeError("Worker is not activated")
            assert self.runtime is not None
            # 显式刷新是唯一能让上一次准备失败的 MCP 再试一次的机会：它清掉
            # 已结算的结论，随后那次重新准备才会重新探测。
            force = bool(message.get("force", False))
            if force:
                preparation = getattr(self.runtime, "mcp_preparation", None)
                if preparation is not None:
                    preparation.invalidate_all()
            result = await self.runtime.plugin_runtime.reload_installed(
                force=force
            )
            # A reload is how a mutation is published, so it is also where a
            # declaration that nobody prepared yet has to be picked up: waiting
            # for a Worker restart would leave a freshly installed server at
            # ``not_loaded`` with nothing running it.  Re-preparing is a lookup
            # for every server that already settled, so this is cheap.
            prepare = getattr(self.runtime, "prepare_installed_mcps", None)
            if callable(prepare):
                prepare()
            return {
                "status": "active",
                "generation_id": int(result.generation_id),
                "changed": bool(result.changed),
            }
        if kind == "worker.catalog":
            if not self._activated:
                raise RuntimeError("Worker is not activated")
            generation = self.runtime.plugin_runtime.current_generation
            catalog = generation.capability_catalog
            preparation = getattr(self.runtime, "mcp_preparation", None)
            mcp_statuses = _mcp_status_entries(
                catalog,
                generation,
                preparation,
                self._installed_mcp_ids(),
            )
            return {
                "generation_id": int(generation.id),
                "capabilities": self._catalog_views(),
                "mcps": sorted(str(mcp_id) for mcp_id in generation.mcps),
                "mcp_servers": await self._mcp_server_views(generation),
                "mcp_statuses": mcp_statuses,
            }
        if kind == "health":
            if self.runtime is None:
                return {"status": "starting", "pid": os.getpid(), "active_runs": 0,
                        "waiting_runs": 0, "generation_id": 0}
            diagnostics = await self.runtime.diagnostics()
            return {
                "status": "running",
                "pid": os.getpid(),
                "active_runs": diagnostics.active_runs,
                "waiting_runs": diagnostics.waiting_runs,
                "generation_id": diagnostics.generation_id,
            }
        if kind == "run.start":
            if not self._activated:
                raise RuntimeError("Worker is not activated")
            assert self.runtime is not None
            run_id = str(message.get("run_id") or "").strip()
            session_id = str(message.get("session_id") or "").strip()
            thread_id = str(message.get("thread_id") or "").strip()
            prompt = str(message.get("prompt") or "")
            if not run_id or not session_id or not thread_id or not prompt:
                raise ValueError(
                    "run.start requires run_id, session_id, thread_id and prompt"
                )
            if run_id in self._active:
                raise RuntimeError("Run already active")
            process_agent_id = str(
                os.environ.get("BIANBU_WORKER_AGENT_ID") or ""
            ).strip()
            requested_agent_id = str(
                message.get("agent_id") or process_agent_id
            ).strip()
            account_id = str(message.get("account_id") or "").strip()
            if not account_id:
                raise ValueError("run.start requires account_id")
            if not process_agent_id:
                process_agent_id = requested_agent_id
            if requested_agent_id != process_agent_id:
                raise RuntimeError(
                    f"Worker is bound to Agent {process_agent_id}; "
                    f"refusing Run for Agent {requested_agent_id}"
                )
            raw_input_messages = message.get("input_messages")
            prompt_input = (
                tuple(
                    inbox_message_from_dict(item)
                    for item in raw_input_messages
                    if isinstance(item, Mapping)
                )
                if isinstance(raw_input_messages, list) and raw_input_messages
                else prompt
            )
            # Discover out-of-band filesystem installs before taking the Run's
            # generation lease. Existing Runs keep their leased connections.
            # The lease opens right below, so the snapshot verified here is
            # handed over instead of walked and hashed a second time.
            from bianbu_agent.plugins.manager import CapabilityReloadError
            try:
                await self.runtime.plugin_runtime.reload_installed(handoff=True)
            except CapabilityReloadError as exc:
                logging.getLogger(__name__).warning(
                    "Keeping last valid capability generation after discovery failure: %s", exc
                )
                # Run admission still acquires a lease and rejects an unavailable
                # or poisoned manager; only an unpublished candidate is ignored.
            handle = await self.runtime.runs.run(
                session_id,
                prompt_input,
                agent_id=requested_agent_id,
                account_id=account_id,
                thread_id=thread_id,
                metadata=(message.get("metadata") if isinstance(message.get("metadata"), Mapping) else {}),
                idempotency_key=str(message.get("idempotency_key") or "") or None,
                idempotency_fingerprint=str(message.get("idempotency_fingerprint") or "") or None,
                request_id=run_id,
            )
            self._active[run_id] = handle
            async def remove_when_done() -> None:
                try:
                    await handle.wait()
                finally:
                    self._active.pop(run_id, None)
            asyncio.create_task(remove_when_done(), name=f"worker-cleanup-{run_id}")
            return handle
        if kind in {"run.cancel", "run.step_input", "approval.result"}:
            run_id = str(message.get("run_id") or "")
            handle = self._active.get(run_id) or await self.runtime.runs.get(run_id)
            if handle is None:
                raise RuntimeError("Run is not active")
            if kind == "run.cancel":
                return {"accepted": await handle.cancel()}
            if kind == "run.step_input":
                raw = message.get("messages")
                if not isinstance(raw, list) or any(
                    not isinstance(item, Mapping) for item in raw
                ):
                    raise ValueError("run.step_input requires a message array")
                messages = tuple(inbox_message_from_dict(item) for item in raw)
                replace_step_input = getattr(
                    self.runtime.runs._store,
                    "replace_step_input",
                    None,
                )
                if not callable(replace_step_input):
                    raise RuntimeError("Worker step input mirror is unavailable")
                await replace_step_input(handle.session_id, messages)
                return {
                    "accepted": await self.runtime.runs.deliver_step_input(
                        run_id,
                    )
                }
            payload = message.get("payload")
            payload = dict(payload) if isinstance(payload, Mapping) else {}
            action = str(message.get("action") or "")
            approval_id = str(payload.get("approval_id") or payload.get("question_id") or "")
            if action == "approve":
                accepted = await handle.approve(approval_id)
            elif action == "reject":
                accepted = await handle.reject(approval_id, reason=str(payload.get("reason") or ""))
            elif action == "answer":
                accepted = await handle.answer(approval_id, str(payload.get("answer") or ""))
            else:
                raise ValueError("unknown approval action")
            return {"accepted": accepted}
        if kind == "shutdown":
            self._shutdown.set()
            return {"accepted": True}
        raise ValueError(f"unknown Worker request: {kind}")

    @staticmethod
    def _display_field(spec: object, field: str) -> str:
        """One Power format v2 display field, or "" when the spec has none.

        ``SkillSpec`` carries ``name_zh`` / ``name_en`` / ``description_zh`` as
        real fields; ``ToolSpec`` has no such fields and can only pass them
        through ``metadata``. Reading the attribute unchanged made every
        builtin Tool raise ``AttributeError`` and took the whole Worker
        catalog down with it, so the lookup has to tolerate both shapes.
        """

        value = getattr(spec, field, "")
        if value:
            return str(value)
        metadata = getattr(spec, "metadata", None)
        if not isinstance(metadata, Mapping):
            return ""
        return str(metadata.get(field) or "")

    def _catalog_views(self) -> dict[str, list[dict[str, Any]]]:
        """Build a JSON-only active catalog for the control-plane console.

        This deliberately exports metadata only.  No handler, module, or
        plugin instance crosses the Worker boundary.
        """
        generation = self.runtime.plugin_runtime.current_generation
        catalog = generation.capability_catalog
        result: dict[str, list[dict[str, Any]]] = {"tool": [], "skill": [], "hook": []}

        def add(kind: str, capability_id: str, *, display_name: str, description: str,
                version: str, origin: str, plugin_id: str | None, enabled: bool = True,
                assignable: bool = True, unavailable_reason: str = "",
                name_zh: str = "", name_en: str = "", description_zh: str = "") -> None:
            result[kind].append({
                "id": capability_id, "name": capability_id,
                "display_name": display_name, "description": description,
                "name_zh": name_zh, "name_en": name_en,
                "description_zh": description_zh,
                "version": version, "origin": origin, "plugin_id": plugin_id,
                "enabled": enabled, "assignable": assignable,
                "unavailable_reason": unavailable_reason,
            })

        for kind, contributions in (
            ("tool", {} if catalog is None else catalog.tools),
            ("skill", {} if catalog is None else catalog.skills),
        ):
            for capability_id, contribution in contributions.items():
                spec = contribution.spec
                metadata = spec.metadata
                if kind == "tool" and metadata.get("origin") == "mcp":
                    continue
                source = catalog.sources[(CapabilityKind(kind), capability_id)]
                reason = str(metadata.get("unavailable_reason") or "").strip()
                add(kind, capability_id,
                    display_name=(spec.name if kind == "tool" else spec.display_name),
                    description=spec.description, version=source.version,
                    origin=source.origin.value, plugin_id=source.plugin_id,
                    enabled=not reason, unavailable_reason=reason,
                    name_zh=self._display_field(spec, "name_zh"),
                    name_en=self._display_field(spec, "name_en"),
                    description_zh=self._display_field(spec, "description_zh"))

        active_tools = {item["id"] for item in result["tool"]}
        for capability_id, contribution in generation.tools.items():
            if capability_id in active_tools or contribution.spec.metadata.get("origin") == "mcp":
                continue
            spec = contribution.spec
            metadata = spec.metadata
            add("tool", capability_id, display_name=str(metadata.get("display_name") or spec.name),
                description=spec.description, version=str(metadata.get("version") or "0.0.0"),
                origin=str(metadata.get("origin") or "plugin"), plugin_id=contribution.plugin_id)

        active_skills = {item["id"] for item in result["skill"]}
        for capability_id, contribution in generation.skills.items():
            if capability_id in active_skills:
                continue
            spec = contribution.spec
            metadata = spec.metadata
            reason = skill_unavailable_reason(contribution)
            add("skill", capability_id, display_name=spec.display_name,
                description=spec.description, version=spec.version,
                origin=str(metadata.get("origin") or "plugin"),
                plugin_id=contribution.plugin_id, enabled=not reason,
                unavailable_reason=reason,
                name_zh=self._display_field(spec, "name_zh"),
                name_en=self._display_field(spec, "name_en"),
                description_zh=self._display_field(spec, "description_zh"))

        for capability_id, contribution in generation.hooks.items():
            metadata = contribution.metadata
            add("hook", capability_id,
                display_name=str(metadata.get("display_name") or capability_id),
                description=str(metadata.get("description") or ""),
                version=str(metadata.get("version") or "0.0.0"),
                origin=str(metadata.get("origin") or "plugin"),
                plugin_id=contribution.plugin_id,
                enabled=not str(metadata.get("unavailable_reason") or "").strip(),
                # Hooks are not an Agent-level selection: an Agent activates
                # them by selecting the Plugin that provides them.
                assignable=False,
                unavailable_reason=str(metadata.get("unavailable_reason") or "").strip())
        for items in result.values():
            items.sort(key=lambda item: item["id"])
        return result

    def _installed_mcp_ids(self) -> tuple[str, ...]:
        """Every MCP declaration on this device, selected by this Agent or not.

        The capability catalog only lists what this Agent selected, so it cannot
        answer for the servers the Control plane also has to explain.  The
        package manager owns the full installed set.
        """

        lister = getattr(getattr(self.runtime, "mcps", None), "list", None)
        if not callable(lister):
            return ()
        try:
            return tuple(str(record.id) for record in lister())
        except Exception as exc:  # noqa: BLE001 - a broken catalog still yields a status map
            logger.warning("installed MCP catalog unavailable: %s", exc)
            return ()

    async def _mcp_server_views(self, generation) -> list[dict[str, Any]]:
        preparation = getattr(self.runtime, "mcp_preparation", None)
        views: list[dict[str, Any]] = []
        for mcp_id, server in generation.mcps.items():
            status = str(server.status)
            state = mcp_lifecycle_state(status)
            phase = ""
            if state == MCP_STATE_PREPARING and preparation is not None:
                prepared = preparation.state(str(mcp_id))
                if prepared is not None:
                    phase = mcp_phase(prepared.phase)
            views.append({
                "id": str(mcp_id),
                "display_name": str(server.display_name),
                "command": str(server.command),
                "kind": "mcp",
                "status": status,
                "state": state,
                "phase": phase,
                "ready": mcp_state_ready(state),
                "tools_count": len(server.tool_views),
                "transport": str(server.transport),
                "tools": [dict(item) for item in server.tool_views],
                "source_kind": str(server.source_kind),
                "owner_plugin_id": server.owner_plugin_id,
                "removable": server.owner_plugin_id is None,
                "health_checked_at": None,
            })
        return sorted(views, key=lambda item: item["id"])


async def run_worker(config: RuntimeConfig, **kwargs: Any) -> int:
    worker = AgentWorker(config, **kwargs)
    try:
        await worker.start()
        await worker.wait()
        return 0
    finally:
        await worker.close()


class MemoryRunStore:
    """Worker-local Run store.

    The control plane owns durable Run metadata and event history. Keeping the
    Worker execution store in memory prevents the same JSON row being appended
    twice when RPC events are mirrored back to the control process.
    """

    def __init__(self) -> None:
        import asyncio
        self._records: dict[str, dict[str, Any]] = {}
        self._step_input: dict[str, tuple[SessionInboxMessage, ...]] = {}
        self._lock = asyncio.Lock()

    async def open(self) -> None: pass
    async def close(self) -> None: pass

    async def start(self, request_id: str, session_id: str, request: dict[str, Any]) -> None:
        import time
        async with self._lock:
            # A new Run for a Session takes the durable inbox prefix this Run
            # was admitted with as its own input. Step input mirrored here can
            # only belong to the Run that just ended: when that Run stopped
            # before applying it, the same message arrives as this Run's input,
            # and reporting it applied again is rejected by the Session log as
            # "applied input is not the next-step prefix".
            self._drop_consumed_step_input(str(session_id), request)
            self._records.setdefault(request_id, {
                "id": request_id, "session_id": session_id, "status": "running",
                "request": dict(request), "events": [], "result": None,
                "created_at": time.time(), "updated_at": time.time(),
            })

    def _drop_consumed_step_input(
        self, session_id: str, request: Mapping[str, object]
    ) -> None:
        """Forget step input this Run already consumed as its own input.

        Only messages named by ``input_messages`` are dropped: a Run with no
        inbox prefix (a title generation, for example) must leave another Run's
        pending steering untouched.
        """

        raw = request.get("input_messages")
        if not isinstance(raw, list) or not raw:
            return
        consumed = {
            str(item.get("message_id") or "")
            for item in raw
            if isinstance(item, Mapping)
        }
        pending = self._step_input.get(session_id, ())
        remaining = tuple(
            message for message in pending if message.message_id not in consumed
        )
        if not remaining:
            self._step_input.pop(session_id, None)
        else:
            self._step_input[session_id] = remaining

    async def append(self, request_id: str, event: RunEvent) -> None:
        import time
        async with self._lock:
            item = self._records[request_id]
            if event.kind == "input_applied":
                message_id = str(event.payload.get("message_id") or "")
                pending = self._step_input.get(item["session_id"], ())
                if not pending or pending[0].message_id != message_id:
                    raise ValueError(
                        f"applied Worker step input is not the pending prefix: "
                        f"{message_id}"
                    )
                remaining = pending[1:]
                if remaining:
                    self._step_input[item["session_id"]] = remaining
                else:
                    self._step_input.pop(item["session_id"], None)
            item["events"].append(event)
            item["updated_at"] = time.time()

    async def replace_step_input(
        self,
        session_id: str,
        messages: tuple[SessionInboxMessage, ...],
    ) -> None:
        async with self._lock:
            self._step_input[str(session_id)] = tuple(messages)

    async def pending_input(self, session_id: str) -> SessionInboxSnapshot:
        async with self._lock:
            return SessionInboxSnapshot(
                next_step=self._step_input.get(str(session_id), ()),
            )

    async def finish(self, request_id: str, result: Any) -> None:
        import time
        async with self._lock:
            item = self._records[request_id]
            item["status"] = result.status
            item["result"] = result
            item["updated_at"] = time.time()

    async def get(self, request_id: str) -> Any:
        from bianbu_agent.runs.models import StoredRun
        async with self._lock:
            item = self._records.get(request_id)
            if item is None: return None
            return StoredRun(
                request_id=request_id, session_id=item["session_id"], status=item["status"],
                request=dict(item["request"]), events=tuple(item["events"]),
                result=item["result"], created_at=item["created_at"], updated_at=item["updated_at"],
            )

    async def list(self, session_id: str | None = None) -> list[Any]:
        values = []
        async with self._lock:
            ids = tuple(self._records)
        for item in ids:
            record = await self.get(item)
            if record is not None and (session_id is None or record.session_id == session_id): values.append(record)
        return values

    async def delete(self, request_id: str) -> bool:
        async with self._lock: return self._records.pop(request_id, None) is not None
    async def delete_session(self, session_id: str) -> int:
        async with self._lock:
            ids = [key for key, value in self._records.items() if value["session_id"] == session_id]
            for key in ids: self._records.pop(key, None)
            return len(ids)


__all__ = ["AgentWorker", "run_worker"]
