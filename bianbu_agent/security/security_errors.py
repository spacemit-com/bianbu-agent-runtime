"""安全拦截的错误分类（供 run 失败时区分"被安全拦下"与"跑挂了"）。

## 为什么需要这个

securityd 拦截一个工具调用时，`SecurityDenied` 带着完整的结构化信息
（tool / capability / policy / reason）冒泡到 `run_manager`。
但那里一律包成 `RunError(code="run_failed", message=str(exc))` —— code 是
泛化的，前端无从判断这是权限问题还是崩溃，于是显示统一文案
「发送失败，请稍后再试」。

用户看到的是"系统故障，重试可能会好"，实际是"这个操作没有授权，
重试一万次也一样"。而且不知道是哪个工具、缺哪条能力、该去哪里授权 ——
运维在配置台上也定位不到。

## 为什么不 import SecurityDenied

它定义在插件目录 `resources/plugins/bianbu.security/lib/errors.py`，
而插件是可卸载的：硬 import 会让主干依赖一个可能不存在的插件。
按属性识别（duck typing）—— 任何带 `capability` + `reason` 且是
`PermissionError` 的异常都按安全拒绝处理，这也让 hermes 侧或将来的
其它安全插件复用同一条错误通道。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "PROVIDER_CONNECTION_FAILED_CODE",
    "PROVIDER_TIMEOUT_CODE",
    "SECURITY_DENIED_CODE",
    "is_security_denial",
    "security_error_payload",
    "classify_run_exception",
    "SkillUnavailableError",
]

# 前端按这个 code 决定「展示真实原因」而非「显示通用重试文案」
SECURITY_DENIED_CODE = "security_denied"

# securityd 不可达导致的 fail-closed 拦截，与明确的权限拒绝区分开：
# 前者重试/修好总线可能就好了，后者要去授权。
SECURITY_DEGRADED_CODE = "security_unavailable"
NETWORK_POLICY_BLOCKED_CODE = "network_policy_blocked"
PROVIDER_CONNECTION_FAILED_CODE = "provider_connection_failed"
# 模型服务收下了请求却没有在超时时间内回包（含 Run 的模型空闲计时）。
# 与 provider_connection_failed 分开：连不上是网络/地址问题，
# 超时可能是服务过载或模型太慢，两者的处置建议不同。
PROVIDER_TIMEOUT_CODE = "provider_timeout"

# 传输层证据只在 provider 客户端模块里成立。工具自己的 TimeoutError /
# ConnectionError 文案可能撞上同样的词，绝不能算到模型服务头上，
# 所以类型判定限定模块名，文案判定只认下面这两句不会出现在别处的话。
_TRANSPORT_MODULES = frozenset({"openai", "httpx", "httpcore", "langchain_openai"})
_TIMEOUT_TYPE_NAMES = frozenset({
    "APITimeoutError", "TimeoutException", "ConnectTimeout", "ReadTimeout",
    "WriteTimeout", "PoolTimeout",
})
_CONNECTION_TYPE_NAMES = frozenset({
    "APIConnectionError", "ConnectError", "ConnectionError", "ProxyError",
    "RemoteProtocolError", "ProtocolError", "TransportError",
})
_TIMEOUT_MESSAGE_MARKERS = ("model response timed out", "request timed out")
_CONNECTION_MESSAGE_MARKERS = (
    "connection error", "connection refused", "connecterror",
    "network is unreachable", "name or service not known",
    "temporary failure in name resolution",
)
_NETWORK_MODES = frozenset({"whitelist", "deny_all", "allow_all", "learning"})


def _provider_host(base_url: object) -> str:
    value = str(base_url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    return str(parsed.hostname or "").lower().rstrip(".")


def _host_allowed(host: str, domains: list[str]) -> bool:
    for raw in domains:
        domain = str(raw).strip().lower().rstrip(".")
        if not domain:
            continue
        if domain.startswith("*."):
            suffix = domain[1:]
            if host.endswith(suffix) and host != suffix[1:]:
                return True
        elif host == domain:
            return True
    return False


def _exception_chain(exc: BaseException):
    """异常及其 cause/context 链，用于跨包装层识别传输层故障。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _transport_failure_kind(exc: BaseException) -> str | None:
    """provider 客户端抛出的传输层故障类型：timeout / connection / None。"""
    for item in _exception_chain(exc):
        cls = type(item)
        if cls.__module__.partition(".")[0] not in _TRANSPORT_MODULES:
            continue
        if cls.__name__ in _TIMEOUT_TYPE_NAMES:
            return "timeout"
        if cls.__name__ in _CONNECTION_TYPE_NAMES:
            return "connection"
    return None


