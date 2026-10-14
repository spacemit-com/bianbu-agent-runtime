"""Security feature bridge used by the integrated WebUI.

The bridge talks directly to ``bianbu_security_client`` (D-Bus/UDS), never to
the standalone security WebUI service. Requests use Runtime-defined operation IDs.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import os
import subprocess
from pathlib import Path
from typing import Any

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

@router.get('/api/v1/security/syscalls/catalog')
async def syscall_catalog(arch: str = 'x86_64'):
    return await asyncio.to_thread(_invoke_sync, 'syscall-catalog.v1', {'arch': arch})

@router.get('/api/v1/security/agents/{agent_id}/syscall-policy')
async def syscall_policy(agent_id: str, arch: str = 'x86_64'):
    return await asyncio.to_thread(_invoke_sync, 'syscall-policy.get.v1', {'agent_id': agent_id, 'arch': arch})

@router.post('/api/v1/security/agents/{agent_id}/syscall-policy')
async def update_syscall_policy(agent_id: str, request: Request):
    body = await request.json()
    return await asyncio.to_thread(_invoke_sync, 'syscall-policy.agent.update.v1', {'agent_id': agent_id, 'arch': body.get('arch', 'x86_64'), 'additions': body.get('additions', [])})

@router.delete('/api/v1/security/agents/{agent_id}/syscall-policy')
async def reset_syscall_policy(agent_id: str, arch: str = 'x86_64'):
    return await asyncio.to_thread(_invoke_sync, 'syscall-policy.agent.reset.v1', {'agent_id': agent_id, 'arch': arch})

@router.get('/api/v1/security/syscall-audit/events')
async def syscall_audit_events(agent_id: str = '', instance_id: str = '', limit: int = 100, cursor: int = 0):
    return await asyncio.to_thread(_invoke_sync, 'syscall-audit.events.v1', {
        'agent_id': agent_id, 'instance_id': instance_id, 'limit': limit, 'cursor': cursor})

@router.get('/api/v1/security/syscall-audit/status')
async def syscall_audit_status():
    return await asyncio.to_thread(_invoke_sync, 'syscall-audit.status.v1', {})


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
    if operation == "agents.get.v1":
        return sc.get_agent_info(aid)
    if operation == "agents.suspend.v1":
        return {"ok": bool(sc.suspend_agent(aid, str(p.get("reason", "webui"))))}
    if operation == "agents.security-mode.get.v1":
        return sc.get_agent_security_mode(aid)
    if operation == "agents.security-mode.update.v1":
        mode = p.get("mode")
        if mode not in {"normal", "yolo"}:
            raise ValueError("mode must be normal or yolo")
        return sc.set_agent_security_mode(aid, mode)
    if operation in {"capabilities.policy.v1", "capabilities.grant.v1"}:
        # 一个工具一条策略：allow / ask / deny。旧名 grant.v1 作为兼容入口，
        # 语义收敛到同一个三态策略更新。
        policy = str(p.get("policy", "") or "").strip().lower()
        if policy not in {"allow", "ask", "deny"}:
            raise ValueError("policy 必须是 allow / ask / deny")
        return {"ok": bool(sc.set_capability_policy(aid, str(p.get("tool", p.get("capability", ""))), policy))}
    if operation == "capabilities.update.v1":
        tool = str(p.get("tool", p.get("capability", ""))).strip()
        if not tool:
            raise ValueError("tool 不能为空")
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
    if operation == "permissions.get.v1":
        # 五组安全权限设置：可读/可写/网络/系统调用/自定义。
        return sc.get_security_permissions(aid)
    if operation == "permissions.set-group.v1":
        group = str(p.get("group", "") or "")
        if group == "network":
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
    if operation.startswith("network-learning."):
        aid = aid.strip()
        if not aid:
            raise ValueError("agent_id is required")
        if operation == "network-learning.events.v1":
            try:
                limit = int(p.get("limit", 100))
            except (TypeError, ValueError) as exc:
                raise ValueError("limit must be an integer between 1 and 200") from exc
            try:
                cursor = int(p.get("cursor", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("cursor must be a non-negative integer") from exc
            if limit < 1 or limit > 200:
                raise ValueError("limit must be between 1 and 200")
            if cursor < 0:
                raise ValueError("cursor must be non-negative")
            return sc.get_network_learning_events(aid, limit, cursor)
        if operation == "network-learning.status.v1":
            return sc.get_network_learning_status(aid)
        if operation == "network-learning.clear.v1":
            return {"ok": bool(sc.clear_network_learning_events(aid))}
    if operation == "syscall-catalog.v1":
        return sc.get_syscall_catalog(str(p.get("arch", "x86_64")))
    if operation == "syscall-policy.get.v1":
        return sc.get_syscall_policy(aid, str(p.get("arch", "x86_64")))
    if operation == "syscall-policy.agent.update.v1":
        return sc.set_agent_syscall_policy(aid, str(p.get("arch", "x86_64")), list(p.get("additions", [])))
    if operation == "syscall-policy.agent.reset.v1":
        return sc.reset_agent_syscall_policy(aid, str(p.get("arch", "x86_64")))
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
    if operation == "audit.verify.v1":
        path = sc.export_audit(json.dumps({"agent_id": aid} if aid else {}), "json")
        return {"intact": bool(sc.verify_audit_chain(path))}
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


async def _update_security_mode(runtime, params: dict[str, Any]) -> Any:
    """Recreate immutable sandbox networking before confirming a mode change."""
    if params.get("mode") not in {"normal", "yolo"}:
        raise ValueError("mode must be normal or yolo")
    pool = getattr(runtime, "worker_supervisor", None)
    locker = getattr(pool, "agent_lock", None)
    if not callable(locker):
        return await asyncio.to_thread(_invoke_sync, "agents.security-mode.update.v1", params)
    agent_id = str(params.get("agent_id", ""))
    async with locker(agent_id):
        worker = getattr(pool, "_supervisors", {}).get(agent_id)
        record = getattr(worker, "record", None)
        rebuild = bool(record is not None and record.sandboxed)
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
            # Do not report success until the new namespace is ready.
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
        if operation == "agents.security-mode.update.v1":
            app = request.scope.get("app")
            runtime = getattr(getattr(app, "state", None), "runtime", None)
            value = await _update_security_mode(runtime, params)
        else:
            value = await asyncio.to_thread(_invoke_sync, operation, params)
        result = value if isinstance(value, dict) else {"value": value}
        if operation == "agents.security-mode.update.v1":
            observe = getattr(runtime, "apply_network_learning_policy", None)
            if callable(observe):
                try:
                    # Observation is independent of restrictions: YOLO records
                    # destinations using the existing passive learning collector.
                    if result.get("mode") == "yolo":
                        network = {"mode": "learning", "version": 0}
                    else:
                        network = await asyncio.to_thread(_invoke_sync, "network-policy.get.v1", params)
                    result["runtime_learning"] = await observe(
                        str(params.get("agent_id", "")), str(network.get("mode", "allow_all")),
                        int(network.get("version", 0) or 0))
                except Exception as exc:
                    # Collection failure must not re-enable YOLO restrictions.
                    result["runtime_learning"] = {"degraded_instances": 1, "reason": str(exc)}
        if operation == "network-policy.update.v1" and result.get("ok"):
            apply_status = result.get("apply") or {}
            # Learning is loaded through LD_PRELOAD when the Agent starts.
            # Keep the contract explicit: saving the policy never claims that
            # an already-running host Worker began collecting immediately.
            if str(params.get("mode", "")).strip().lower() == "learning":
                result["runtime_learning"] = {
                    "restart_required": True,
                    "instances": int(
                        apply_status.get("restart_required_instances", 0) or 0
                    ),
                }
                return {"protocolVersion": 1, "requestId": request_id,
                        "ok": True, "result": result}
            if apply_status.get("restart_required"):
                result["runtime_learning"] = {
                    "restart_required": True,
                    "instances": int(
                        apply_status.get("restart_required_instances", 0) or 0
                    ),
                }
                return {"protocolVersion": 1, "requestId": request_id,
                        "ok": True, "result": result}
            apply_learning = getattr(
                request.app.state.runtime, "apply_network_learning_policy", None,
            )
            if callable(apply_learning):
                try:
                    result["runtime_learning"] = await apply_learning(
                        str(result.get("agent_id") or params.get("agent_id") or ""),
                        "learning" if result.get("observe") is True else str(
                            result.get("mode") or params.get("mode") or "whitelist"),
                        int(result.get("version", 0) or 0),
                    )
                except Exception as exc:
                    result["runtime_learning"] = {
                        "active_instances": 0, "collectors_started": 0,
                        "collectors_stopped": 0, "degraded_instances": 1,
                        "reason": str(exc),
                    }
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
