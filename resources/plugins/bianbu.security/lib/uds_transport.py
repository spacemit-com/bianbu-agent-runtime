"""Runtime 到 securityd 正式 UDS 数据面的混合 DLP/审计适配。

小文本仍在 Runtime 进程内扫描以降低固定开销；超过 ARCH-01 阈值的文本交给
securityd `dlp.scan`。DLP 策略始终优先从 securityd 中央策略库读取，避免生产
环境中 Runtime 与 securityd 使用不同 UID 时各自打开一份 SQLite 而发生策略分叉。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Iterable

logger = logging.getLogger("bianbu.security.uds")

__all__ = [
    "managed_worker_identity",
    "SecurityUdsTransport",
    "HybridDLPPolicyStore",
    "HybridPIIScanner",
]


def managed_worker_identity() -> dict:
    """受管 Worker 在 UDS 数据面上必须携带的三元身份。

    securityd `uds_handlers._require_agent` 用 `_worker_security_agent_id`（Runtime
    安全身份，如 com.bianbu.runtime）去匹配已提交的 lease；缺了它会退回用逻辑
    Agent ID 查 lease，而 lease 归属 Runtime 身份，于是每次 dlp.scan / dlp.policy /
    audit.batch 都被 "Worker 未通过已提交 Runtime lease 身份校验" 拒绝，PA 静默
    回退本地扫描与本地策略库（2026-09-11 验收缺陷 D-1）。
    与 securityd 客户端 `bianbu_security_client._worker_uds_call` 保持同一组字段。
    """
    return {
        "_worker_instance_id": os.environ.get("BIANBU_WORKER_INSTANCE_ID", ""),
        "_worker_lease_id": os.environ.get("BIANBU_WORKER_LEASE_ID", ""),
        "_worker_security_agent_id": os.environ.get("BIANBU_WORKER_SECURITY_AGENT_ID", ""),
    }


class SecurityUdsTransport:
    """每次调用使用独立连接，天然支持 Runtime 线程池并发。"""

    def __init__(self, client_factory=None, *, worker_identity=None):
        if client_factory is None:
            from bianbu_security_uds import UdsClient  # noqa: PLC0415

            client_factory = UdsClient
        self._client_factory = client_factory
        self._worker_identity = dict(worker_identity or {})
        if not self._worker_identity and os.environ.get("BIANBU_WORKER_MANAGED") == "1":
            self._worker_identity = managed_worker_identity()

    def call(self, request: dict) -> dict:
        # Lease coordinates are immutable process identity.  A tool or prompt
        # must never be able to replace them by adding fields to a UDS call.
        request = {**dict(request), **self._worker_identity}
        with self._client_factory() as client:
            response = client.call(request)
        if not isinstance(response, dict):
            raise RuntimeError("securityd UDS 返回值不是 JSON object")
        if response.get("ok") is not True:
            raise RuntimeError(str(response.get("error") or "securityd UDS 调用失败"))
        return response


class HybridDLPPolicyStore:
    """securityd 中央策略优先；不可达时缓存优先并默认 fail-closed。"""

    def __init__(self, local_store, transport: SecurityUdsTransport,
                 *, cache_ttl: float | None = None):
        from securityd.dlp.policy_store import DLPAction  # noqa: PLC0415

        self.local_store = local_store
        self.transport = transport
        self._action = DLPAction
        self.cache_ttl = float(
            cache_ttl if cache_ttl is not None
            else os.environ.get("BIANBU_AGENT_DLP_POLICY_TTL", "5"))
        self._cache: dict[str, tuple[float, dict[str, str]]] = {}
        self._lock = threading.Lock()

    def update_remote_policy(self, agent_id: str, matrix: dict) -> dict[str, str]:
        valid_actions = {action.value for action in self._action}
        normalized = {
            str(key): str(value) for key, value in (matrix or {}).items()
            if str(value) in valid_actions
        }
        if not normalized:
            raise ValueError("securityd 未返回有效 DLP 策略")
        with self._lock:
            self._cache[str(agent_id)] = (time.monotonic(), normalized)
        return normalized

    def _cached(self, agent_id: str, *, allow_stale: bool = False):
        with self._lock:
            item = self._cache.get(str(agent_id))
        if item is None:
            return None
        created, matrix = item
        if allow_stale or time.monotonic() - created <= self.cache_ttl:
            return matrix
        return None

    def _matrix(self, agent_id: str) -> dict[str, str]:
        cached = self._cached(agent_id)
        if cached is not None:
            return cached
        try:
            response = self.transport.call({
                "op": "dlp.policy", "agent_id": str(agent_id)})
            return self.update_remote_policy(
                agent_id, response.get("effective_policy") or {})
        except Exception as exc:  # noqa: BLE001 — 采用缓存或 fail-closed
            stale = self._cached(agent_id, allow_stale=True)
            if stale is not None:
                logger.warning("中央 DLP 策略刷新失败，使用最近缓存：%s", exc)
                return stale
            if os.environ.get("BIANBU_AGENT_DLP_POLICY_LOCAL_FALLBACK") == "1":
                logger.warning("中央 DLP 策略不可达，显式使用本地开发回退：%s", exc)
                return self.local_store.effective_matrix(agent_id)
            logger.error("中央 DLP 策略不可达且无缓存，按 BLOCK fail-closed：%s", exc)
            from securityd.dlp.pii_scanner import PIIType  # noqa: PLC0415

            return {pii_type.value: self._action.BLOCK.value for pii_type in PIIType}

    def get_action(self, agent_id, pii_type):
        value = self._matrix(str(agent_id)).get(pii_type.value)
        return self._action(value) if value else self._action.BLOCK

    def effective_matrix(self, agent_id: str) -> dict[str, str]:
        return dict(self._matrix(agent_id))

    def enabled_types(self, agent_id: str):
        from securityd.dlp.pii_scanner import PIIType  # noqa: PLC0415

        return {pii_type for pii_type in PIIType
                if self.get_action(agent_id, pii_type) != self._action.ALLOW}

    def set_from_manifest(self, agent_id: str, dlp_section: dict) -> int:
        # manifest 仅更新本地故障后备；在线策略由 securityd 管理面统一控制。
        return self.local_store.set_from_manifest(agent_id, dlp_section)

    def close(self) -> None:
        self.local_store.close()


class HybridPIIScanner:
    """保持 PIIScanner 接口，并为 handler 提供带 agent_id 的 UDS 扩展。"""

    def __init__(self, local_scanner, transport: SecurityUdsTransport,
                 policy_store: HybridDLPPolicyStore):
        self.local_scanner = local_scanner
        self.transport = transport
        self.policy_store = policy_store
        self.ner_backend = local_scanner.ner_backend
        self.min_confidence = local_scanner.min_confidence

    @staticmethod
    def _payload_bytes(text: str) -> int:
        return len(text.encode("utf-8"))

    def _use_uds(self, text: str) -> bool:
        from bianbu_security_uds import should_use_uds  # noqa: PLC0415

        return should_use_uds(self._payload_bytes(text))

    def _from_response(self, agent_id: str, text: str, response: dict):
        from securityd.dlp.pii_scanner import (  # noqa: PLC0415
            PIIFinding, PIIType, RiskLevel, ScanResult,
        )

        self.policy_store.update_remote_policy(
            agent_id, response.get("effective_policy") or {})
        findings = []
        for item in response.get("findings") or []:
            start, end = int(item["start"]), int(item["end"])
            if start < 0 or end < start or end > len(text):
                raise ValueError(f"securityd DLP finding 越界: {start}:{end}")
            findings.append(PIIFinding(
                pii_type=PIIType(str(item["pii_type"])),
                start=start,
                end=end,
                value=text[start:end],
                confidence=float(item.get("confidence", 0.0)),
                risk_level=RiskLevel(str(item["risk_level"])),
                detector=str(item.get("detector") or "uds"),
            ))
        return ScanResult(findings=findings,
                          masked_text=str(response.get("masked_text", text)))

    def scan_for_agent(self, agent_id: str, text: str):
        if not self._use_uds(text):
            return self.local_scanner.scan(text)
        try:
            response = self.transport.call({
                "op": "dlp.scan", "agent_id": str(agent_id), "text": text})
            return self._from_response(agent_id, text, response)
        except Exception as exc:  # noqa: BLE001 — 扫描算法本地等价后备，不绕过 DLP
            logger.error("大载荷 UDS DLP 扫描失败，使用本地安全后备：%s", exc)
            return self.local_scanner.scan(text)

    @staticmethod
    def _mask_findings(text: str, findings: Iterable):
        from securityd.dlp.pii_scanner import PIIScanner, ScanResult  # noqa: PLC0415

        ordered = sorted(findings, key=lambda finding: finding.start)
        if not ordered:
            return ScanResult(findings=[], masked_text=text, mask_map={})
        counters, parts, mask_map, cursor = {}, [], {}, 0
        for finding in ordered:
            counters[finding.pii_type] = counters.get(finding.pii_type, 0) + 1
            placeholder = (f"[PII-{PIIScanner._LABEL[finding.pii_type]}-"
                           f"{counters[finding.pii_type]:03d}]")
            parts.extend((text[cursor:finding.start], placeholder))
            mask_map[placeholder] = text[finding.start:finding.end]
            cursor = finding.end
        parts.append(text[cursor:])
        return ScanResult(findings=ordered, masked_text="".join(parts),
                          mask_map=mask_map)

    def scan_and_mask_for_agent(self, agent_id: str, text: str,
                                *, enabled_types=None):
        result = self.scan_for_agent(agent_id, text)
        findings = result.findings
        if enabled_types is not None:
            enabled = set(enabled_types)
            findings = [finding for finding in findings
                        if finding.pii_type in enabled]
        return self._mask_findings(text, findings)

    def scan_mask_subset_for_agent(self, agent_id: str, text: str, store):
        from securityd.dlp.policy_store import DLPAction  # noqa: PLC0415
        from securityd.dlp.pii_scanner import ScanResult  # noqa: PLC0415

        full = self.scan_for_agent(agent_id, text)
        keep = [finding for finding in full.findings
                if store.get_action(agent_id, finding.pii_type)
                in (DLPAction.MASK, DLPAction.BLOCK)]
        masked = self._mask_findings(text, keep)
        return ScanResult(findings=full.findings,
                          masked_text=masked.masked_text,
                          mask_map=masked.mask_map)

    # 非 Agent-aware 的第三方调用保持原 PIIScanner 行为。
    def scan(self, text: str):
        return self.local_scanner.scan(text)

    def scan_and_mask(self, text: str):
        return self.local_scanner.scan_and_mask(text)