def _timeout_seconds(exc: BaseException) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*s\b", str(exc))
    if match is None:
        return None
    return float(match.group(1))


def _failure_kind(exc: BaseException) -> str | None:
    """模型服务不可达/无响应的故障类型，判断不依赖网络策略是否存在。"""
    by_type = _transport_failure_kind(exc)
    if by_type is not None:
        return by_type
    text = str(exc).strip().lower()
    if any(marker in text for marker in _TIMEOUT_MESSAGE_MARKERS):
        return "timeout"
    if any(marker in text for marker in _CONNECTION_MESSAGE_MARKERS):
        return "connection"
    return None


def _provider_unreachable_payload(
    exc: BaseException,
    *,
    agent_id: str,
    provider_base_url: object,
    network_policy: Mapping[str, object] | None,
) -> dict[str, Any] | None:
    """模型服务连不上/没回包时的错误载荷。

    没有网络策略证据（``BIANBU_SECURITY=0``、Worker 没拿到策略快照）时也要
    给出可执行错误 —— 以前这种情况只剩裸的 ``run_failed`` + 英文原文
    ``Connection error.``，用户既不知道是谁连不上，也不知道该做什么。
    但放宽仅限**传输层证据**：工具自己的 "Connection error"/"timed out"
    文案仍然保持 run_failed，不能把工具的故障算成 Provider 的。
    """
    kind = _failure_kind(exc)
    if kind is None:
        return None
    policy = network_policy if isinstance(network_policy, Mapping) else None
    if policy is None:
        by_type = _transport_failure_kind(exc)
        if by_type is None and kind != "timeout":
            return None
        mode = ""
        domains: list[str] = []
    else:
        mode = str(policy.get("mode") or "").strip().lower()
        domains = [str(value).strip().lower().rstrip(".") for value in
                   list(policy.get("domains") or []) if str(value).strip()]
        if mode not in _NETWORK_MODES:
            return None

    host = _provider_host(provider_base_url)
    base_details = {
        "agent_id": str(agent_id),
        "provider_host": host,
        "network_mode": mode,
        "domains": domains,
    }
    blocked = mode == "deny_all" or (mode == "whitelist" and not _host_allowed(host, domains))
    if blocked:
        action = "add_provider_host_to_whitelist_and_restart_agent"
        reason = (
            f"当前 Agent 只允许访问指定网站，但模型服务 {host} 不在白名单中。"
            "下一步：打开“安全控制中心 → 配置中心 → 网络策略”，将该域名加入白名单；"
            "保存后重启此 Agent 再重试。"
            if mode == "whitelist" else
            f"当前 Agent 已禁止所有外网访问，无法连接模型服务 {host}。"
            "下一步：打开“安全控制中心 → 配置中心 → 网络策略”，改为允许访问或白名单模式；"
            "保存后重启此 Agent 再重试。"
        )
        return {
            "code": NETWORK_POLICY_BLOCKED_CODE,
            "message": f"无法连接到模型服务：{reason}",
            # 策略本身拦下了这次连接：不改配置直接重试，结果只会一样。
            "retryable": False,
            "details": {**base_details, "action": action},
        }
    if kind == "timeout":
        seconds = _timeout_seconds(exc)
        waited = (
            f"在 {seconds:g} 秒内没有返回任何内容" if seconds is not None
            else "在超时时间内没有返回任何内容"
        )
        details = {**base_details, "action": "check_network_provider_and_retry"}
        if seconds is not None:
            details["timeout_seconds"] = seconds
        target = f"模型服务 {host}" if host else "模型服务"
        return {
            "code": PROVIDER_TIMEOUT_CODE,
            "message": (
                f"无法连接到{target}：{waited}。"
                "下一步：检查网络与 Provider 地址、服务状态后重试；"
                "若该模型一贯较慢，请更换更快的模型或 Provider。"
            ),
            "retryable": True,
            "details": details,
        }
    if not host:
        reason = "模型服务暂时无法连接"
    elif mode:
        reason = f"当前网络策略已允许访问 {host}，但模型服务暂时无法连接"
    else:
        reason = f"无法访问 {host}，模型服务暂时无法连接"
    return {
        "code": PROVIDER_CONNECTION_FAILED_CODE,
        "message": (
            f"无法连接到模型服务：{reason}。"
            "下一步：检查网络、Provider 地址和服务状态，然后重试。"
        ),
        "retryable": True,
        "details": {**base_details, "action": "check_network_provider_and_retry"},
    }


