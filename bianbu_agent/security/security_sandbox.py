"""沙箱模式支持（INT-RT-14~18）。

三种模式，由 `agent.yaml` 的 `metadata.sandbox.mode` 选择：

    off     不沙箱化（默认）
    agent   整个 FastAPI 服务进程在 bwrap 内（由 run.sh 包裹，见 §5.2）
            MCP 子进程自动继承限制
    mcp     只沙箱化 stdio MCP 子进程 —— 第三方代码，风险最高
    tool    单工具容器，Agent 指定哪些工具进沙箱

`agent` 与 `mcp` 互斥：agent 模式下整进程已在沙箱内，MCP 子进程自然继承，
再包一层会嵌套 bwrap（bwrap 不支持嵌套 unshare-user）。

配置放 `AgentSpec.metadata`（已证实经 agents/file_store.py 双向持久化），
不改 AgentSpec schema。
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("bianbu_agent.security.sandbox")

__all__ = [
    "SandboxMode",
    "SandboxConfig",
    "parse_sandbox_config",
    "wrap_mcp_command",
    "sandbox_available",
    "SandboxConfigError",
]

VALID_MODES = ("off", "agent", "mcp", "tool")


class SandboxConfigError(ValueError):
    """沙箱配置非法。启动期抛出，不静默降级 —— 配了沙箱却没生效比没配更危险。"""


class SandboxMode:
    OFF = "off"
    AGENT = "agent"
    MCP = "mcp"
    TOOL = "tool"


@dataclass(frozen=True)
class SandboxConfig:
    mode: str = SandboxMode.OFF
    net: bool = True
    rw_dirs: tuple[str, ...] = ()
    mcp_servers: tuple[str, ...] = ()          # mode=mcp 限定范围，空=全部
    tools: tuple[str, ...] = ()                # mode=tool 限定范围，空=全部
    limits: Mapping[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return self.mode != SandboxMode.OFF

    def covers_mcp(self, mcp_id: str) -> bool:
        if self.mode != SandboxMode.MCP:
            return False
        return not self.mcp_servers or mcp_id in self.mcp_servers

    def covers_tool(self, tool_name: str) -> bool:
        if self.mode != SandboxMode.TOOL:
            return False
        return not self.tools or tool_name in self.tools


def parse_sandbox_config(metadata: Mapping[str, Any] | None) -> SandboxConfig:
    """从 AgentSpec.metadata 解析沙箱配置。

    配置错误直接抛 SandboxConfigError —— 静默降级会让用户以为隔离生效了。
    """
    if not isinstance(metadata, Mapping):
        return SandboxConfig()
    raw = metadata.get("sandbox")
    if raw is None:
        return SandboxConfig()
    if not isinstance(raw, Mapping):
        raise SandboxConfigError("metadata.sandbox 必须是映射")

    mode = str(raw.get("mode", SandboxMode.OFF)).strip().lower()
    if mode not in VALID_MODES:
        raise SandboxConfigError(
            f"metadata.sandbox.mode 非法：{mode!r}（可选 {', '.join(VALID_MODES)}）")

    def _tuple(key: str) -> tuple[str, ...]:
        val = raw.get(key) or ()
        if isinstance(val, str):
            raise SandboxConfigError(f"metadata.sandbox.{key} 必须是数组，不是字符串")
        return tuple(str(x) for x in val if str(x).strip())

    limits = raw.get("limits") or {}
    if not isinstance(limits, Mapping):
        raise SandboxConfigError("metadata.sandbox.limits 必须是映射")

    cfg = SandboxConfig(
        mode=mode,
        net=bool(raw.get("net", True)),
        rw_dirs=_tuple("rw_dirs"),
        mcp_servers=_tuple("mcp_servers"),
        tools=_tuple("tools"),
        limits={str(k): str(v) for k, v in limits.items()},
    )

    # mode=agent 下 MCP 子进程自动继承，配 mcp_servers 是无效配置 → 明确报错
    if cfg.mode == SandboxMode.AGENT and cfg.mcp_servers:
        raise SandboxConfigError(
            "mode=agent 时 MCP 子进程自动继承沙箱限制，"
            "不应再配 mcp_servers（会导致嵌套 bwrap）")
    if cfg.mode == SandboxMode.MCP and cfg.tools:
        raise SandboxConfigError("mode=mcp 时不应配 tools（该字段仅 mode=tool 有效）")
    if cfg.mode == SandboxMode.TOOL and cfg.mcp_servers:
        raise SandboxConfigError("mode=tool 时不应配 mcp_servers")
    return cfg


def sandbox_available() -> tuple[bool, str]:
    """bwrap 是否可用。返回 (可用, 原因)。"""
    path = shutil.which("bwrap")
    if not path:
        return False, "bwrap 未安装（apt install bubblewrap）"
    return True, path


def _dns_binds() -> list[str]:
    """net=true 时把 DNS 配置真正带进沙箱（只在启用网络时调用）。

    `--ro-bind /etc /etc` 看似够了，实际不够：systemd-resolved 的系统上
    `/etc/resolv.conf` 是指向 `../run/systemd/resolve/stub-resolv.conf` 的
    **符号链接**，bind /etc 只带过去链接本身，而 /run/systemd/resolve 没绑，
    链接就悬空 —— 沙箱内 `/etc/resolv.conf` 不存在，getaddrinfo 全部
    EAI_AGAIN。worker 实测：net=on 的沙箱里 DNS 完全不通，凡是要联网的
    MCP server（npx 拉包、web fetch 类工具）都会失败。

    修法是把符号链接的**目标路径按原路径**绑进沙箱，让 /etc 里那个链接能正常
    解析。不能直接绑到 /etc/resolv.conf —— /etc 是只读绑定，且里面的链接悬空，
    bwrap 会报 `Can't create file at /etc/resolv.conf: No such file or directory`
    （实测踩过）。不改宿主任何东西。
    """
    binds: list[str] = []
    resolv = Path("/etc/resolv.conf")
    try:
        target = resolv.resolve()
    except OSError:
        return binds
    if not target.is_file():
        logger.warning("沙箱 net=true 但 /etc/resolv.conf 不可解析，沙箱内 DNS 可能不可用")
        return binds
    # target == resolv 时它是普通文件，/etc 的 bind 已覆盖，无需重复
    if target != resolv:
        binds += ["--ro-bind", str(target), str(target)]
    return binds


def _merged_usr_binds() -> list[str]:
    """merged-usr 布局适配。

    worker（K3 riscv64）上 /bin /lib /sbin 都是指向 usr/* 的符号链接。
    直接 --ro-bind 会把符号链接当目录挂，导致 execvp 找不到程序
    （securityd 的 sandbox.py 踩过这个坑）。符号链接要用 --symlink 重建。
    """
    args: list[str] = []
    for top in ("bin", "sbin", "lib", "lib32", "lib64", "libx32"):
        p = Path("/") / top
        if not p.exists():
            continue
        if p.is_symlink():
            args += ["--symlink", os.readlink(p), f"/{top}"]
        else:
            args += ["--ro-bind", str(p), f"/{top}"]
    return args


def build_cgroup_wrapper(cfg: SandboxConfig) -> list[str]:
    """cgroup 限额前缀（INT-RT-18）。

    用 systemd-run --user 的 transient unit 施加限额 —— 非 root 也能用
    （直接写 /sys/fs/cgroup 需要 root 或已委派的 cgroup）。
    systemd-run 不可用时返回空前缀并记 warning：**限额降级但沙箱隔离仍生效**，
    这与"沙箱起不来就拒绝启动"是不同性质的问题，故不抛异常。

    限额键：cpu（→CPUQuota）、memory（→MemoryMax）、pids（→TasksMax）。
    """
    if not cfg.limits:
        return []
    if not shutil.which("systemd-run"):
        logger.warning(
            "systemd-run 不可用，cgroup 限额降级（沙箱隔离仍生效）：%s", cfg.limits)
        return []

    prefix = ["systemd-run", "--user", "--scope", "--quiet"]
    mapping = {"cpu": "CPUQuota", "memory": "MemoryMax", "pids": "TasksMax"}
    applied = []
    for key, prop in mapping.items():
        val = cfg.limits.get(key)
        if val:
            prefix += ["-p", f"{prop}={val}"]
            applied.append(f"{prop}={val}")
    if not applied:
        return []
    logger.info("cgroup 限额：%s", " ".join(applied))
    return prefix


def build_bwrap_argv(
    command: str,
    args: list[str],
    cfg: SandboxConfig,
    *,
    extra_ro: tuple[str, ...] = (),
    with_limits: bool = True,
) -> tuple[str, list[str]]:
    """构造 bwrap 命令行。返回 (可执行路径, 完整 args)。

    with_limits=True 且配了 limits 时，外层套 systemd-run 施加 cgroup 限额，
    此时返回的可执行路径是 systemd-run 而非 bwrap。
    """
    ok, detail = sandbox_available()
    if not ok:
        raise SandboxConfigError(f"沙箱不可用：{detail}")

    argv = [
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
        "--cap-drop", "ALL",
        "--die-with-parent",          # 父进程退出时一并清理，避免子进程逃逸存活
        "--ro-bind", "/usr", "/usr",
        "--ro-bind", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    # /sys 只读挂载。psutil、uvicorn 等会读它（CPU 数、网卡、cgroup 信息），
    # 不挂会让它们拿到空值或报错。
    #
    # 但 /sys 是 bind 自宿主的，即使在新 netns 里 /sys/class/net 仍会列出
    # 宿主网卡名 —— 这是信息泄露（网卡名可见），不是访问权限（实测出网确被
    # --unshare-net 阻断）。net=false 表示调用方要网络隔离，此时连网卡名也
    # 不该暴露，故不绑 /sys。
    if cfg.net and Path("/sys").exists():
        argv += ["--ro-bind", "/sys", "/sys"]
    argv += _merged_usr_binds()
    if cfg.net:
        argv += _dns_binds()

    for d in extra_ro:
        p = Path(d).expanduser()
        if p.exists():
            argv += ["--ro-bind", str(p), str(p)]

    for d in cfg.rw_dirs:
        p = Path(d).expanduser()
        if p.exists():
            argv += ["--bind", str(p), str(p)]
        else:
            logger.warning("沙箱 rw_dir 不存在，跳过：%s", p)

    if not cfg.net:
        argv.append("--unshare-net")

    argv += ["--", command, *args]

    if with_limits:
        cgroup = build_cgroup_wrapper(cfg)
        if cgroup:
            # systemd-run <props> bwrap <bwrap args> -- cmd
            return cgroup[0], [*cgroup[1:], detail, *argv]
    return detail, argv


def wrap_mcp_command(
    command: str,
    args: list[str],
    cfg: SandboxConfig,
    mcp_id: str,
) -> tuple[str, list[str]]:
    """mode=mcp：把 MCP stdio 子进程包进 bwrap（INT-RT-16）。

    不覆盖范围内的 server 原样返回。

    已知边界：`npx -y @modelcontextprotocol/server-...` 首次运行要联网拉包，
    若配了 net=false 会失败。这类 server 需预装或单独放开网络。
    """
    if not cfg.covers_mcp(mcp_id):
        return command, args
    try:
        bwrap, argv = build_bwrap_argv(
            command, args, cfg,
            # MCP 子进程通常是 node/python，需要用户级包目录可读
            extra_ro=(str(Path.home() / ".npm"), str(Path.home() / ".cache")))
    except SandboxConfigError as e:
        # 配了沙箱但起不来 → 拒绝启动该 server，不静默裸跑
        raise SandboxConfigError(
            f"MCP server {mcp_id} 配置了沙箱但无法启动：{e}") from e
    logger.info("MCP server %s 在 bwrap 沙箱内启动（net=%s）", mcp_id, cfg.net)
    return bwrap, argv
