"""Integrated Runtime security control-plane operation contracts."""
from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException

from bianbu_agent.api import security


def test_network_learning_operations_forward_exact_parameters(monkeypatch):
    calls = []
    events = {
        "agent_id": "agent.demo",
        "events": [{"event_type": "dns_query", "domain": "api.example.com"}],
        "total": 1,
        "next_cursor": None,
    }

    def call(method, *args, **kwargs):
        calls.append((method, args, kwargs))
        return {
            "get_network_learning_events": events,
            "get_network_learning_status": {
                "agent_id": "agent.demo", "state": "active", "reason": "",
            },
            "clear_network_learning_events": True,
        }[method]

    monkeypatch.setattr(security, "_call_client", call)
    assert security._invoke_sync(
        "network-learning.events.v1",
        {"agent_id": "agent.demo", "limit": 20, "cursor": 4},
    ) == events
    assert security._invoke_sync(
        "network-learning.status.v1", {"agent_id": "agent.demo"},
    )["state"] == "active"
    assert security._invoke_sync(
        "network-learning.clear.v1", {"agent_id": "agent.demo"},
    ) == {"ok": True}
    assert calls == [
        ("get_network_learning_events", ("agent.demo", 20, 4), {}),
        ("get_network_learning_status", ("agent.demo",), {}),
        ("clear_network_learning_events", ("agent.demo",), {}),
    ]


@pytest.mark.parametrize(
    "params,error",
    [
        ({"agent_id": "", "limit": 20, "cursor": 0}, "agent_id"),
        ({"agent_id": "agent.demo", "limit": 0, "cursor": 0}, "limit"),
        ({"agent_id": "agent.demo", "limit": 201, "cursor": 0}, "limit"),
        ({"agent_id": "agent.demo", "limit": 20, "cursor": -1}, "cursor"),
        ({"agent_id": "agent.demo", "limit": "many", "cursor": 0}, "limit"),
    ],
)
def test_network_learning_events_rejects_invalid_pagination_before_client(
    monkeypatch, params, error,
):
    monkeypatch.setattr(
        security, "_call_client",
        lambda *args, **kwargs: pytest.fail("invalid request reached securityd"),
    )
    with pytest.raises(ValueError, match=error):
        security._invoke_sync("network-learning.events.v1", params)


@pytest.mark.asyncio
async def test_old_security_learning_api_returns_incompatible_envelope(monkeypatch):
    monkeypatch.setattr(security, "_client", lambda: SimpleNamespace())
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/security-api/invoke",
             "headers": [(b"content-type", b"application/json")],
             "query_string": b"", "server": ("test", 80),
             "client": ("test", 1), "scheme": "http"}
    body = (
        b'{"protocolVersion":1,"serviceId":"security","requestId":"req-learning",'
        b'"operationId":"network-learning.status.v1",'
        b'"params":{"agent_id":"agent.demo"}}'
    )
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    response = await security.invoke_security(Request(scope, receive))
    assert response["ok"] is False
    assert response["error"]["code"] == "incompatible"
    assert response["error"]["retryable"] is False


@pytest.mark.asyncio
async def test_network_policy_learning_requires_agent_restart(monkeypatch):
    from starlette.requests import Request

    calls = []

    class Runtime:
        async def apply_network_learning_policy(self, agent_id, mode, version):
            calls.append((agent_id, mode, version))
            return {"active_instances": 1, "collectors_started": 1,
                    "collectors_stopped": 0, "degraded_instances": 0}

    monkeypatch.setattr(
        security, "_invoke_sync",
        lambda operation, params: {
            "ok": True, "agent_id": params["agent_id"],
            "mode": params["mode"], "version": 7, "domains": [],
        },
    )
    scope = {"type": "http", "method": "POST", "path": "/security-api/invoke",
             "headers": [(b"content-type", b"application/json")],
             "query_string": b"", "server": ("test", 80),
             "client": ("test", 1), "scheme": "http",
             "app": SimpleNamespace(state=SimpleNamespace(runtime=Runtime()))}
    body = (
        b'{"protocolVersion":1,"serviceId":"security","requestId":"req-policy",'
        b'"operationId":"network-policy.update.v1",'
        b'"params":{"agent_id":"agent.demo","mode":"learning",'
        b'"domains":[]}}'
    )
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    response = await security.invoke_security(Request(scope, receive))

    assert response["ok"] is True
    assert response["result"]["runtime_learning"] == {
        "restart_required": True, "instances": 0,
    }
    assert calls == []


