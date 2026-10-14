"""安全拦截的错误透传（用户报的症状：web_search 直接返回「发送失败，请稍后再试」）。

## 症状与根因

securityd 拒绝一个工具调用时，`SecurityDenied` 带着完整信息
（tool / capability / scope / risk_level / reason）冒泡到 run_manager，
但那里一律包成 `RunError(code="run_failed")`，前端错误分支又
无条件显示 `t('error_send')` = 「发送失败，请稍后再试」。

用户看到的是"系统故障，重试可能会好"，实际是"这个操作没有授权，
重试一万次也一样"，且不知道缺哪条能力、该去哪里授权。
"""

from __future__ import annotations

import pytest

from bianbu_agent.security.security_errors import (
    PROVIDER_TIMEOUT_CODE,
    SECURITY_DEGRADED_CODE,
    SECURITY_DENIED_CODE,
    classify_run_exception,
    is_security_denial,
    security_error_payload,
)


class _Denied(PermissionError):
    """复刻插件的 SecurityDenied 形状 —— 主干不 import 插件（插件可卸载）。"""

    def __init__(self, *, tool, capability, reason, risk_level="L3",
                 scope="", decision="BLOCK", degraded=False):
        self.tool = tool
        self.capability = capability
        self.reason = reason
        self.risk_level = risk_level
        self.scope = scope
        self.decision = decision
        self.degraded = degraded
        super().__init__(f"[bianbu-security] {reason}")

    def to_metadata(self):
        return {"tool": self.tool, "capability": self.capability,
                "scope": self.scope, "risk_level": self.risk_level,
                "decision": self.decision, "reason": self.reason,
                "degraded": self.degraded}


