"""Security feature bridge used by the integrated WebUI.

The bridge talks directly to ``bianbu_security_client`` (D-Bus/UDS), never to
the standalone security WebUI service. Requests use Runtime-defined operation IDs.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

# 控制台 DLP 矩阵的展示用 PII 分类。securityd 的公开契约只暴露生效矩阵
# （UDS op ``dlp.policy``），不暴露"某一行是否被该 Agent 单独覆盖"，
# 所以 Runtime 不再读取 securityd 内部模块（控制面边界见
# tests/sdk/test_dependency_boundary.py），分类由 Runtime 自己维护。
_DLP_PII_TYPES: tuple[str, ...] = (
    "name", "email", "phone", "address",
    "id_card", "bank_card", "medical", "biometric",
)
# 未显式配置时 securityd 的兜底动作；控制面只做展示，判定以 securityd 为准。
_DLP_DEFAULT_ACTION = "mask"

def _host_arch() -> str:
    """本机内核架构的规范名。

    系统调用号是架构相关的：riscv64 板子上把 x86_64 的编号写进策略既是错的
    也在目标架构上不可用。控制面默认用本机架构，调用方显式指定时才覆盖。
    """
    from bianbu_agent.security.syscall_catalog import normalize_arch

    resolved = normalize_arch(None)
    return resolved if resolved != "unknown" else "x86_64"


@router.get('/api/v1/security/syscalls/catalog')
async def syscall_catalog(arch: str = ''):
    resolved = str(arch or "").strip() or _host_arch()
    result = await asyncio.to_thread(
        _invoke_sync, 'syscall-catalog.v1', {'arch': resolved})
    if isinstance(result, dict):
        result.setdefault('arch', resolved)
        result.setdefault('host_arch', _host_arch())
    return result

@router.get('/api/v1/security/agents/{agent_id}/syscall-policy')
async def syscall_policy(agent_id: str, arch: str = ''):
    resolved = str(arch or "").strip() or _host_arch()
    return await asyncio.to_thread(_invoke_sync, 'syscall-policy.get.v1', {'agent_id': agent_id, 'arch': resolved})

@router.post('/api/v1/security/agents/{agent_id}/syscall-policy')
async def update_syscall_policy(agent_id: str, request: Request):
    body = await request.json()
    resolved = str(body.get('arch') or '').strip() or _host_arch()
    return await asyncio.to_thread(_invoke_sync, 'syscall-policy.agent.update.v1', {'agent_id': agent_id, 'arch': resolved, 'additions': body.get('additions', [])})

@router.delete('/api/v1/security/agents/{agent_id}/syscall-policy')
async def reset_syscall_policy(agent_id: str, arch: str = ''):
    resolved = str(arch or "").strip() or _host_arch()
    return await asyncio.to_thread(_invoke_sync, 'syscall-policy.agent.reset.v1', {'agent_id': agent_id, 'arch': resolved})

@router.get('/api/v1/security/syscall-audit/events')
async def syscall_audit_events(agent_id: str = '', instance_id: str = '', limit: int = 100, cursor: int = 0):
    return await asyncio.to_thread(_invoke_sync, 'syscall-audit.events.v1', {
        'agent_id': agent_id, 'instance_id': instance_id, 'limit': limit, 'cursor': cursor})

@router.get('/api/v1/security/syscall-audit/status')
async def syscall_audit_status():
    return await asyncio.to_thread(_invoke_sync, 'syscall-audit.status.v1', {})


@router.post('/api/v1/security/agents/reconcile')
async def reconcile_security_agents(request: Request):
    """Remove securityd identities that no Runtime Expert declares.

    This is deliberately not a "delete every Agent" reset.  Service principals
    and Agents owned by other applications stay, and an Agent with a live
    Worker is reported instead of losing its policies mid-run.  The default is
    a dry run; callers opt into deletion with ``dry_run: false``.
    """

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(400, "request body must be JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "request body must be an object")
    dry_run = _boolean(body.get("dry_run"), True)
    keep = _string_list(body.get("keep"), "keep")
    prefix = body.get("prefix", "com.bianbu.")
    if prefix is not None and not isinstance(prefix, str):
        raise HTTPException(400, "prefix must be a string or null")
    prefix = str(prefix).strip() if prefix is not None else None
    if prefix == "":
        raise HTTPException(400, "prefix must not be empty")
    runtime = request.app.state.runtime
    experts = [spec.id for spec in await runtime.agents.list()]
    registered = await asyncio.to_thread(_invoke_sync, 'agents.list.v1', {})
    rows = registered.get("agents", []) if isinstance(registered, dict) else registered
    busy = _busy_agents(runtime)
    from bianbu_agent.security.agent_reconciliation import (
        apply_reconciliation,
        plan_reconciliation,
    )

    plan = plan_reconciliation(
        rows or [], experts, keep=keep, active=busy,
        is_service=_is_service_agent, managed_prefix=prefix,
    )
    backup = None
    if not dry_run and plan.remove:
        # Unregistering deletes capability, binding and session-trust rows.
        # Keep a consistent snapshot first; there is no undo endpoint.
        try:
            backup = await asyncio.to_thread(
                _backup_security_registry, plan, _security_database_path(),
                _security_backup_root(runtime),
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, detail={
                "code": "security_backup_failed",
                "message": f"无法备份安全注册库，已取消清理：{exc}",
            }) from exc
    result = apply_reconciliation(
        plan,
        lambda agent_id: bool(
            _invoke_sync('agents.unregister.v1', {"agent_id": agent_id}).get("ok")
        ),
        dry_run=dry_run,
    )
    payload = result.as_dict()
    payload["prefix"] = prefix
    if backup is not None:
        payload["backup"] = backup
    return payload


def _boolean(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise HTTPException(400, "dry_run must be a boolean")


def _string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise HTTPException(400, f"{field} must be a list of Agent IDs")
    return [item.strip() for item in value if item.strip()]


def _is_service_agent(agent_id: str) -> bool:
    """Service principals are never user Experts, so never reconcile them.

    These IDs are securityd's service identities (see its ``config`` module).
    The Runtime control plane is not allowed to import securityd internals, so
    the fixed contract is repeated here; an installation may add its own
    runtime principal through ``BIANBU_RUNTIME_SECURITY_ID``.
    """

    service_ids = {"com.bianbu.runtime", "com.bianbu.runtime.development",
                   "com.bianbu.agent"}
    configured = os.environ.get("BIANBU_RUNTIME_SECURITY_ID", "").strip()
    if configured:
        service_ids.add(configured)
    return str(agent_id) in service_ids


def _security_database_path() -> Path | None:
    """The live securityd registry database, when this host has one."""

    configured = os.environ.get("BIANBU_SECURITY_DB", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
    else:
        etc = os.environ.get("BIANBU_ETC", "").strip()
        candidate = (Path(etc) if etc else Path("/etc/bianbu")) / "capabilities.db"
    return candidate if candidate.is_file() else None


def _security_backup_root(runtime) -> Path:
    """A directory the Runtime user can write.

    ``capabilities.db`` belongs to ``bianbu-security``; the service reads it
    through that supplementary group but cannot create a sibling directory.
    Backups therefore live in the Runtime data directory.
    """

    configured = os.environ.get("BIANBU_SECURITY_BACKUP_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_dir = getattr(getattr(runtime, "config", None), "data_dir", None)
    if data_dir is None:
        return Path(tempfile.gettempdir()) / "bianbu-agent-security-backups"
    return Path(data_dir) / "backups"


def _backup_security_registry(
    plan, database: Path | None, backup_root: Path,
) -> str | None:
    """Copy the registry before deleting rows and record exactly what was planned."""

    if database is None:
        # A host without a registry file has nothing to restore, and the
        # unregister call still goes through securityd's own DBus API.  Refuse
        # instead: deleting rows we cannot snapshot is not reversible.
        if _runtime_production_mode():
            raise HTTPException(503, detail={
                "code": "security_backup_unavailable",
                "message": "未找到安全注册库，已取消清理。",
            })
        return None
    backup_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="security-agent-reconcile-",
                                      dir=backup_root))
    destination = directory / "capabilities.db"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as source:
        with sqlite3.connect(destination) as copy:
            source.backup(copy)
    os.chmod(destination, 0o600)
    (directory / "plan.json").write_text(
        json.dumps(plan.as_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(directory / "plan.json", 0o600)
    return str(directory)


def _busy_agents(runtime) -> list[str]:
    """Agents whose Worker is executing a Run right now.

    The scheduler API is the contract; internal supervisor maps are only a
    fallback for older pools that predate ``busy_agents``.
    """

    pool = getattr(runtime, "worker_supervisor", None)
    if pool is None:
        return []
    busy = getattr(pool, "busy_agents", None)
    if callable(busy):
        return [str(agent_id) for agent_id in busy()]
    if not callable(getattr(pool, "agent_busy", None)):
        return []
    return [
        str(agent_id) for agent_id in getattr(pool, "_supervisors", {})
        if pool.agent_busy(agent_id)
    ]


def _runtime_production_mode() -> bool:
    mode = os.environ.get("BIANBU_RUNTIME_MODE", "development")
    return str(mode).strip().lower() == "production"


def _client():
    try:
        from bianbu_agent.security.security_runtime import security_client_module
        return security_client_module()
    except Exception as exc:  # pragma: no cover - depends on host installation
        raise HTTPException(503, f"securityd unavailable: {exc}") from exc


def _call_client(method: str, *args, **kwargs):
    """Call security client, using the Runtime's Python-ABI helper when needed."""
    missing_catalog = False
    try:
        client = _client()
        result = getattr(client, method)(*args, **kwargs)
        if method == "capability_catalog" and not result:
            missing_catalog = True
            # Thin clients return [] when securityd's immutable metadata is
            # absent from this venv. Try its installed system environment first.
            raise ImportError("securityd capability metadata unavailable in Runtime environment")
        return result
    except (HTTPException, ImportError):
        helper = (Path(__file__).parent.parent / "security" / "dbus_client_helper.py").resolve()
        if not helper.is_file():
            if missing_catalog:
                return []
            raise
        payload = json.dumps({"method": method, "args": args, "kwargs": kwargs}, ensure_ascii=False)
        env = os.environ.copy()
        client_dir = env.get("BIANBU_SECURITY_CLIENT_DIR", "")
        home = env.get("BIANBU_SECURITYD_HOME", "")
        env["PYTHONPATH"] = client_dir or (str(Path(home) / "client") if home else "")
        try:
            proc = subprocess.run(["/usr/bin/python3", str(helper), payload], capture_output=True, text=True, env=env, timeout=30)
        except (OSError, subprocess.SubprocessError):
            if missing_catalog:
                return []
            raise
        if proc.returncode:
            detail = proc.stderr.strip() or proc.stdout.strip() or "securityd unavailable"
            lowered = detail.lower()
            if "unknownmethod" in lowered or "has no attribute" in lowered:
                raise KeyError(f"securityd incompatible: {method}")
            raise HTTPException(503, detail)
        return json.loads(proc.stdout)
    except AttributeError as exc:
        raise KeyError(f"securityd incompatible: {method}") from exc
    except Exception as exc:
        lowered = str(exc).lower()
        if "unknownmethod" in lowered or "unknown method" in lowered:
            raise KeyError(f"securityd incompatible: {method}") from exc
        raise