@pytest.mark.asyncio
async def test_bwrap_restart_required_skips_runtime_hot_apply(monkeypatch):
    from starlette.requests import Request

    class Runtime:
        async def apply_network_learning_policy(self, *args):
            pytest.fail("bwrap pending-restart policy must not be hot-applied")

    monkeypatch.setattr(
        security, "_invoke_sync",
        lambda operation, params: {
            "ok": True, "agent_id": params["agent_id"],
            "mode": params["mode"], "version": 8, "domains": [],
            "apply": {"restart_required": True,
                      "restart_required_instances": 2},
        },
    )
    scope = {"type": "http", "method": "POST", "path": "/security-api/invoke",
             "headers": [(b"content-type", b"application/json")],
             "query_string": b"", "server": ("test", 80),
             "client": ("test", 1), "scheme": "http",
             "app": SimpleNamespace(state=SimpleNamespace(runtime=Runtime()))}
    body = (
        b'{"protocolVersion":1,"serviceId":"security","requestId":"req-bwrap",'
        b'"operationId":"network-policy.update.v1",'
        b'"params":{"agent_id":"com.bianbu.runtime","mode":"learning",'
        b'"domains":[]}}'
    )
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    response = await security.invoke_security(Request(scope, receive))

    assert response["ok"] is True
    assert response["result"]["runtime_learning"] == {
        "restart_required": True, "instances": 2,
    }


def test_security_mode_get_operation_calls_securityd_client(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {
            "agent_id": args[0], "mode": "normal"
        },
    )

    result = security._invoke_sync(
        "agents.security-mode.get.v1", {"agent_id": "agent.demo"}
    )

    assert result == {"agent_id": "agent.demo", "mode": "normal"}
    assert calls == [("get_agent_security_mode", ("agent.demo",))]


def test_security_mode_update_operation_calls_securityd_client(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {
            "agent_id": args[0], "old_mode": "normal", "mode": args[1]
        },
    )

    result = security._invoke_sync(
        "agents.security-mode.update.v1",
        {"agent_id": "agent.demo", "mode": "yolo"},
    )

    assert result["mode"] == "yolo"
    assert calls == [("set_agent_security_mode", ("agent.demo", "yolo"))]


def test_dlp_policy_mutations_call_securityd_client(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or True,
    )
    assert security._invoke_sync(
        "dlp.policy.update.v1",
        {"agent_id": "agent.demo", "pii_type": "phone", "action": "mask"},
    ) == {"ok": True}
    assert security._invoke_sync(
        "dlp.policy.delete.v1",
        {"agent_id": "agent.demo", "pii_type": "phone"},
    ) == {"ok": True}
    assert calls == [
        ("set_dlp_policy", ("agent.demo", "phone", "mask")),
        ("delete_dlp_policy", ("agent.demo", "phone")),
    ]

def test_dlp_matrix_only_uses_public_client_contract(monkeypatch):
    """矩阵行只来自公开的生效策略，运行时不读 securityd 内部模块。"""
    calls = []
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {
            "phone": "mask", "email": "ask", "custom_type": "deny",
        },
    )

    result = security._invoke_sync("dlp.matrix.v1", {"agent_id": "agent.demo"})

    assert calls == [("dlp_policy", ("agent.demo",))]
    assert result["agent_id"] == "agent.demo"
    rows = {row["pii_type"]: row for row in result["rows"]}
    assert set(rows) == set(security._DLP_PII_TYPES) | {"custom_type"}
    assert rows["phone"] == {"pii_type": "phone", "action": "mask"}
    assert rows["email"] == {"pii_type": "email", "action": "ask"}
    assert rows["custom_type"] == {"pii_type": "custom_type", "action": "deny"}
    # 没有显式配置的 PII 类型统一走兜底动作 mask，不再按风险等级分档。
    assert rows["id_card"] == {"pii_type": "id_card", "action": "mask"}