class TestClassification:
    def test_connection_error_reports_whitelist_block_with_provider_host(self):
        got = classify_run_exception(
            RuntimeError("Connection error."),
            agent_id="agent.demo",
            provider_base_url="https://api.rvcompute.com/v1",
            network_policy={
                "mode": "whitelist",
                "domains": ["api.minimaxi.com"],
            },
        )

        assert got["code"] == "network_policy_blocked"
        assert got["details"] == {
            "agent_id": "agent.demo",
            "provider_host": "api.rvcompute.com",
            "network_mode": "whitelist",
            "domains": ["api.minimaxi.com"],
            "action": "add_provider_host_to_whitelist_and_restart_agent",
        }

    def test_connection_error_for_allowed_provider_is_not_blamed_on_policy(self):
        got = classify_run_exception(
            RuntimeError("Connection error."),
            agent_id="agent.demo",
            provider_base_url="https://api.rvcompute.com/v1",
            network_policy={
                "mode": "whitelist",
                "domains": ["api.rvcompute.com"],
            },
        )

        assert got["code"] == "provider_connection_failed"
        assert got["details"]["provider_host"] == "api.rvcompute.com"
        assert got["details"]["network_mode"] == "whitelist"

    def test_connection_error_on_allow_all_is_not_blamed_on_policy(self):
        got = classify_run_exception(
            RuntimeError("Connection error."),
            agent_id="agent.demo",
            provider_base_url="https://api.rvcompute.com/v1",
            network_policy={"mode": "allow_all", "domains": []},
        )

        assert got["code"] == "provider_connection_failed"
        assert got["details"]["network_mode"] == "allow_all"

    def test_denial_gets_own_code(self):
        exc = _Denied(tool="web_search", capability="network.access",
                      reason="能力 network.access 未授权或已撤销", risk_level="L2")
        got = classify_run_exception(exc)
        assert got["code"] == SECURITY_DENIED_CODE
        # 前端要靠 code 决定「显示真实原因」而非「通用重试文案」
        assert got["code"] != "run_failed"

    def test_message_carries_reason(self):
        exc = _Denied(tool="web_search", capability="network.access",
                      reason="能力 network.access 未授权或已撤销")
        assert "network.access" in classify_run_exception(exc)["message"]

    def test_details_locate_the_problem(self):
        """details 是"该去哪授权"的唯一线索 —— 少任一字段运维就得猜。"""
        exc = _Denied(tool="web_search", capability="network.access",
                      reason="未授权", risk_level="L2", scope="https://api.x.com")
        d = classify_run_exception(exc)["details"]
        assert d["tool"] == "web_search"
        assert d["capability"] == "network.access"
        assert d["scope"] == "https://api.x.com"
        assert d["risk_level"] == "L2"

    def test_degraded_distinguished_from_denial(self):
        """securityd 不可达的 fail-closed 与明确拒绝要分开：
        前者修好总线就能用，后者必须去授权 —— 提示文案不同。"""
        exc = _Denied(tool="terminal", capability="system.execute",
                      reason="securityd 不可达，高危能力 fail-closed 拦截",
                      risk_level="L4", degraded=True)
        assert classify_run_exception(exc)["code"] == SECURITY_DEGRADED_CODE

    def test_plain_exception_keeps_run_failed(self):
        assert classify_run_exception(RuntimeError("boom"))["code"] == "run_failed"

    def test_plain_permission_error_not_misread(self):
        """文件权限不足这类普通 PermissionError 不该被当成安全拦截 ——
        否则前端会提示"去配置台授权能力"，而问题在文件系统。"""
        assert not is_security_denial(PermissionError("文件权限不足"))
        assert classify_run_exception(PermissionError("x"))["code"] == "run_failed"

    def test_partial_attributes_not_misread(self):
        """只带部分属性的异常不算 —— 避免宽松识别把无关异常吞进安全通道。"""
        class Half(PermissionError):
            capability = "x"
        assert not is_security_denial(Half("x"))

    def test_payload_falls_back_without_to_metadata(self):
        """没有 to_metadata 也要能摊出 details（其它安全插件可能不实现它）。"""
        class Bare(PermissionError):
            tool = "t"
            capability = "c"
            reason = "r"
            risk_level = "L3"
        d = security_error_payload(Bare("boom"))["details"]
        assert d["capability"] == "c" and d["tool"] == "t"

    def test_broken_to_metadata_does_not_break_payload(self):
        class Bad(PermissionError):
            tool = "t"
            capability = "c"
            reason = "r"
            risk_level = "L3"

            def to_metadata(self):
                raise ValueError("boom")

        got = security_error_payload(Bad("x"))
        assert got["code"] == SECURITY_DENIED_CODE
        assert got["details"]["capability"] == "c"


class TestApiProjection:
    def test_console_renders_network_connection_errors_as_actionable_security_cards(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "bianbu_agent/console/web/static/js/console.js").read_text()
        start = js.find("const SECURITY_ERROR_CODES")
        end = js.find("function isSecurityErrorPayload", start)
        codes = js[start:end]

        assert "network_policy_blocked" in codes
        assert "provider_connection_failed" in codes
        assert "network_policy_blocked_title" in js
        assert "provider_connection_failed_title" in js

    def test_error_event_carries_details(self):
        """Session 终态事件保留 details，客户端 reducer 才能展示授权线索。"""
        from bianbu_agent.runs import RunError, RunResult
        from bianbu_agent.sessions.events import session_event_for_run_result

        result = RunResult(
            request_id="r1", status="failed",
            error=RunError(code=SECURITY_DENIED_CODE,
                           message="[bianbu-security] 能力 network.access 未授权",
                           details={"tool": "web_search",
                                    "capability": "network.access"}))
        event = session_event_for_run_result(
            "s1", result, seq=0, event_time=1.0,
        )
        assert event.type == "run/failed"
        assert event.data["error"]["code"] == SECURITY_DENIED_CODE
        assert event.data["error"]["details"]["capability"] == "network.access"


