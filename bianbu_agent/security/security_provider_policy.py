"""Provider base_url 白名单（D-8 / BN-SEC-CAPMAP-001 §6.6）。

## 风险

Provider 的 `base_url` 决定**全部对话内容发往哪台服务器**。此前可注册任意
URL 且无任何校验 —— 一次 API 调用（或一次 Web 控制台操作）就能把所有对话
导向攻击者的服务器，而且不会有任何提示：模型照样答，用户看不出区别。

这比单个工具越权严重：工具越权影响一次操作，provider 改向影响全部会话，
且泄露的是对话原文（往往含 DLP 想拦的东西）。

## 为什么放在 Runtime 侧而不是 securityd 能力

§6.6 给了两个方案：securityd 加一条 `model.provider` 能力（scope=host），
或 Runtime 侧配置项列白名单。选后者：

  · provider 注册是**低频配置动作**，不是每 run 都要判的运行期决策，
    走 D-Bus 判权收益不大
  · 白名单要在 store 的写事务内校验才挡得住所有入口（HTTP API / CLI /
    直接调 store），而事务内做 D-Bus 同步调用会把锁持有时间与总线可用性
    绑在一起 —— securityd 一慢，配置就写不进去
  · 判权返回 CONFIRM 时无人可问：注册 provider 可能发生在无人值守的
    CLI/脚本里，弹不出审批

## 默认放行

`BIANBU_ALLOWED_PROVIDER_HOSTS` 未配置时不限制 —— 本地部署常连内网
推理服务或 localhost，预置一份公有云白名单只会让人装不上自己的服务。
但一旦配置，就是硬门禁（拒绝，不是告警）：配了白名单还放行等于没配。

    BIANBU_ALLOWED_PROVIDER_HOSTS="api.openai.com,*.mycorp.internal,localhost"

  · 精确 host 匹配（不含端口，端口不影响"数据发给谁"）
  · `*.example.com` 匹配子域，**不匹配** example.com 本身（要则显式加上）
  · 大小写不敏感（host 本就不区分大小写）
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

__all__ = [
    "ProviderHostRejected",
    "allowed_provider_hosts",
    "provider_host_allowed",
    "enforce_provider_host",
]

_ENV = "BIANBU_ALLOWED_PROVIDER_HOSTS"


class ProviderHostRejected(PermissionError):
    """base_url 的 host 不在白名单内。

    继承 PermissionError：API 层与 CLI 已按它分流权限类错误。
    """

    def __init__(self, host: str, base_url: str, allowed: tuple[str, ...]):
        self.host = host
        self.base_url = base_url
        self.allowed = allowed
        super().__init__(
            f"[bianbu-security] 拒绝注册 Provider：base_url 的 host {host!r} "
            f"不在允许列表内（当前允许 {', '.join(allowed) or '（空）'}）。"
            f"如确需使用，请把它加入环境变量 {_ENV}。")


def allowed_provider_hosts() -> tuple[str, ...]:
    """白名单。空元组表示未配置（不限制）。"""
    raw = os.environ.get(_ENV) or ""
    return tuple(h.strip().lower() for h in raw.replace(";", ",").split(",")
                 if h.strip())


def _host_of(base_url: str) -> str:
    """取 base_url 的 host（小写、不含端口与用户信息）。

    无 scheme 时补 https:// 再解析：`api.openai.com/v1` 这种写法很常见，
    直接 urlsplit 会把整串当 path，host 解析成空 —— 那会让校验形同放过。
    """
    u = (base_url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        return (urlsplit(u).hostname or "").lower()
    except ValueError:
        return ""


def provider_host_allowed(base_url: str) -> tuple[bool, str]:
    """返回 (是否允许, 解析出的 host)。未配置白名单时一律允许。"""
    allowed = allowed_provider_hosts()
    host = _host_of(base_url)
    if not allowed:
        return True, host
    if not host:
        # 配了白名单却解析不出 host —— 不能放过，否则畸形 URL 就是绕过手段
        return False, host
    for pattern in allowed:
        if pattern == host:
            return True, host
        if pattern.startswith("*."):
            # *.example.com 匹配子域，不匹配 example.com 本身
            if host.endswith(pattern[1:]):
                return True, host
    return False, host


def enforce_provider_host(base_url: str) -> None:
    """不在白名单内就抛 ProviderHostRejected。空 base_url 不校验。

    空值放过是因为它不构成"数据发往外部"：provider 记录可以先建后填
    （store 允许 base_url=""），真正发请求时 default_engine 只在
    `model.base_url` 非空时才设置它。
    """
    if not (base_url or "").strip():
        return
    ok, host = provider_host_allowed(base_url)
    if not ok:
        raise ProviderHostRejected(host, base_url, allowed_provider_hosts())
