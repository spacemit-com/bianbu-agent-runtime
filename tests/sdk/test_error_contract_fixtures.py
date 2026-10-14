"""错误契约 fixture：Runtime 产出的错误对象必须与 fixture 完全一致。

`tests/fixtures/error-contract/` 同时被 Bibit 的投影测试读取，是跨端契约的
单一事实来源。改这里的期望值等于改协议，两端测试会同时失败 —— 这正是想要
的效果：错误字段不能再单方面漂移。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from bianbu_agent.runs import RunError, encode_error
from bianbu_agent.security.security_errors import (
    SkillUnavailableError,
    classify_run_exception,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "error-contract"
FIXTURE_NAMES = sorted(path.stem for path in FIXTURE_DIR.glob("*.json"))


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


class _SecurityDenied(PermissionError):
    """复刻 resources/plugins/bianbu.security/lib/errors.py 的形状。

    主干不 import 插件（插件可卸载），分类器按属性识别，所以测试也按形状复刻。
    """

    def __init__(self, *, tool: str, capability: str, reason: str,
                 risk_level: str = "L3", scope: str = "",
                 decision: str = "BLOCK", degraded: bool = False) -> None:
        self.tool = tool
        self.capability = capability
        self.reason = reason
        self.risk_level = str(risk_level or "L3").upper()
        self.scope = scope
        self.decision = decision
        self.degraded = degraded
        super().__init__(f"[bianbu-security] {reason}")

    @property
    def should_terminate_run(self) -> bool:
        return self.risk_level in {"L3", "L4"}

    def to_metadata(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "scope": self.scope,
            "risk_level": self.risk_level,
            "decision": self.decision,
            "reason": self.reason,
            "degraded": self.degraded,
            "terminate_run": self.should_terminate_run,
        }


def _transport_error(kind: str) -> BaseException:
    """真实的 openai/httpx 传输层异常：分类靠类型链，不靠文案。"""
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    if kind == "timeout":
        return openai.APITimeoutError(request=request)
    return openai.APIConnectionError(request=request)


CLASSIFIERS: dict[str, Callable[[], dict[str, Any]]] = {
    "security-denied": lambda: classify_run_exception(_SecurityDenied(
        tool="web_search", capability="network.access",
        reason="能力 network.access 未授权或已撤销",
        risk_level="L2", scope="https://api.example.com",
    )),
    "security-degraded": lambda: classify_run_exception(_SecurityDenied(
        tool="terminal", capability="terminal.execute",
        reason="securityd 不可达，按失败关闭策略拦截",
        risk_level="L3", degraded=True,
    )),
    "skill-unavailable": lambda: classify_run_exception(
        SkillUnavailableError(("web-search", "doc-reader"))
    ),
    "network-policy-blocked": lambda: classify_run_exception(
        ConnectionError("Connection error."),
        agent_id="agent.demo",
        provider_base_url="https://api.example.com/v1",
        network_policy={"mode": "deny_all", "domains": []},
    ),
    "provider-timeout": lambda: classify_run_exception(
        _transport_error("timeout"),
        agent_id="agent.demo",
        provider_base_url="https://api.example.com/v1",
        network_policy=None,
    ),
    "provider-connection-failed": lambda: classify_run_exception(
        _transport_error("connection"),
        agent_id="agent.demo",
        provider_base_url="https://api.example.com/v1",
        network_policy=None,
    ),
    "empty-message-exception": lambda: classify_run_exception(TimeoutError()),
}


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_declares_the_shared_fields(name: str) -> None:
    """每个 fixture 都必须带完整错误对象：客户端不允许收到残缺的错误。"""
    fixture = load_fixture(name)
    assert set(fixture) >= {"name", "description", "error"}
    assert fixture["name"] == name
    for key in ("code", "message", "retryable", "details"):
        assert key in fixture["error"], f"{name}: error 缺 {key}"
    assert str(fixture["error"]["message"]).strip(), f"{name}: message 不能为空"
    assert isinstance(fixture["error"]["retryable"], bool)
    assert isinstance(fixture["error"]["details"], dict)


@pytest.mark.parametrize("name", sorted(CLASSIFIERS))
def test_classifier_matches_the_fixture(name: str) -> None:
    fixture = load_fixture(name)
    assert CLASSIFIERS[name]() == fixture["error"]


# 不是分类器产生的 fixture：一个是过程中的诊断事件，一个是 RPC 协议失败。
PROTOCOL_FIXTURES = {"mid-run-diagnostic", "worker-rpc-interrupted"}


def test_every_fixture_is_executed_by_a_runtime_test() -> None:
    """新增 fixture 必须同时接上一个测试，否则它只是一份没人执行的文档。"""
    assert set(FIXTURE_NAMES) == set(CLASSIFIERS) | PROTOCOL_FIXTURES


@pytest.mark.parametrize("name", sorted(PROTOCOL_FIXTURES))
def test_protocol_fixture_round_trips_through_the_shared_encoding(name: str) -> None:
    """协议侧错误也走同一份编码：客户端不需要第二套读取逻辑。"""
    fixture = load_fixture(name)
    for payload in (fixture.get("diagnostic"), fixture["error"]):
        if payload is None:
            continue
        error = RunError(**payload)
        assert encode_error(error) == payload


def test_encode_error_keeps_retryable_and_details_apart() -> None:
    error = RunError(code="security_denied", message="denied", retryable=False,
                     details={"capability": "network.access"})
    assert encode_error(error) == {
        "code": "security_denied",
        "message": "denied",
        "retryable": False,
        "details": {"capability": "network.access"},
    }
