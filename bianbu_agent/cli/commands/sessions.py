from __future__ import annotations

import argparse

from bianbu_agent.cli.output import emit
from bianbu_agent.sessions import JSONSessionStore


async def session_command(store: JSONSessionStore, args: argparse.Namespace) -> int:
    if args.action == "list":
        value = await store.list()
    elif args.action == "create":
        value = await store.create(args.session_id, agent_id=args.agent_id, title=args.title)
    elif args.action == "show":
        value = await store.get(args.session_id)
    else:
        value = {"deleted": await store.delete(args.session_id)}
    emit(value)
    return 0
