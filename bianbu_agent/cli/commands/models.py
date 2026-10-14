from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from bianbu_agent.cli.output import emit
from bianbu_agent.config import load_runtime_config
from bianbu_agent.home import AgentHome
from bianbu_agent.models import (
    ModelConfig,
    ProviderConfig,
    YAMLModelStore,
    model_config_view,
)


async def model_command(store: YAMLModelStore, args: argparse.Namespace) -> int:
    if args.action == "list":
        value = [model_config_view(item) for item in await store.list()]
    elif args.action == "create":
        value = model_config_view(
            await store.create(
                ModelConfig(args.model_id, args.provider, args.model, args.base_url)
            )
        )
    elif args.action == "bootstrap-chat":
        print(
            "bootstrap-chat must be invoked through the bianbu-agent command.",
            file=sys.stderr,
        )
        return 2
    elif args.action == "show":
        stored = await store.get(args.model_id)
        value = None if stored is None else model_config_view(stored)
    else:
        value = {"deleted": await store.delete(args.model_id)}
    emit(value)
    return 0


async def chat_bootstrap_exists(store: YAMLModelStore) -> bool:
    return bool(await store.list_providers())


def prompt_chat_bootstrap() -> tuple[str, str, str] | int:
    if not sys.stdin.isatty():
        print("Chat model bootstrap requires an interactive terminal.", file=sys.stderr)
        return 2
    try:
        provider_id = input("Provider ID: ").strip()
        base_url = input("OpenAI-compatible Base URL: ").strip()
        api_key = getpass.getpass("API Key: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nChat model bootstrap cancelled.", file=sys.stderr)
        return 130
    if not provider_id or not base_url or not api_key:
        print("Provider ID, Base URL, and API Key are required.", file=sys.stderr)
        return 2
    return provider_id, base_url, api_key


async def inspect_chat_bootstrap(home: AgentHome) -> bool:
    store = YAMLModelStore(home.models_dir)
    await store.open()
    try:
        return await chat_bootstrap_exists(store)
    finally:
        await store.close()


async def persist_chat_bootstrap(
    home: AgentHome,
    values: tuple[str, str, str],
) -> int:
    store = YAMLModelStore(home.models_dir)
    await store.open()
    try:
        if await chat_bootstrap_exists(store):
            print(
                "Chat model is already configured; use the Web Console to change it.",
                file=sys.stderr,
            )
            return 1
        provider_id, base_url, api_key = values
        try:
            await store.create_provider(
                ProviderConfig(
                    id=provider_id,
                    name=provider_id,
                    protocol="openai-compatible",
                    base_url=base_url,
                    api_key=api_key,
                )
            )
        except ValueError as exc:
            if str(exc).startswith("record already exists:"):
                print(
                    "Chat model is already configured; use the Web Console to change it.",
                    file=sys.stderr,
                )
                return 1
            print(f"Chat model bootstrap failed: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"Chat model bootstrap failed: {exc}", file=sys.stderr)
            return 1
    finally:
        await store.close()
    emit({"provider_id": provider_id, "configured": True})
    return 0


def run_chat_bootstrap(args: argparse.Namespace) -> int:
    config = load_runtime_config(args.config)
    home = AgentHome(config.data_dir).prepare()
    if asyncio.run(inspect_chat_bootstrap(home)):
        print(
            "Chat model is already configured; use the Web Console to change it.",
            file=sys.stderr,
        )
        return 1
    values = prompt_chat_bootstrap()
    if isinstance(values, int):
        return values
    return asyncio.run(persist_chat_bootstrap(home, values))