def _invoke_sync(operation: str, p: dict[str, Any]) -> Any:
    class ClientProxy:
        def __getattr__(self, name):
            return lambda *args, **kwargs: _call_client(name, *args, **kwargs)
    sc = ClientProxy()
    aid = str(p.get("agent_id", ""))
    if operation == "agents.list.v1":
        return {"agents": sc.list_agents()}
    if operation == "agents.unregister.v1":
        if not aid:
            raise ValueError("agent_id is required")
        return {"ok": bool(sc.unregister_agent(aid))}
    if operation == "agents.get.v1":
        return sc.get_agent_info(aid)
    if operation == "agents.suspend.v1":
        return {"ok": bool(sc.suspend_agent(aid, str(p.get("reason", "webui"))))}
    if operation == "agents.security-mode.get.v1":
        return sc.get_agent_security_mode(aid)
    if operation == "agents.security-mode.update.v1":
        mode = p.get("mode")
        if mode not in {"safe", "yolo"}:
            raise ValueError("mode must be safe or yolo")
        return sc.set_agent_security_mode(aid, mode)
    if operation in {"capabilities.policy.v1", "capabilities.grant.v1"}:
        # 一个工具一条策略：allow / ask / deny。旧名 grant.v1 作为兼容入口，
        # 语义收敛到同一个三态策略更新。
        #
        # This is a policy-row write, not a sandbox change: it must not stop or
        # restart the Worker.  securityd is consulted on every HITL precheck,
        # so the next tool call already sees the new row.
        policy = str(p.get("policy", "") or "").strip().lower()
        if policy not in {"allow", "ask", "deny"}:
            raise ValueError("policy 必须是 allow / ask / deny")
        return {"ok": bool(sc.set_capability_policy(aid, str(p.get("tool", p.get("capability", ""))), policy))}
    if operation == "capabilities.update.v1":
        tool = str(p.get("tool", p.get("capability", ""))).strip()
        if not tool:
            raise ValueError("tool 不能为空")
        # Like capabilities.policy.v1, this only writes one tool's policy row.
        # The Worker's sandbox lease depends on the Agent's security mode and
        # permissions, not on per-tool allow/ask/deny, so no restart is needed.
        policy = str(p.get("policy", "") or "").strip().lower()
        if policy not in {"allow", "ask", "deny"}:
            raise ValueError("policy 必须是 allow / ask / deny")
        ok = sc.register_capabilities(aid, [{
            "tool": tool, "policy": policy,
            "framework": str(p.get("framework", "") or "")}])
        return {"ok": bool(ok)}
    if operation == "capabilities.catalog.v1":
        # securityd exposes an authoritative immutable catalog, scoped to the
        # framework the Agent is wired to (runtime vs hermes). Keep the
        # per-agent union as a compatibility fallback for older daemons.
        framework = str(p.get("framework", "") or "")
        if not framework and aid:
            # The console never has to know which framework an Agent belongs
            # to: securityd records it at registration, and this is the only
            # place that decides whether runtime or hermes tools are listed.
            try:
                framework = str((sc.get_agent_info(aid) or {}).get("framework", "") or "")
            except Exception:
                framework = ""
        catalog = sc.capability_catalog(framework)
        if catalog:
            return {"capabilities": catalog}
        caps = {}
        for a in sc.list_agents():
            for c in sc.get_capabilities(str(a.get("agent_id", ""))):
                caps.setdefault(str(c.get("tool", c.get("capability", ""))), c)
        return {"capabilities": list(caps.values())}
    if operation == "permission.check.v1":
        d = int(sc.check_permission(aid, str(p.get("tool", p.get("capability", "")))))
        return {"decision": {0: "allow", 1: "ask", 2: "deny"}.get(d, "deny")}
    if operation == "permissions.boundary.v1":
        # 只读预览：这个专家进沙箱后究竟能读什么、写什么。边界由 securityd 用
        # 启动 Worker 时的同一个编译器算出来，这里只补上安全服务看不到的 Worker
        # 数据根——缺省时 securityd 会显示成 <agent-root> 占位符。
        return _drop_retired_session_boundary(
            sc.get_execution_boundary(aid, str(p.get("worker_data_root", "") or ""))
        )
    if operation == "permissions.get.v1":
        # 五组安全权限设置：可读/可写/网络/系统调用/自定义。
        return sc.get_security_permissions(aid)
    if operation == "permissions.set-group.v1":
        group = str(p.get("group", "") or "")
        if group == "network":
            current = sc.get_security_permissions(aid)
            if current.get("network_managed") is False:
                raise ValueError(
                    "该 Agent 的网络边界由它自身的框架接管，securityd 的域名策略"
                    "不会生效；请改框架自身的配置。")
            ok = bool(sc.set_network_policy(
                aid, list(p.get("domains", [])), str(p.get("mode", "allow_all"))))
            return {"ok": ok, **sc.get_security_permissions(aid)}
        return sc.set_permission_group(aid, group, p.get("value"))
    if operation == "dlp.scan.v1":
        return sc.dlp_scan(aid, str(p.get("text", "")))
    if operation == "network-policy.get.v1":
        return sc.get_network_policy(aid)
    if operation == "network-policy.update.v1":
        ok = bool(sc.set_network_policy(aid, list(p.get("domains", [])), str(p.get("mode", "whitelist"))))
        return {"ok": ok, **(sc.get_network_policy(aid) if ok else {})}
    if operation in {"syscall-catalog.v1", "syscall-policy.get.v1",
                     "syscall-policy.agent.update.v1", "syscall-policy.agent.reset.v1"}:
        # 架构默认跟随本机内核：riscv64 板子上把 x86_64 的编号写进策略既是错的
        # 也在目标架构上不可用。调用方显式指定时才用它的值。
        resolved = str(p.get("arch") or "").strip() or _host_arch()
    if operation == "syscall-catalog.v1":
        return sc.get_syscall_catalog(resolved)
    if operation == "syscall-policy.get.v1":
        return sc.get_syscall_policy(aid, resolved)
    if operation in {"syscall-policy.agent.update.v1",
                     "syscall-policy.agent.reset.v1"}:
        current = sc.get_security_permissions(aid)
        if current.get("syscall_managed") is False:
            raise ValueError(
                "该 Agent 的系统调用边界不由 securityd 接管，保存不会生效。")
    if operation == "syscall-policy.agent.update.v1":
        return sc.set_agent_syscall_policy(aid, resolved, list(p.get("additions", [])))
    if operation == "syscall-policy.agent.reset.v1":
        return sc.reset_agent_syscall_policy(aid, resolved)
    if operation == "syscall-audit.events.v1":
        instance_id = str(p.get("instance_id", ""))
        # Runtime leases are owned by the security runtime principal
        # (normally ``com.bianbu.runtime``), while the WebUI selects the
        # user-facing Agent ID.  Query the independent chain without the
        # security-principal filter, then scope results to instances whose
        # immutable ID starts with the selected Worker Agent ID.  Keep the
        # legacy direct-agent match for records created before lease-bound V2.
        query_agent = aid if not aid or aid == "com.bianbu.runtime" else ""
        rows = sc.get_syscall_audit_events(query_agent, instance_id,
                                           int(p.get("limit", 100)), int(p.get("cursor", 0)))
        if aid and aid != "com.bianbu.runtime":
            prefix = aid + "-"
            rows = [row for row in rows if (
                str(row.get("agent_id", "")) == aid
                or str(row.get("instance_id", "")).startswith(prefix)
            )]
        return rows
    if operation == "syscall-audit.status.v1":
        return sc.get_syscall_audit_status()
    if operation == "audit.query.v1":
        # Never fall back to full export: browsing must stay bounded even when
        # the installed Security client/daemon has not yet been upgraded.
        query = {key: p[key] for key in
                 ("filter", "limit", "cursor", "snapshot_seq", "from", "to") if key in p}
        query["agent_id"] = aid
        return sc.query_audit(json.dumps(query))
    if operation == "audit.view.v1":
        # The raw query stays available for the technical view; this operation
        # adds the product-facing category, plain-language title and severity.
        # Category/outcome filters are applied across raw pages so the caller
        # never sees a page that was silently filtered after pagination.
        from bianbu_agent.security.audit_view import query_audit_view

        query = {key: p[key] for key in
                 ("filter", "limit", "cursor", "snapshot_seq", "from", "to",
                  "category", "outcome", "outcome_group", "attention") if key in p}
        query["agent_id"] = aid
        return query_audit_view(lambda raw: sc.query_audit(json.dumps(raw)), query)
    if operation == "dlp.matrix.v1":
        policy = {str(k): str(v) for k, v in dict(sc.dlp_policy(aid) or {}).items()}
        types = list(_DLP_PII_TYPES) + [k for k in policy if k not in _DLP_PII_TYPES]
        return {"agent_id": aid,
                "rows": [{"pii_type": k,
                          "action": policy.get(k, _DLP_DEFAULT_ACTION)} for k in types]}
    if operation == "dlp.stats.v1":
        filt = {"agent_id": aid} if aid else {}
        path = sc.export_audit(json.dumps(filt), "json")
        records = json.loads(Path(path).read_text()).get("records", [])
        by_decision = {}
        by_point = {}
        for rec in records:
            op = str(rec.get("op_type", "")); result = str(rec.get("result", ""))
            if not op.startswith("hook:") or "dlp_" not in result.lower():
                continue
            point = op[5:]; by_point[point] = by_point.get(point, 0) + 1
            decision = str(rec.get("target", "")); by_decision[decision] = by_decision.get(decision, 0) + 1
        return {"total": sum(by_point.values()),
                "by_hook_point": [{"name": k, "count": v} for k, v in sorted(by_point.items())],
                "by_decision": [{"name": k, "count": v} for k, v in sorted(by_decision.items())]}
    if operation == "dlp.policy.update.v1":
        return {"ok": bool(sc.set_dlp_policy(aid, str(p.get("pii_type", "")), str(p.get("action", ""))))}
    if operation == "dlp.policy.delete.v1":
        return {"ok": bool(sc.delete_dlp_policy(aid, str(p.get("pii_type", ""))))}
    raise KeyError(f"unknown security operation: {operation}")


