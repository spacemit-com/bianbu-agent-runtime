"""Integrated Runtime security control-plane operation contracts."""
from __future__ import annotations

import pytest
from pathlib import Path
from types import SimpleNamespace
from fastapi import HTTPException

from bianbu_agent.api import security


def test_security_mode_get_operation_calls_securityd_client(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {
            "agent_id": args[0], "mode": "safe"
        },
    )

    result = security._invoke_sync(
        "agents.security-mode.get.v1", {"agent_id": "agent.demo"}
    )

    assert result == {"agent_id": "agent.demo", "mode": "safe"}
    assert calls == [("get_agent_security_mode", ("agent.demo",))]


def test_security_mode_update_operation_calls_securityd_client(monkeypatch):
    calls = []
    monkeypatch.setattr(
        security, "_call_client",
        lambda method, *args, **kwargs: calls.append((method, args)) or {
            "agent_id": args[0], "old_mode": "safe", "mode": args[1]
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


def test_audit_view_enriches_the_bounded_page(monkeypatch):
    calls = []
    page = {
        "records": [
            {"seq": 2, "ts": "2026-10-23T09:00:00+00:00",
             "agent_id": "a1", "op_type": "check_permission",
             "target": "terminal:", "result": "deny"},
            {"seq": 1, "ts": "2026-10-23T09:00:00+00:00",
             "agent_id": "a1", "op_type": "bind_caller",
             "target": ":1.2", "result": "success"},
        ],
        "snapshot_seq": 20, "has_more": True, "next_cursor": 1,
    }

    def call(method, query):
        assert method == "query_audit"
        request = security.json.loads(query)
        calls.append(request)
        rows = [row for row in page["records"]
                if not request["cursor"] or row["seq"] < request["cursor"]]
        return {**page, "records": rows, "has_more": bool(rows),
                "next_cursor": rows[-1]["seq"] if rows else None}

    monkeypatch.setattr(security, "_call_client", call)
    query = {"agent_id": "a1", "filter": "all", "limit": 10,
             "cursor": 5, "snapshot_seq": 20,
             "from": "2026-10-23T00:00:00+00:00",
             "to": "2026-10-23T23:59:59+00:00"}
    result = security._invoke_sync("audit.view.v1", query)
    assert calls[0] == {**query, "limit": 200}
    assert result["snapshot_seq"] == 20
    assert result["summary"]["total"] == 2
    assert result["summary"]["attention"] == 1
    assert result["records"][0]["category"] == "decision"
    assert result["records"][0]["severity"] == "critical"
    assert "被拒绝使用 terminal" in result["records"][0]["title"]
    assert result["records"][1]["category"] == "identity"
    assert result["records"][1]["attention"] is False


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

    with pytest.raises(ValueError, match="safe|yolo"):
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
async def test_capability_policy_change_does_not_touch_the_worker(monkeypatch):
    """allow/ask/deny is one policy row; it must not rebuild the sandbox."""

    calls = []

    class Client:
        def set_capability_policy(self, agent_id, tool, policy):
            calls.append((agent_id, tool, policy))
            return True

    monkeypatch.setattr(
        security, '_call_client',
        lambda method, *args, **kwargs: getattr(Client(), method)(*args, **kwargs),
    )

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("a policy row must not restart the Worker")

    monkeypatch.setattr(security, '_update_security_mode', forbidden)

    for operation in ('capabilities.policy.v1', 'capabilities.grant.v1'):
        result = await _invoke_envelope(operation, {
            'agent_id': 'agent', 'tool': 'terminal', 'policy': 'allow',
        })
        assert result['ok'] is True

    assert calls == [
        ('agent', 'terminal', 'allow'),
        ('agent', 'terminal', 'allow'),
    ]


@pytest.mark.asyncio
async def test_security_traceback_is_not_exposed_to_users(monkeypatch):
    monkeypatch.setattr(security, '_invoke_sync', lambda *_: (_ for _ in ()).throw(
        HTTPException(503, 'Traceback (most recent call last):\n  File "/private/deployment/client.py", line 3\nDBusException: service crashed')))
    result = await _invoke_envelope('agents.list.v1', {})
    assert result['error']['code'] == 'unavailable'
    assert 'Traceback' not in result['error']['message']
    assert '/private/' not in result['error']['message']
