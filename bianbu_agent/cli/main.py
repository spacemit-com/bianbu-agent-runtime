"""Configuration-driven command line interface for Bianbu Agent."""

from __future__ import annotations

import asyncio
import argparse

import os
import sys
from pathlib import Path
import uuid
from bianbu_agent.cli.parser import build_parser


async def main_async(
    argv: list[str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    return await _main_with_args(args)


async def _main_with_args(
    args: argparse.Namespace,
) -> int:
    from bianbu_agent.config import load_runtime_config

    args.config = args.config or _default_config_path()
    config = load_runtime_config(args.config)
    if args.resource in {None, "serve"}:
        from bianbu_agent.cli.commands.server import serve
        selected_role = getattr(args, "serve_role", None) or args.role or "control"
        if selected_role == "agent-worker":
            raise ValueError("agent-worker must use the worker subcommand")
        return await serve(config) if selected_role == "control" else await serve(config, role=selected_role)
    if args.resource == "worker":
        from bianbu_agent.security.runtime_process.worker import run_worker
        from bianbu_agent.security.runtime_process.worker_rpc import default_socket_root, make_instance_socket
        instance_id = args.instance_id or os.environ.get("BIANBU_WORKER_INSTANCE_ID") or f"com.bianbu.agent-{uuid.uuid4().hex}"
        socket_path = args.socket_path or os.environ.get("BIANBU_WORKER_SOCKET")
        if not socket_path:
            socket_path = make_instance_socket(config.runtime.control.worker_socket_root or default_socket_root(config.data_dir), instance_id)
        return await run_worker(
            config, instance_id=instance_id, socket_path=socket_path,
            lease_id=args.lease_id or os.environ.get("BIANBU_WORKER_LEASE_ID", ""),
            effective_mode=args.effective_mode, backend=args.backend,
            sandboxed=args.sandboxed or os.environ.get("BIANBU_SANDBOX_ACTIVE") == "1",
            container_id=args.container_id or os.environ.get("BIANBU_CONTAINER_ID", ""),
        )
    from bianbu_agent.home import AgentHome
    home = AgentHome(config.data_dir).prepare()
    if args.resource == "mcp":
        from bianbu_agent.cli.commands.mcp import mcp_command
        return await mcp_command(home, args)
    if args.resource in {"skill", "plugin"}:
        from bianbu_agent.cli.commands.packages import package_command
        return package_command(home, args, runtime_config=config)
    if args.resource == "agent":
        from bianbu_agent.agents import AgentPackageManager, ManifestAgentStore
        from bianbu_agent.cli.commands.agents import agent_command
        manager = AgentPackageManager(home)
        store = ManifestAgentStore(home, manager)
        # CLI reads current manifests only; Runtime owns legacy migration.
        return await agent_command(store, manager, args)
    if args.resource == "model":
        from bianbu_agent.models import YAMLModelStore
        from bianbu_agent.cli.commands.models import model_command
        store = YAMLModelStore(home.models_dir)
        await store.open()
        try:
            return await model_command(store, args)
        finally:
            await store.close()
    if args.resource == "session":
        from bianbu_agent.sessions import JSONSessionStore
        from bianbu_agent.cli.commands.sessions import session_command
        store = JSONSessionStore(home.sessions_dir)
        await store.open()
        return await session_command(store, args)
    raise ValueError(f"unsupported CLI resource: {args.resource}")


def main() -> None:
    parser = build_parser()
    if not sys.argv[1:]:
        parser.print_help()
        return
    args = parser.parse_args()
    try:
        if args.resource == "model" and args.action == "bootstrap-chat":
            from bianbu_agent.cli.commands.models import run_chat_bootstrap
            args.config = args.config or _default_config_path()
            raise SystemExit(run_chat_bootstrap(args))
        exit_code = asyncio.run(_main_with_args(args))
    except KeyboardInterrupt:
        exit_code = 130
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        if args.debug:
            raise
        print(f"bianbu-agent: {exc}", file=sys.stderr)
        print("使用 bianbu-agent <命令> --help 查看用法；--debug 显示完整异常。", file=sys.stderr)
        exit_code = 1
    raise SystemExit(exit_code)


def _default_config_path() -> Path:
    project = Path.cwd() / "runtime.yaml"
    if project.is_file():
        return project
    return Path(os.environ.get("BIANBU_AGENT_CONFIG") or Path.home() / ".bianbu-agent/config.yaml").expanduser()


if __name__ == "__main__":
    main()


__all__ = ["build_parser", "main", "main_async"]
