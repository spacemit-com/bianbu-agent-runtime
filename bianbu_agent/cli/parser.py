"""Command-line argument declarations."""

from __future__ import annotations

import argparse
from pathlib import Path


class _VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        from importlib.metadata import version, PackageNotFoundError
        try:
            release = version("bianbu-agent")
        except PackageNotFoundError:
            release = "0.1.0"
        print(f"bianbu-agent {release}")
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bianbu-agent",
        description="本地资源文件管理；资源命令不启动 Runtime 或 Worker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""资源命令直接读写 data_dir 中的文件，输出 JSON。
示例：
  bianbu-agent --config /路径/config.yaml agent list
  bianbu-agent skill install /路径/skill包
  bianbu-agent mcp add filesystem --command npx --args -y @modelcontextprotocol/server-filesystem /tmp
  bianbu-agent plugin effective
Tool 由 Runtime 内置代码提供，不再支持安装；Hook 随 Plugin 安装；扩展能力请使用 mcp。
详细帮助：bianbu-agent <资源> <子命令> --help
serve / worker 是 Runtime 服务启动入口。
使用指南：docs/cli-guide.md（deb：/usr/share/doc/bianbu-agent-runtime/cli-guide.md）""",
    )
    parser.add_argument("--version", action=_VersionAction, nargs=0, help="显示版本并退出")
    parser.add_argument("--debug", action="store_true", help="操作失败时显示完整异常堆栈")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="配置文件（默认：当前目录 runtime.yaml、BIANBU_AGENT_CONFIG、~/.bianbu-agent/config.yaml）",
    )
    parser.add_argument(
        "--role", choices=("control", "agent-worker"), default=None,
        help="内部运行角色：control 控制面，agent-worker 专家执行进程",
    )
    resources = parser.add_subparsers(dest="resource")

    for resource in ("skill", "plugin"):
        selected = resources.add_parser(resource, help={
            "skill": "管理技能包",
            "plugin": "管理插件及其能力",
        }[resource])
        actions = selected.add_subparsers(dest="action", required=True)
        actions.add_parser("list", help="列出已安装资源")
        install = actions.add_parser("install", help="从本地目录安装资源")
        install.add_argument("source", type=Path)
        if resource == "plugin":
            install.add_argument("--upgrade", action="store_true")
        for action in ("show", "verify", "remove"):
            command = actions.add_parser(action, help={"show": "查看包文件信息", "verify": "校验包文件", "remove": "移除包"}[action])
            command.add_argument("package_id")
        if resource == "plugin":
            members = actions.add_parser("members", help="列出插件成员")
            members.add_argument("package_id", nargs="?", default=None)
            actions.add_parser("effective", help="显示合并后的插件配置")
            rollback = actions.add_parser("rollback", help="恢复插件的上一版本")
            rollback.add_argument("package_id")

    mcp = resources.add_parser(
        "mcp",
        help="manage external MCP servers",
        description="Manage external MCP servers for Bianbu Agent",
    )
    mcp_actions = mcp.add_subparsers(dest="action", required=True)
    mcp_actions.add_parser("list", help="list installed MCP servers")
    mcp_get = mcp_actions.add_parser("get", help="show an installed MCP server")
    mcp_get.add_argument("name")
    mcp_add = mcp_actions.add_parser(
        "add", help="install a local or remote MCP server",
    )
    mcp_add.add_argument("name")
    source = mcp_add.add_mutually_exclusive_group(required=True)
    source.add_argument("--command", help="stdio executable")
    source.add_argument("--url", help="remote HTTP/SSE endpoint")
    mcp_add.add_argument("--args", nargs=argparse.REMAINDER, default=[])
    mcp_add.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    mcp_add.add_argument("--auth", choices=("none", "header", "oauth"), default="none")
    # --args is a REMAINDER, so these two must be written before it.
    mcp_add.add_argument(
        "--upgrade",
        action="store_true",
        help="replace an MCP server that is already installed under this name",
    )
    mcp_add.add_argument(
        "--no-verify",
        dest="no_verify",
        action="store_true",
        help=(
            "kept for compatibility: add only registers the declaration, and "
            "the Runtime verifies the server in the background"
        ),
    )
    for action, help_text in (("login", "authorize an MCP server"), ("reauth", "reauthorize MCP servers")):
        command = mcp_actions.add_parser(action, help=help_text)
        command.add_argument("name", nargs="?" if action == "login" else "?")
        command.add_argument("--all", action="store_true")
        command.add_argument("--flow", choices=("browser", "device"), default="browser")
    mcp_remove = mcp_actions.add_parser("remove", help="remove an MCP server")
    mcp_remove.add_argument("name")
    # AgentHub installs MCP servers by staging the package directory and calling
    # this verb, so it is APM's install contract rather than a retired user
    # layout. `show`/`verify` stay gone: no caller needs them.
    mcp_install = mcp_actions.add_parser(
        "install",
        help="install an MCP package directory (APM)",
    )
    mcp_install.add_argument("source")

    agent = resources.add_parser("agent", help="创建、查看、校验或移除专家")
    agent_actions = agent.add_subparsers(dest="action", required=True)
    agent_actions.add_parser("list", help="列出已安装资源")
    agent_create = agent_actions.add_parser("create", help="创建资源文件")
    agent_create.add_argument("name")
    agent_create.add_argument("--provider", required=True)
    agent_create.add_argument("--model", required=True)
    agent_create.add_argument("--tool", action="append", default=[])
    agent_create.add_argument("--skill", action="append", default=[])
    agent_create.add_argument("--mcp", action="append", default=[])
    agent_create.add_argument("--plugin", action="append", default=[])
    for action in ("show", "remove"):
        command = agent_actions.add_parser(action, help={"show": "查看专家配置", "remove": "移除专家"}[action])
        command.add_argument("agent_id")
    agent_verify = agent_actions.add_parser("verify", help="校验资源文件结构与内容")
    agent_verify.add_argument("agent_id")

    model = resources.add_parser("model", help="配置模型与 Provider")
    model_actions = model.add_subparsers(dest="action", required=True)
    model_actions.add_parser("list", help="列出已安装资源")
    model_create = model_actions.add_parser("create", help="创建资源文件")
    model_create.add_argument("model_id")
    model_create.add_argument("provider")
    model_create.add_argument("model")
    model_create.add_argument("--base-url", default="")
    model_actions.add_parser("bootstrap-chat", help="交互填写并保存 Provider 配置")
    for action in ("show", "remove"):
        command = model_actions.add_parser(action, help={"show": "查看模型配置（隐藏凭据）", "remove": "移除模型"}[action])
        command.add_argument("model_id")

    session = resources.add_parser("session", help="查看、创建或删除会话")
    session_actions = session.add_subparsers(dest="action", required=True)
    session_actions.add_parser("list", help="列出已安装资源")
    session_create = session_actions.add_parser("create", help="创建资源文件")
    session_create.add_argument("session_id")
    session_create.add_argument("--title", default="")
    session_create.add_argument("--agent", dest="agent_id", required=True, help="会话所属专家 ID")
    for action in ("show", "remove"):
        command = session_actions.add_parser(action, help={"show": "查看会话文件", "remove": "移除会话"}[action])
        command.add_argument("session_id")

    serve = resources.add_parser("serve", help="前台启动 Runtime HTTP 服务")
    serve.add_argument("--role", dest="serve_role", choices=("control", "agent-worker"), default=None)
    worker = resources.add_parser("worker", help="内部 Worker 入口，不供日常手动启动")
    worker.add_argument("--instance-id", default=None)
    worker.add_argument("--socket", dest="socket_path", default=None)
    worker.add_argument("--lease-id", default="")
    worker.add_argument("--effective-mode", choices=("host", "sandbox"), default="host")
    worker.add_argument("--backend", default="host-process")
    worker.add_argument("--sandboxed", action="store_true")
    worker.add_argument("--container-id", default="")
    return parser


__all__ = ["build_parser"]
