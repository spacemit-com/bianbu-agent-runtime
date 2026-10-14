"""安装期代码签名门禁（D-6 / BN-SEC-CAPMAP-001 §6.5）。

Hook 是可执行代码、权力大于 tool（挂 8 个干预点、能读改整个 run 的上下文），
但此前没有任何来源校验 —— 装上即全权。

为什么是安装期而不是运行期判权：securityd 的判权入口本身就是 hook
（`security.before_tool`），用 hook 判 hook 会循环依赖；而且每 run × 8 个点
都判，开销也不划算。安装期校验没有这两个问题，且能复用 securityd 已有的
ed25519 验签实现（`securityd.code_signing`）。

## 缺 securityd 时怎么办

`fail-open`，只记警告。理由与 `lib/bridge.py` 的降级分档一致但更宽松：
这里挡的是"安装"这个低频动作，且策略默认 warn 本来就放行未签名包 ——
securityd 不可达时硬拒会让离线开发装不了任何 hook，而收益仅限于
"顺便挡住恰好在此刻被篡改的包"。

想要 securityd 不可达即拒绝，设 BIANBU_SECURITY_CODESIGN_REQUIRE_DAEMON=1。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("bianbu_agent.security.codesign")

__all__ = ["CodeSignatureRejected", "verify_code_package", "enforce_code_signature"]


class CodeSignatureRejected(PermissionError):
    """代码包签名校验未通过，拒绝安装。

    继承 PermissionError 而非自定义基类：installer 的调用方与 CLI 已按
    PermissionError 分流权限类错误，沿用它就不必改上层。
    """

    def __init__(self, kind: str, path: Path | str, reason: str, digest: str = ""):
        self.kind = kind
        self.path = str(path)
        self.reason = reason
        self.digest = digest
        super().__init__(f"[bianbu-security] 拒绝安装 {kind} {path}：{reason}")


def _securityd_client():
    """加载版本化安全客户端；不把 securityd 内部源码加入 Runtime 导入路径。"""
    try:
        from bianbu_agent.security.security_runtime import security_client_module
        return security_client_module()
    except Exception:
        return None


def verify_code_package(path: Path | str, kind: str = "hook") -> dict:
    """问 securityd 这个代码包能否安装。

    返回 {allowed, signed, valid, digest, policy, reason, key_id}。
    securityd 不可达时返回 allowed=True 并带 degraded=True（除非要求必须可达）。
    """
    if os.environ.get("BIANBU_SECURITY", "1") == "0":
        return {"allowed": True, "degraded": True, "reason": "BIANBU_SECURITY=0"}

    require = os.environ.get("BIANBU_SECURITY_CODESIGN_REQUIRE_DAEMON") == "1"
    sc = _securityd_client()
    if sc is None:
        msg = "securityd 客户端不可用（未找到 bianbu_security_client）"
        if require:
            return {"allowed": False, "degraded": True, "reason": msg}
        logger.warning("代码签名校验跳过：%s", msg)
        return {"allowed": True, "degraded": True, "reason": msg}

    try:
        return sc.verify_code_package(str(path), kind)
    except Exception as e:  # noqa: BLE001 — 总线/接口异常走降级
        msg = f"securityd 不可达：{e}"
        if require:
            return {"allowed": False, "degraded": True, "reason": msg}
        logger.warning("代码签名校验跳过（%s）", msg)
        return {"allowed": True, "degraded": True, "reason": msg}


def enforce_code_signature(path: Path | str, kind: str = "hook") -> dict:
    """校验并在不通过时抛 CodeSignatureRejected。返回结论供调用方记录。"""
    verdict = verify_code_package(path, kind)
    if not verdict.get("allowed", True):
        raise CodeSignatureRejected(
            kind, path,
            verdict.get("reason") or "签名校验未通过",
            str(verdict.get("digest") or ""))
    return verdict
