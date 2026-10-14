"""远端 MCP 数据出境识别（D-9 / BN-SEC-CAPMAP-001 §3.1）。

## 缺口

MCP 有两种传输，风险面完全不同：

    stdio            本地子进程 —— 第三方代码在本机跑，可用 bwrap 隔离 ✅ 已覆盖
    streamable-http  远端端点   —— 无本地进程，但**参数与返回值经网络出境**

`mcp/client.py::_open_session` 的 HTTP 分支在 stdio 分支的 `return` 之后，
结构上不可能被沙箱覆盖 —— 沙箱对它也确实无意义（没有本地进程要关）。

但判权与 DLP 此前**不区分二者**：`dlp_pre_tool` 对所有工具用同一套策略，
而远端 MCP 的工具参数是发给第三方服务器的。策略里 `action=allow` 的 PII
类型（如运维认为无所谓的姓名）在本地工具上确实无所谓，发到外部 server
就是另一回事了 —— 数据一旦出境不可撤回。

## 做法

不追加沙箱（决策 9），而是让判权链知道「这次调用会把参数发到外部」：
本模块识别工具属于哪台 MCP server、该 server 是不是远端传输，
`before_tool` hook 把结论带进 securityd 的 payload，DLP handler 据此
对远端出境提一档处置（audit_only/allow → confirm）。

## 为什么读 mcp.json 而不是问运行时

判权发生在 hook 里，拿不到 MCPPlugin 实例（那是 runtime 内部对象）。
mcp.json 是同一份事实来源、就在磁盘上、且 transport 字段是必填 ——
读它比穿透 runtime 拿引用更简单也更稳。结果按 mtime 缓存，避免每次
工具调用都读盘。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("bianbu.security.mcp_transport")

__all__ = ["transport_of", "is_remote_mcp", "remote_endpoint_of", "clear_cache"]

# mcp_id → (transport, url, mtime)
_CACHE: dict[str, tuple[str, str, float]] = {}


def clear_cache() -> None:
    """清缓存。装/改 MCP 后应调用，否则旧 transport 会继续生效。"""
    _CACHE.clear()


def _mcp_roots() -> list[Path]:
    """MCP 安装目录。与 bianbu_agent.config 的 `data_dir/mcps` 约定一致。

    插件跑在 Runtime 进程里但不能 import bianbu_agent.config（会拉进完整包
    依赖，而 hook 的 bootstrap 只保证 lib 可导入），所以按同一约定找路径。
    """
    roots: list[Path] = []
    data_dir = os.environ.get("BIANBU_AGENT_DATA_DIR")
    if data_dir:
        roots.append(Path(data_dir) / "mcps")
    roots.append(Path.cwd() / ".bianbu-agent" / "mcps")
    # 插件目录下的 mcps/（plugin-owned MCP，见 installer 的五类目录白名单）
    here = Path(__file__).resolve().parents[1]        # 插件根
    roots.append(here.parent.parent / "mcps")
    return [r for r in roots if r.is_dir()]


def _read_config(mcp_id: str) -> Optional[tuple[str, str]]:
    """读某台 server 的 (transport, url)。找不到返回 None。"""
    for root in _mcp_roots():
        cfg = root / mcp_id / "mcp.json"
        if not cfg.is_file():
            continue
        try:
            mtime = cfg.stat().st_mtime
        except OSError:
            continue
        cached = _CACHE.get(mcp_id)
        if cached is not None and cached[2] == mtime:
            return cached[0], cached[1]
        try:
            doc = json.loads(cfg.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as e:
            logger.warning("mcp.json 读取失败 %s：%s", cfg, e)
            continue
        if not isinstance(doc, dict):
            continue
        transport = str(doc.get("transport") or "")
        url = str(doc.get("url") or "")
        _CACHE[mcp_id] = (transport, url, mtime)
        return transport, url
    return None


def transport_of(mcp_id: str) -> str:
    """server 的传输类型：stdio | streamable-http | ""（未知）。"""
    got = _read_config(mcp_id)
    return got[0] if got else ""


def is_remote_mcp(mcp_id: str) -> Optional[bool]:
    """该 server 是否把数据发往远端。

    返回 None 表示**判不出来**（配置读不到）—— 调用方应按远端处理：
    读不到配置就当本地，等于给"藏起配置"开了绕过口子。
    """
    t = transport_of(mcp_id)
    if not t:
        return None
    return t != "stdio"


def remote_endpoint_of(mcp_id: str) -> str:
    """远端 server 的 URL host，供审计与审批弹窗展示「发给谁」。

    审批时只说"参数含敏感信息"用户无法判断，说清"要发到 api.foo.com"
    才能做决定。
    """
    got = _read_config(mcp_id)
    if not got or got[0] == "stdio" or not got[1]:
        return ""
    from urllib.parse import urlsplit

    try:
        return (urlsplit(got[1]).hostname or "").lower()
    except ValueError:
        return ""


def egress_info(tool_name: str, tool_metadata: dict | None = None) -> Optional[dict]:
    """给 MCP 工具名（`<mcp_id>/<local_name>`）判断出境属性。

    非 MCP 工具返回 None。MCP 工具返回
    {"mcp_id", "transport", "remote": bool, "endpoint": str}。
    """
    metadata = tool_metadata or {}
    origin = str(metadata.get("origin") or "")
    metadata_mcp_id = str(metadata.get("mcp_id") or "")
    if origin and origin != "mcp" and not metadata_mcp_id:
        return None
    if "/" not in (tool_name or "") and not metadata_mcp_id:
        return None
    # 有元数据时以 mcp_id 为准；无元数据时保留限定名兼容兜底。
    mcp_id = metadata_mcp_id or tool_name.partition("/")[0]
    remote = is_remote_mcp(mcp_id)
    if remote is None:
        # 判不出来按远端处理（fail-safe），并留日志让运维能查
        logger.warning(
            "MCP server %s 的 mcp.json 未找到，按「远端出境」从严处理；"
            "如为本地 stdio server 请确认安装完整", mcp_id)
        return {"mcp_id": mcp_id, "transport": "", "remote": True,
                "endpoint": "", "transport_unknown": True}
    return {"mcp_id": mcp_id, "transport": transport_of(mcp_id),
            "remote": remote, "endpoint": remote_endpoint_of(mcp_id)}
