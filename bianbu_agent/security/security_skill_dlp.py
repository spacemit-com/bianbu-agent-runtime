"""Skill 文本 DLP 扫描（D-10 / BN-SEC-CAPMAP-001 §4.3）。

## 缺口

Skill 的 `instructions` 被拼进 system prompt（`default_engine.py`），
而 `after_context` 的 DLP 扫的是**用户输入**，不扫 Skill 注入的 context。
于是 Skill 文本里的密钥/内部信息会随每一轮请求发到云端 LLM，
且不留任何痕迹 —— 用户看不到，运维也查不到。

Skill 本身不执行代码（§4.1 已实测确认：`SkillSpec.resources` 只是路径
字符串映射表，全代码库无任何读取点），所以它不需要能力判权。但"不执行"
不等于"无风险"：文本会出境。

## 处置：脱敏而非拦截

与 `dlp_post_tool`（工具返回值）同一逻辑，而非 `dlp_pre_tool`（工具参数）：

    工具参数   必须原样  —— 脱了工具就没法用（路径里的手机号被替换会找不到文件）
    工具返回值 必须脱敏  —— 只是给模型看的文本，脱敏不影响功能
    Skill 文本 必须脱敏  ←  同上，它也只是给模型看的指令

拦截（不加载该 Skill）只留给 `action=deny` 的类型 —— 那是运维明确
标为"绝不外发"的，宁可 Skill 不可用。

不弹确认：prompt 组装不是用户发起的动作，此刻没有可问的人；
每轮 run 都问一次也不可接受。

## 缓存

Skill 文本安装后不变，按 (agent_id, sha256(文本)) 缓存扫描结果 ——
否则每轮 run 都要重扫全部 Skill 文本，是纯粹的浪费。
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger("bianbu_agent.security.skill_dlp")

__all__ = ["SkillTextRejected", "scan_skill_text", "clear_cache"]

# (agent_id, digest) → (脱敏后文本, 命中类型元组)
_CACHE: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
_CACHE_CAP = 256


class SkillTextRejected(PermissionError):
    """Skill 文本含 action=deny 的敏感类型，拒绝载入 prompt。"""

    def __init__(self, skill: str, pii_types: tuple[str, ...]):
        self.skill = skill
        self.pii_types = pii_types
        super().__init__(
            f"[bianbu-security] Skill {skill!r} 的文本含受限敏感信息"
            f"（{'、'.join(pii_types)}），已拒绝载入 system prompt")


def clear_cache() -> None:
    """清缓存。改 Skill 或改 DLP 策略后应调用。"""
    _CACHE.clear()


def _securityd_dlp():
    """Return the versioned client DLP methods, never securityd internals."""
    try:
        from bianbu_agent.security.security_runtime import security_client_module
        client = security_client_module()
        if callable(getattr(client, "dlp_scan", None)) and callable(
            getattr(client, "dlp_policy", None)
        ):
            return client
    except Exception:
        pass
    return None


_SCANNER = None
_STORE = None
def _ensure_engine():
    global _SCANNER, _STORE
    if _SCANNER is not None:
        return _SCANNER, _STORE
    got = _securityd_dlp()
    if got is None:
        logger.warning(
            "securityd DLP 不可用，拒绝把 Skill 原文放入 prompt")
        return None, None
    # The actual scan is performed by securityd through the versioned client;
    # keep the client object as the single adapter and cache no plaintext.
    _SCANNER = got
    _STORE = got
    return _SCANNER, _STORE


def _security_required() -> bool:
    """Whether a DLP outage must prevent Skill text from reaching the LLM."""
    production = str(os.environ.get("BIANBU_RUNTIME_MODE", "development")).lower() == "production"
    if production:
        return True
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return False
    return str(os.environ.get("BIANBU_RUNTIME_SECURITY", "required")).lower() \
        not in {"0", "false", "off", "optional"}


def scan_skill_text(agent_id: str, skill_name: str, text: str) -> str:
    """扫描并脱敏一段 Skill 文本，返回可安全进 prompt 的版本。

    action=deny/ask 的类型抛 SkillTextRejected；mask 的脱敏；allow 原样通过。

    DLP/securityd 不可用时生产模式拒绝载入 Skill，避免原文进入云端 prompt；
    只有显式 development + optional 配置才允许原文回退。
    """
    if not text:
        return text
    from bianbu_agent.security.security_dlp import dlp_checks_disabled
    if dlp_checks_disabled(stage='skill_text', agent_id=agent_id):
        return text
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        if _security_required():
            raise SkillTextRejected(skill_name, ("security_disabled",))
        return text

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    key = (agent_id or "", digest)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit[0]

    client, _policy = _ensure_engine()
    if client is None:
        if _security_required():
            raise SkillTextRejected(skill_name, ("securityd_unavailable",))
        return text

    try:
        response = client.dlp_scan(agent_id, text)
        findings = list(response.get("findings") or [])
    except Exception as e:  # noqa: BLE001 — fail-closed on the prompt boundary
        logger.error("Skill %s 文本扫描失败，拒绝载入 prompt：%s", skill_name, e)
        if _security_required():
            raise SkillTextRejected(skill_name, ("dlp_scan_failed",)) from e
        return text
    if not findings:
        if len(_CACHE) < _CACHE_CAP:
            _CACHE[key] = (text, ())
        return text

    blocked: list[str] = []
    to_mask: list[dict] = []
    for finding in findings:
        pii_type = str(finding.get("pii_type") or "unknown")
        action = str(finding.get("action") or "deny").lower()
        # 载入 Skill 时没有交互确认通道：ask 与 deny 一样拒绝。
        if action in ("deny", "ask"):
            blocked.append(pii_type)
        elif action == "mask":
            to_mask.append(finding)

    if blocked:
        # 不缓存拒绝结果：策略可能被改宽，改完应立即生效
        raise SkillTextRejected(skill_name, tuple(sorted(blocked)))

    masked = text
    if to_mask:
        try:
            masked = _mask_findings(text, to_mask)
        except Exception as e:  # noqa: BLE001
            logger.error("Skill %s 脱敏失败，拒绝载入 prompt：%s", skill_name, e)
            if _security_required():
                raise SkillTextRejected(skill_name, ("dlp_mask_failed",)) from e
            masked = text
        else:
            logger.info("Skill %s 文本已脱敏（%s）", skill_name,
                        "、".join(sorted(str(t.get("pii_type")) for t in to_mask)))

    if len(_CACHE) < _CACHE_CAP:
        _CACHE[key] = (masked, tuple(sorted(str(t.get("pii_type")) for t in to_mask)))
    return masked


def _mask_findings(text: str, findings: list[dict]) -> str:
    """按 securityd 返回的 action/offset 仅遮盖需 mask 的命中。"""
    counters: dict[str, int] = {}
    out = text
    for finding in sorted(findings, key=lambda item: int(item["start"]), reverse=True):
        pii_type = str(finding.get("pii_type") or "unknown")
        counters[pii_type] = counters.get(pii_type, 0) + 1
        start, end = int(finding["start"]), int(finding["end"])
        if start < 0 or end < start or end > len(out):
            raise ValueError("securityd DLP finding offset 越界")
        out = out[:start] + f"[PII-{pii_type}-{counters[pii_type]:03d}]" + out[end:]
    return out


def scan_skill_sections(agent_id: str, sections: dict[str, str]) -> dict[str, str]:
    """批量扫描 {skill_name: instructions}。

    单个 Skill 被拒不影响其余 —— 拒绝的从结果里剔除并记日志，
    否则一个含密钥的 Skill 会让整个 Agent 起不来。
    """
    out = {}
    for name, text in sections.items():
        try:
            out[name] = scan_skill_text(agent_id, name, text)
        except SkillTextRejected as e:
            logger.error("%s", e)
    return out