class SkillUnavailableError(ValueError):
    """One or more explicitly selected Skills left the leased Generation."""

    def __init__(self, skill_ids: tuple[str, ...]) -> None:
        self.skill_ids = tuple(skill_ids)
        super().__init__(
            "Selected Skills are unavailable: " + ", ".join(self.skill_ids)
        )


def is_security_denial(exc: BaseException) -> bool:
    """这个异常是不是安全拦截。

    按属性识别而非 isinstance —— 见模块 docstring。要求同时具备
    capability 与 reason，避免把普通 PermissionError（如文件权限不足）
    误判成安全拦截。
    """
    from bianbu_agent.security.security_dlp import DLPDenied
    if isinstance(exc, DLPDenied):
        return True
    if not isinstance(exc, PermissionError):
        return False
    return all(hasattr(exc, a) for a in ("capability", "reason"))


def security_error_payload(exc: BaseException) -> dict[str, Any]:
    """把安全拦截摊成给前端的结构。

    `message` 用异常自带的文案（已带 `[bianbu-security]` 前缀），
    details 给出定位与处置所需的字段 —— 前端据此能提示
    「工具 X 需要能力 Y，请在配置台授权」，而不是让用户猜。
    """
    details: dict[str, Any] = {}
    to_meta = getattr(exc, "to_metadata", None)
    if callable(to_meta):
        try:
            details = dict(to_meta())
        except Exception:  # noqa: BLE001 — 取不到细节不影响主消息
            details = {}
    if not details:
        for attr in ("tool", "capability", "policy",
                     "decision", "degraded"):
            if hasattr(exc, attr):
                details[attr] = getattr(exc, attr)
    degraded = bool(details.get("degraded"))
    return {
        "code": SECURITY_DEGRADED_CODE if degraded else SECURITY_DENIED_CODE,
        "message": str(exc).strip() or (
            "安全服务不可达，已按失败关闭策略拦截本次操作"
            if degraded else "安全策略拒绝了本次操作"
        ),
        # 明确的拒绝要先去授权/改配置；securityd 不可达则是服务恢复后
        # 重试可能成功。两种情况的处置方式不同，不能只靠 code 让前端猜。
        "retryable": degraded,
        "details": details,
    }


def classify_run_exception(
    exc: BaseException,
    *,
    agent_id: str = "",
    provider_base_url: object = "",
    network_policy: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """run 失败时的错误载荷。非安全类异常保持原来的 run_failed 语义。"""
    if isinstance(exc, SkillUnavailableError):
        return {
            "code": "skill_unavailable",
            "message": str(exc),
            # 会话选中的 Skill 已离开当前 Generation，原样重试不会变好。
            "retryable": False,
            "details": {"skill_ids": list(exc.skill_ids)},
        }
    if is_security_denial(exc):
        return security_error_payload(exc)
    network_error = _provider_unreachable_payload(
        exc,
        agent_id=agent_id,
        provider_base_url=provider_base_url,
        network_policy=network_policy,
    )
    if network_error is not None:
        return network_error
    # 事件日志写不进去和「跑挂了」是两种故障：前者重试无用，必须先让用户
    # 知道已有多少内容真的存住了、以及为什么存不住。按属性识别，见模块
    # 头部的 duck typing 说明。
    durable_events = getattr(exc, "session_log_durable_events", None)
    if isinstance(durable_events, int):
        cause = str(getattr(exc, "session_log_cause", "") or "未知")
        return {
            "code": "session_log_write_failed",
            "message": (
                f"无法保存新的对话内容：会话事件日志写入失败，已持久保存 "
                f"{durable_events} 条事件，后续内容尚未确认保存。请检查磁盘空间与写入"
                f"权限；恢复后会重试保存，请勿重复执行原任务。底层原因：{cause}"
            ),
            "details": {
                "session_id": str(getattr(exc, "session_id", "")),
                "durable_events": durable_events,
                "cause": cause,
            },
        }
    # 没有专门分类的异常保持可重试语义，但必须留下可读原因：``TimeoutError()``
    # 这类异常的 ``str()`` 是空串，客户端会只剩一个看不懂的 ``run_failed``。
    return {
        "code": "run_failed",
        "message": str(exc).strip() or type(exc).__name__,
        "retryable": True,
        "details": {},
    }