def _public_security_error(detail: object) -> str:
    message = str(detail)
    if 'Traceback (most recent call last)' in message or '\n  File "' in message:
        return '安全服务调用失败，请检查 securityd 日志。'
    return message


def _drop_retired_session_boundary(view: dict[str, Any]) -> dict[str, Any]:
    """Hide Session paths an older securityd still advertises.

    The Runtime drops every ``sessions/`` mount while it augments the Worker
    policy, because the control plane owns the transcript.  A securityd that
    predates that change still lists ``<agent-root>/sessions/checkpoints`` as
    writable, which made the console promise write access the Worker never
    receives.  Filter it here so the preview matches the launch policy.
    """

    retired = "<agent-root>/sessions"

    def keep(entry: object) -> bool:
        if not isinstance(entry, Mapping):
            return False
        path = str(entry.get("path", "") or "")
        return not (path == retired or path.startswith(retired + "/"))

    filtered: dict[str, Any] = dict(view)
    for group in ("read_only", "writable", "read_only_overrides", "invisible"):
        entries = view.get(group)
        if isinstance(entries, list):
            filtered[group] = [entry for entry in entries if keep(entry)]
    return filtered


def _with_worker_data_root(runtime, params: dict[str, Any]) -> dict[str, Any]:
    """给生效范围预览补上 Worker 数据根（调用方显式给出时不覆盖）。"""
    if str(params.get("worker_data_root", "") or "").strip():
        return params
    pool = getattr(runtime, "worker_supervisor", None)
    data_dir = getattr(getattr(pool, "config", None), "data_dir", None)
    if data_dir is None:
        return params
    return {**params, "worker_data_root": str(data_dir)}