class TestFrontendContract:
    """前端按 code 分流，所以这两个常量是跨语言契约 —— 改名要同步改 JS。"""

    @pytest.mark.parametrize('code,policy_type,want_hint', [
        ('security_unavailable', 'dlp', 'dlp_unavailable_hint'),
        ('security_denied', 'dlp', 'dlp_denied_hint'),
        ('security_denied', '', 'security_denied_hint'),
        ('security_unavailable', '', 'security_unavailable_hint'),
    ])
    def test_renderer_selects_remediation_for_failure_type(self, code, policy_type, want_hint):
        import json
        import subprocess
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / 'bianbu_agent/console/web/static/js/console.js').read_text()
        start = js.index('function buildSecurityErrorEl(')
        renderer = js[start:js.index('\n}', start) + 2]
        script = '''
const document = {createElement: () => ({})};
const t = key => key;
const escapeHtml = text => text;
const formatTime = () => '';
'''
        script += renderer
        script += '\nconsole.log(buildSecurityErrorEl(' + json.dumps({
            'code': code, 'details': {'policy_type': policy_type},
        }) + ').innerHTML);'
        html = subprocess.run(['node', '-e', script], check=True,
                              text=True, capture_output=True).stdout
        assert want_hint in html
        if policy_type == 'dlp':
            assert 'security_denied_hint' not in html

    def test_codes_match_frontend_list(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "bianbu_agent/console/web/static/js/console.js").read_text()
        assert "SECURITY_ERROR_CODES" in js, "前端未定义安全错误码列表"
        for code in (SECURITY_DENIED_CODE, SECURITY_DEGRADED_CODE):
            assert f"'{code}'" in js, f"前端 SECURITY_ERROR_CODES 缺 {code}"

    def test_frontend_uses_dedicated_renderer(self):
        """Session reducer 的安全错误必须走专用渲染器。"""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "bianbu_agent/console/web/static/js/console.js").read_text()
        assert "function addRunErrorMessage" in js
        idx = js.find("isSecurityErrorPayload(msg?.error)")
        assert idx > 0
        branch = js[idx:idx + 800]
        assert "buildSecurityErrorEl(msg.error)" in branch, \
            "Session 安全错误仍无条件显示通用文案"

    def test_frontend_retries_worker_startup_503_for_management_catalog(self):
        """Control starts before Worker; initial catalog 503 must self-recover."""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "bianbu_agent/console/web/static/js/console.js").read_text()
        assert "worker_unavailable" in js
        assert "response.clone().json()" in js
        assert "for (let attempt = 0; attempt < 7" in js

    def test_frontend_escapes_untrusted_details(self):
        """details 里的 tool / scope 可能源自模型生成的参数（外部输入），
        必须转义 —— 走 escapeHtml，不能进 renderMarkdown。"""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "bianbu_agent/console/web/static/js/console.js").read_text()
        start = js.find("function buildSecurityErrorEl")
        assert start > 0
        body = js[start:start + 3000]
        assert "escapeHtml" in body
        assert "renderMarkdown" not in body, \
            "安全提示里不能对外部输入走 markdown 渲染"

    def test_frontend_exchanges_production_admin_session(self):
        """Management requests must authenticate without blocking chat Runs."""
        from pathlib import Path

        js = (Path(__file__).resolve().parents[2]
              / "bianbu_agent/console/web/static/js/console.js").read_text()
        assert "auth/session" in js
        assert "credentials: 'same-origin'" in js
        assert "function consoleFetch" in js
        assert "const fetch = consoleFetch" in js
        assert "PUBLIC_API_BASE}/runs" not in js
        assert "PUBLIC_API_BASE}/sessions" in js


@pytest.mark.parametrize("mode,domains,code", [
    ("deny_all", [], "network_policy_blocked"),
    ("whitelist", ["example.com"], "network_policy_blocked"),
    # 策略放行了这个域名，超时就不再是「没授权」，而是 Provider 真的没回包。
    ("whitelist", ["api.minimaxi.com"], PROVIDER_TIMEOUT_CODE),
    ("allow_all", [], PROVIDER_TIMEOUT_CODE),
    ("learning", [], PROVIDER_TIMEOUT_CODE),
])
def test_model_timeout_uses_policy_evidence(mode, domains, code):
    result = classify_run_exception(
        RuntimeError("model response timed out after 120s"),
        provider_base_url="https://api.minimaxi.com/v1",
        network_policy={"mode": mode, "domains": domains},
    )
    assert result["code"] == code