def test_audit_query_forwards_page_without_export_or_verification(monkeypatch):
    calls = []
    page = {"records": [{"seq": 8}], "snapshot_seq": 20, "has_more": True, "next_cursor": 8}
    def call(method, query):
        assert method == "query_audit"
        calls.append(security.json.loads(query))
        return page
    monkeypatch.setattr(security, "_call_client", call)
    query = {"agent_id": "a1", "filter": "deny", "limit": 10, "cursor": 15,
             "snapshot_seq": 20, "from": "2026-09-01", "to": "2026-10-01"}
    assert security._invoke_sync("audit.query.v1", query) == page
    assert calls == [query]


def test_audit_query_does_not_fall_back_on_old_security(monkeypatch):
    def call(method, *args):
        assert method == "query_audit"
        raise KeyError("securityd incompatible: query_audit")
    monkeypatch.setattr(security, "_call_client", call)
    with pytest.raises(KeyError, match="incompatible"):
        security._invoke_sync("audit.query.v1", {"agent_id": "a1"})


def test_audit_verification_remains_explicit(monkeypatch):
    calls = []
    def call(method, *args):
        calls.append((method, args))
        return "/tmp/audit-export.json" if method == "export_audit" else True
    monkeypatch.setattr(security, "_call_client", call)
    assert security._invoke_sync("audit.verify.v1", {"agent_id": "a1"}) == {"intact": True}
    assert [method for method, _ in calls] == ["export_audit", "verify_audit_chain"]
    assert security.json.loads(calls[0][1][0]) == {"agent_id": "a1"}

def test_capability_catalog_uses_authoritative_client(monkeypatch):
    calls=[]
    monkeypatch.setattr(security, "_call_client", lambda method,*a,**k: calls.append(method) or [{"capability":"file.read"}] if method=="capability_catalog" else [])
    assert security._invoke_sync("capabilities.catalog.v1", {}) == {"capabilities":[{"capability":"file.read"}]}
    assert calls == ["capability_catalog"]


@pytest.mark.asyncio
async def test_security_unavailable_keeps_actionable_error_in_protocol_envelope(monkeypatch):
    monkeypatch.setattr(security, "_invoke_sync", lambda *_args: (_ for _ in ()).throw(
        HTTPException(503, "securityd unavailable: dbus-python ABI requires helper bridge")
    ))
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/security-api/invoke",
             "headers": [(b"content-type", b"application/json")],
             "query_string": b"", "server": ("test", 80),
             "client": ("test", 1), "scheme": "http"}
    body = b'{"protocolVersion":1,"serviceId":"security","requestId":"req-1","operationId":"agents.list.v1","params":{}}'
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}
    response = await security.invoke_security(Request(scope, receive))
    assert response["ok"] is False
    assert "dbus-python ABI" in response["error"]["message"]


@pytest.mark.parametrize("mode", ["", "YOLO", "full", None])
def test_security_mode_update_rejects_values_outside_closed_enum(monkeypatch, mode):
    monkeypatch.setattr(
        security, "_call_client",
        lambda *args, **kwargs: pytest.fail("invalid mode reached securityd client"),
    )

    with pytest.raises(ValueError, match="normal|yolo"):
        security._invoke_sync(
            "agents.security-mode.update.v1",
            {"agent_id": "agent.demo", "mode": mode},
        )