async def _update_security_mode(runtime, params: dict[str, Any]) -> Any:
    """Rebuild the Worker before reporting a security mode change.

    The mode is what selects host or sandbox, so every real transition needs a
    new Worker: a sandbox Worker cannot become a host process (and vice versa),
    and the network namespace / mount table are fixed at launch.
    """
    if params.get("mode") not in {"safe", "yolo"}:
        raise ValueError("mode must be safe or yolo")
    pool = getattr(runtime, "worker_supervisor", None)
    locker = getattr(pool, "agent_lock", None)
    if not callable(locker):
        return await asyncio.to_thread(_invoke_sync, "agents.security-mode.update.v1", params)
    agent_id = str(params.get("agent_id", ""))
    async with locker(agent_id):
        worker = getattr(pool, "_supervisors", {}).get(agent_id)
        rebuild = worker is not None and getattr(worker, "ready", False)
        if rebuild:
            current = await asyncio.to_thread(_invoke_sync, "agents.security-mode.get.v1", params)
            rebuild = current.get("mode") != params["mode"]
        if rebuild:
            if pool.agent_busy(agent_id):
                raise HTTPException(409, detail={
                    "code": "agent_busy",
                    "message": "专家正在执行任务，请等待完成或停止任务后再切换安全模式。",
                })
            await worker.stop(reason="Agent security mode changed", strict=True)
        try:
            result = await asyncio.to_thread(_invoke_sync, "agents.security-mode.update.v1", params)
        except Exception:
            if rebuild:
                # Security leaves the previous mode intact on a failed update.
                await worker.start()
            raise
        if rebuild:
            # Do not report success until the Worker for the new mode is ready.
            await worker.start()
        return result