def test_unrelated_tool_timeout_is_not_a_network_denial():
    """工具自己的超时与模型服务无关 —— 即便策略是 deny_all 也不能算到它头上。"""
    result = classify_run_exception(
        TimeoutError("tool timed out"),
        provider_base_url="https://example.com",
        network_policy={"mode": "deny_all"},
    )
    assert result["code"] == "run_failed"


# ── 模型服务连不上 / 没回包 ────────────────────────────────────────
# 用户报的症状：Provider 挂掉时 Run 只回裸的 run_failed + 英文原文
# "Connection error." / "Request timed out."，既不知道是谁连不上，
# 也不知道该做什么；而且只有恰好配了网络策略才会分类。

def _transport_error(kind: str):
    """真实的 openai/httpx 传输层异常 —— 分类靠类型链，不靠文案。"""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.rvcompute.com/v1/chat/completions")
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    return openai.APIConnectionError(request=request)


@pytest.mark.parametrize("kind,code", [
    ("connection", "provider_connection_failed"),
    ("timeout", PROVIDER_TIMEOUT_CODE),
])
def test_provider_transport_failure_is_actionable_without_network_policy(kind, code):
    """没有策略证据（BIANBU_SECURITY=0 / Worker 没收到策略快照）也要给出可执行错误。"""
    result = classify_run_exception(
        _transport_error(kind),
        agent_id="agent.demo",
        provider_base_url="https://api.rvcompute.com/v1",
        network_policy=None,
    )

    assert result["code"] == code
    assert result["details"]["provider_host"] == "api.rvcompute.com"
    assert result["details"]["action"] == "check_network_provider_and_retry"
    assert "api.rvcompute.com" in result["message"]
    assert "run_failed" != result["code"]


def test_model_idle_timeout_reports_the_waited_seconds():
    """Run 的模型空闲计时到期同样属于 Provider 超时，不是泛指的系统故障。"""
    result = classify_run_exception(
        RuntimeError("model response timed out after 120s"),
        agent_id="agent.demo",
        provider_base_url="https://api.rvcompute.com/v1",
        network_policy=None,
    )

    assert result["code"] == PROVIDER_TIMEOUT_CODE
    assert result["details"]["timeout_seconds"] == 120.0
    assert "120" in result["message"]


def test_provider_timeout_is_not_conflated_with_policy_blocking():
    """超时不是"没授权"，提示里不能把人引去改网络策略。"""
    result = classify_run_exception(
        _transport_error("timeout"),
        provider_base_url="https://api.rvcompute.com/v1",
        network_policy={"mode": "whitelist", "domains": ["api.rvcompute.com"]},
    )

    assert result["code"] == PROVIDER_TIMEOUT_CODE
    assert "白名单" not in result["message"]


@pytest.mark.parametrize("exc", [
    TimeoutError("tool timed out"),
    TimeoutError("terminal command reached its own timeout"),
    RuntimeError("Connection error."),
])
def test_tool_side_failures_are_never_blamed_on_the_provider(exc):
    """工具自己的超时/连接文案与 Provider 无关，必须留在 run_failed。"""
    result = classify_run_exception(
        exc, provider_base_url="https://api.rvcompute.com/v1", network_policy=None,
    )

    assert result["code"] == "run_failed"


def test_console_renders_provider_timeout_as_its_own_card():
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2]
          / "bianbu_agent/console/web/static/js/console.js").read_text()
    codes = js[js.index("const SECURITY_ERROR_CODES"):]
    codes = codes[:codes.index("]")]

    assert "provider_timeout" in codes
    for key in ("provider_timeout_title", "provider_timeout_hint"):
        assert js.count(f"{key}:") >= 2, f"{key} 需要中英文双语"


