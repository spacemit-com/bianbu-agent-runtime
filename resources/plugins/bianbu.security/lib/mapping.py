"""工具 → 策略解析（INT-RT-04）。

能力管理只有一个维度：**工具名**。工具名本身就是 securityd 的策略键，因此
"映射"退化为恒等映射 —— 不再有"工具 → 能力名"的第二套命名（曾经
`system.exec` 与 `system.execute` 同义不同名，导致干净部署上工具被误拦），
也不再从参数里提取 scope。

`resolve_capability` 保留 (tool, scope) 的返回形状供既有调用方使用；scope 恒为
空。未识别的工具返回 None，由调用方按 unmapped_policy() 处置（默认 allow）。

返回 capability 为空字符串表示显式免判权仍受支持：某些纯交互工具（如
`clarify`）不需要策略门。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("bianbu.security.mapping")

__all__ = ["resolve_capability", "unmapped_policy", "unmapped_decision",
           "UNMAPPED_CAPABILITY", "BUILTIN_MAP"]

# 未映射工具的处置策略以 securityd 权威字典为准。
# 默认 allow：与工具目录的默认值一致，未声明的工具也直接放行。需要收紧时
# 用 BIANBU_SECURITY_UNMAPPED=ask|deny，生产模式下该覆盖被忽略。
UNMAPPED_CAPABILITY = "*"

# 兼容旧导入名；新模型不再使用独立的能力映射表。
BUILTIN_MAP: dict[str, tuple[str, str]] = {}

# 纯交互工具显式免判权：这些工具不产生外部副作用，不需要策略门。
_NO_POLICY_TOOLS = frozenset({"clarify"})


def _unmapped_from_securityd():
    try:
        from securityd.capabilities import unmapped_policy  # noqa: PLC0415
        return unmapped_policy
    except ImportError:
        return None


_UNMAPPED_POLICIES = ("allow", "ask", "deny")
_UNMAPPED_DEFAULT = "allow"


def unmapped_policy() -> str:
    """未映射工具的默认策略：allow | ask | deny（默认 allow）。

    与 securityd 的权威实现 `capabilities.unmapped_policy()` 保持一致：生产
    模式下环境变量覆盖被忽略，一律使用目录默认值——`BIANBU_SECURITY_UNMAPPED`
    只是开发期的调试开关，不能成为生产环境放宽新工具判权的后门。
    """
    shared = _unmapped_from_securityd()
    if shared is not None:
        return shared()
    import os

    value = (os.environ.get("BIANBU_SECURITY_UNMAPPED") or "").strip().lower()
    production = (os.environ.get("BIANBU_RUNTIME_MODE") or "").strip().lower() == "production"
    if value == "allow" and production:
        return _UNMAPPED_DEFAULT
    return value if value in _UNMAPPED_POLICIES else _UNMAPPED_DEFAULT


def unmapped_decision() -> int:
    """把策略翻成决策码（0=ALLOW 1=ASK 2=DENY）。"""
    return {"allow": 0, "ask": 1, "deny": 2}[unmapped_policy()]


def resolve_capability(
    tool_name: str,
    args: dict | None = None,
    *,
    client_provider=None,
    tool_metadata: dict | None = None,
) -> Optional[tuple[str, str]]:
    """把工具名解析为 (tool, scope)；scope 恒为空。

    None 表示未识别，交调用方按 unmapped_policy() 处置。空 tool 表示显式免判权。
    """
    if not tool_name:
        return None
    name = str(tool_name)
    metadata = tool_metadata or {}
    mcp_id = str(metadata.get("mcp_id") or "")
    origin = str(metadata.get("origin") or "")
    if origin == "mcp" or mcp_id or ("/" in name and not origin):
        # MCP 的能力边界由 server 身份决定，不能被工具名字面绕过：一台 server
        # 一条 `mcp.<server>` 策略即可管住其下全部工具。Runtime 的层 1 判权
        # 用同一个键，层 2 的 hook 也保持一致，避免两层各说各话。
        owner, _, local_name = name.partition("/")
        server = mcp_id or owner
        if server and local_name:
            return (f"mcp.{server}", local_name)
    local_name = name.rpartition("/")[2] if "/" in name else name
    if local_name in _NO_POLICY_TOOLS:
        return ("", "")
    return (local_name, "")