@router.post("/security-api/invoke", include_in_schema=False)
async def invoke_security(request: Request):
    body = await request.json()
    request_id = body.get("requestId", "")
    if body.get("protocolVersion") != 1 or body.get("serviceId") != "security":
        raise HTTPException(400, "unsupported security service protocol")
    if not isinstance(request_id, str) or not request_id:
        raise HTTPException(400, "requestId is required")
    operation = str(body.get("operationId", ""))
    params = body.get("params") or {}
    if not operation or not isinstance(params, dict):
        raise HTTPException(400, "operationId and params are required")
    try:
        if operation == "permissions.boundary.v1":
            # 生效范围预览要显示专家真实能碰到的目录，数据根只有 Runtime 知道；
            # 调用方不必自带这个参数。
            app = request.scope.get("app")
            runtime = getattr(getattr(app, "state", None), "runtime", None)
            params = _with_worker_data_root(runtime, params)
            value = await asyncio.to_thread(_invoke_sync, operation, params)
            value = _drop_retired_session_boundary(value)
        elif operation == "agents.security-mode.update.v1":
            app = request.scope.get("app")
            runtime = getattr(getattr(app, "state", None), "runtime", None)
            value = await _update_security_mode(runtime, params)
        else:
            value = await asyncio.to_thread(_invoke_sync, operation, params)
        result = value if isinstance(value, dict) else {"value": value}
        return {"protocolVersion": 1, "requestId": request_id,
                "ok": True, "result": result}
    except HTTPException as exc:
        # Feature Host expects a protocol envelope even when the backing
        # security service is unavailable.  Returning HTTP 503 here makes the
        # host replace the useful error with its generic "External service is
        # unavailable" message.  Keep malformed requests as HTTP errors, but
        # expose runtime/securityd failures through the versioned envelope so
        # the UI can show the actionable cause (D-Bus, UDS, ABI, etc.).
        if exc.status_code < 500:
            raise
        return {"protocolVersion": 1, "requestId": request_id, "ok": False,
                "error": {"code": "unavailable", "message": _public_security_error(exc.detail),
                           "retryable": True}}
    except ValueError as exc:
        return {"protocolVersion": 1, "requestId": request_id, "ok": False,
                "error": {"code": "invalid_request", "message": _public_security_error(exc),
                           "retryable": False}}
    except KeyError as exc:
        return {"protocolVersion": 1, "requestId": request_id, "ok": False,
                "error": {"code": "incompatible", "message": str(exc), "retryable": False}}
    except Exception as exc:
        return {"protocolVersion": 1, "requestId": request_id, "ok": False,
                "error": {"code": "unavailable", "message": _public_security_error(exc), "retryable": True}}


__all__ = ["router"]