@pytest.mark.parametrize("snapshot", [
    '{"agent_id":"agent.demo","mode":"allow_all","domains":[]}',
    '{"agent_id":"another.agent","mode":"deny_all"}',
    "invalid-json", "[]", "",
])
def test_managed_worker_diagnoses_startup_policy_without_control_plane(monkeypatch, snapshot):
    import json
    from bianbu_agent.runs.manager import _agent_network_policy
    from bianbu_agent.security import security_runtime

    calls = []

    class Client:
        def get_agent_security_mode(self, agent_id):
            return {"mode": "normal"}

        def get_network_policy(self, agent_id):
            calls.append(True)
            raise AssertionError("Worker must not query desired policy over D-Bus")

    def unavailable():
        return Client()

    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_WORKER_MANAGED", "1")
    monkeypatch.setenv("BIANBU_WORKER_NETWORK_POLICY", snapshot)
    monkeypatch.setattr(security_runtime, "security_client_module", unavailable)
    result = _agent_network_policy("agent.demo")
    assert calls == []
    if '"agent.demo"' in snapshot:
        assert result == json.loads(snapshot)
    else:
        assert result is None


# ── retryable：由 Runtime 判定，客户端不再按 code 猜 ─────────────────
#
# 之前只有 code 可用，客户端只能自己维护「哪些 code 不该重试」的清单，
# 新错误码一出现就漂移。判定放在产生错误的一方，语义才只有一份。

class TestRetryable:
    def test_denial_is_not_retryable(self):
        """明确的拒绝要先去授权，原样重试一万次也一样。"""
        exc = _Denied(tool="t", capability="c", reason="r")
        assert classify_run_exception(exc)["retryable"] is False

    def test_degraded_security_service_is_retryable(self):
        """securityd 不可达是 fail-closed 的临时状态，服务恢复后重试可能成功。"""
        exc = _Denied(tool="t", capability="c", reason="r", degraded=True)
        assert classify_run_exception(exc)["retryable"] is True

    def test_skill_unavailable_is_not_retryable(self):
        from bianbu_agent.security.security_errors import SkillUnavailableError

        payload = classify_run_exception(SkillUnavailableError(("web-search",)))
        assert payload["retryable"] is False

    def test_network_policy_block_is_not_retryable(self):
        payload = classify_run_exception(
            ConnectionError("Connection error."),
            provider_base_url="https://api.example.com/v1",
            network_policy={"mode": "deny_all", "domains": []},
        )
        assert payload["code"] == "network_policy_blocked"
        assert payload["retryable"] is False

    def test_provider_transport_failure_is_retryable(self):
        payload = classify_run_exception(
            _transport_error("connection"),
            provider_base_url="https://api.rvcompute.com/v1",
            network_policy=None,
        )
        assert payload["code"] == "provider_connection_failed"
        assert payload["retryable"] is True

    def test_unclassified_failure_stays_retryable(self):
        assert classify_run_exception(RuntimeError("boom"))["retryable"] is True


# ── 空消息：客户端不能只剩一个看不懂的 code ─────────────────────────

class TestEmptyMessage:
    def test_empty_exception_message_falls_back_to_the_type_name(self):
        payload = classify_run_exception(TimeoutError())
        assert payload["code"] == "run_failed"
        assert payload["message"] == "TimeoutError"
        assert payload["retryable"] is True

    def test_security_payload_without_a_message_stays_readable(self):
        class Bare(PermissionError):
            tool = "terminal"
            capability = "terminal.execute"
            reason = "未授权"
            risk_level = "L2"

        payload = security_error_payload(Bare())
        assert payload["message"] == "安全策略拒绝了本次操作"
        assert payload["retryable"] is False

    def test_run_error_backstop_fills_an_empty_message(self):
        from bianbu_agent.runs import RunError

        assert RunError("custom_code", "   ").message == "custom_code"