def test_bundled_security_console_exposes_confirmed_yolo_control():
    root = Path(__file__).resolve().parents[2] / "bianbu_agent/console/web/security"
    javascript = "\n".join(path.read_text() for path in (root / "assets").glob("*.js"))

    assert "agents.security-mode.get.v1" in javascript
    assert "agents.security-mode.update.v1" in javascript
    assert "Enable YOLO mode" in javascript
    assert "Enable YOLO mode for this Agent?" in javascript


def test_bundled_security_console_keeps_yolo_out_of_configuration_panel():
    root = Path(__file__).resolve().parents[2] / "bianbu_agent/console/web/security"
    javascript = "\n".join(path.read_text() for path in (root / "assets").glob("*.js"))

    config_start = javascript.index("function mv")
    config_end = javascript.index("function gv", config_start)
    configuration_panel = javascript[config_start:config_end]
    assert "s.jsx(vv" not in configuration_panel


def test_security_console_initializes_theme_before_assets_and_styles_host_shell():
    root = Path(__file__).resolve().parents[2] / "bianbu_agent/console/web/security"
    html = (root / "index.html").read_text()
    theme_init = html.index("localStorage.getItem('bianbu_theme')")
    module = html.index('src="/security/assets/index-')
    assert theme_init < module
    assert "html.dark .bibit-dev-host__settings-shell" in html
    assert "html.dark .bibit-dev-host__settings-content" in html
    assert "html.dark .bibit-dev-host__settings-body > aside strong" in html


def test_security_console_dark_theme_styles_agent_security_mode_control():
    root = Path(__file__).resolve().parents[2] / "bianbu_agent/console/web/security"
    html = (root / "index.html").read_text()
    assert "html.dark .security-settings .security-mode-control" in html
    assert "html.dark .security-settings .security-mode-control.is-yolo" in html
    assert "security-mode-control .security-pill--active" in html


async def _invoke_envelope(operation, params):
    import json
    from starlette.requests import Request
    body = json.dumps({'protocolVersion': 1, 'serviceId': 'security', 'requestId': 'validation',
                       'operationId': operation, 'params': params}).encode()
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    return await security.invoke_security(Request({'type': 'http'}, receive))


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', ['trusted', '', 'prompt'])
async def test_invalid_capability_policy_is_invalid_not_service_outage(monkeypatch, policy):
    monkeypatch.setattr(security, '_call_client', lambda *args, **kwargs: pytest.fail('invalid policy reached securityd'))
    result = await _invoke_envelope('capabilities.update.v1', {
        'agent_id': 'agent', 'tool': 'read_file', 'policy': policy,
    })
    assert result['ok'] is False
    assert result['error']['code'] == 'invalid_request'
    assert result['error']['retryable'] is False
    assert 'policy' in result['error']['message']


@pytest.mark.asyncio
async def test_capability_update_registers_tool_policy(monkeypatch):
    """三态策略只按工具名注册，不再携带域、风险、确认策略或范围。"""
    calls = []
    class Client:
        def register_capabilities(self, agent_id, rows):
            calls.append((agent_id, rows))
            return True
    monkeypatch.setattr(security, '_call_client',
                        lambda method, *args, **kwargs: getattr(Client(), method)(*args, **kwargs))
    result = await _invoke_envelope('capabilities.update.v1', {
        'agent_id': 'agent', 'tool': 'terminal', 'policy': 'DENY', 'framework': 'runtime',
    })
    assert result['ok'] is True
    assert calls == [('agent', [{'tool': 'terminal', 'policy': 'deny', 'framework': 'runtime'}])]


@pytest.mark.asyncio
async def test_security_traceback_is_not_exposed_to_users(monkeypatch):
    monkeypatch.setattr(security, '_invoke_sync', lambda *_: (_ for _ in ()).throw(
        HTTPException(503, 'Traceback (most recent call last):\n  File "/private/deployment/client.py", line 3\nDBusException: service crashed')))
    result = await _invoke_envelope('agents.list.v1', {})
    assert result['error']['code'] == 'unavailable'
    assert 'Traceback' not in result['error']['message']
    assert '/private/' not in result['error']['message']
