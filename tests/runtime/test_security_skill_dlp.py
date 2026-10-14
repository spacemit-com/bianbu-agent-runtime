from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import bianbu_agent.security.security_skill_dlp as skill_dlp


@pytest.fixture(autouse=True)
def _reset_skill_dlp(monkeypatch):
    skill_dlp.clear_cache()
    monkeypatch.setattr(skill_dlp, "_SCANNER", None)
    monkeypatch.setattr(skill_dlp, "_STORE", None)
    yield
    skill_dlp.clear_cache()


def _client(response=None, error: Exception | None = None):
    def dlp_scan(_agent, _text):
        if error is not None:
            raise error
        return response or {"findings": []}

    return SimpleNamespace(dlp_scan=dlp_scan, dlp_policy=lambda _agent: {})


def test_skill_text_uses_securityd_offsets_and_masks_only_mask_findings(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_RUNTIME_SECURITY", "required")
    monkeypatch.setitem(sys.modules, "bianbu_security_client", _client({
        "findings": [{
            "pii_type": "phone", "start": 3, "end": 14,
            "action": "mask",
        }, {
            "pii_type": "email", "start": 19, "end": 35,
            "action": "audit_only",
        }],
    }))

    result = skill_dlp.scan_skill_text(
        "com.test", "guide", "秘钥 13812345678 邮箱 a@example.test")

    assert "13812345678" not in result
    assert "[PII-phone-001]" in result
    assert "a@example.test" in result


def test_skill_text_fails_closed_when_securityd_unavailable(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_RUNTIME_SECURITY", "required")
    monkeypatch.delenv("BIANBU_SECURITYD_HOME", raising=False)
    monkeypatch.setitem(sys.modules, "bianbu_security_client", None)

    with pytest.raises(skill_dlp.SkillTextRejected, match="securityd_unavailable"):
        skill_dlp.scan_skill_text("com.test", "guide", "secret instruction")


def test_skill_text_fails_closed_when_scan_errors(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_RUNTIME_SECURITY", "required")
    monkeypatch.setitem(
        sys.modules, "bianbu_security_client", _client(error=RuntimeError("UDS down"))
    )

    with pytest.raises(skill_dlp.SkillTextRejected, match="dlp_scan_failed"):
        skill_dlp.scan_skill_text("com.test", "guide", "secret instruction")


def test_explicit_development_optional_allows_text_fallback(monkeypatch):
    monkeypatch.setenv("BIANBU_SECURITY", "1")
    monkeypatch.setenv("BIANBU_RUNTIME_MODE", "development")
    monkeypatch.setenv("BIANBU_RUNTIME_SECURITY", "optional")
    monkeypatch.setitem(sys.modules, "bianbu_security_client", None)

    assert skill_dlp.scan_skill_text("com.test", "guide", "dev text") == "dev text"
