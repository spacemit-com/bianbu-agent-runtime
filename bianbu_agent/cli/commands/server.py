from __future__ import annotations

import ipaddress
import socket
import sys

import psutil
import uvicorn

from bianbu_agent.config import RuntimeConfig
from bianbu_agent.server.factory import create_runtime_app
from bianbu_agent.server.process import RuntimeProcessOwner
from bianbu_agent.security.execution_runtime import production_launch_allowed


_VIRTUAL_INTERFACE_PREFIXES = ("br-", "docker", "veth", "virbr")


def _lan_ipv4_addresses() -> tuple[str, ...]:
    stats_by_interface = psutil.net_if_stats()
    addresses: list[str] = []
    for interface, entries in psutil.net_if_addrs().items():
        stats = stats_by_interface.get(interface)
        flags = {
            flag.strip().lower()
            for flag in str(getattr(stats, "flags", "")).split(",")
        }
        if (
            stats is None
            or not stats.isup
            or interface.lower().startswith(_VIRTUAL_INTERFACE_PREFIXES)
            or "pointopoint" in flags
        ):
            continue
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            try:
                address = ipaddress.ip_address(entry.address)
            except ValueError:
                continue
            if address.is_loopback or address.is_link_local or address.is_unspecified:
                continue
            value = str(address)
            if value not in addresses:
                addresses.append(value)
    return tuple(addresses)


def _browser_access_messages(
    host: str,
    port: int,
    *,
    lan_addresses: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    if host != "0.0.0.0":
        return ()
    if lan_addresses is None:
        lan_addresses = _lan_ipv4_addresses()
    lan_address = lan_addresses[0] if lan_addresses else "<本机局域网IP>"
    return (
        f"本机请访问：http://127.0.0.1:{port}",
        f"其他设备请访问：http://{lan_address}:{port}",
        f"请勿在浏览器中使用：http://0.0.0.0:{port}",
    )


async def serve(config: RuntimeConfig, *, role: str = "control") -> int:
    if not production_launch_allowed():
        raise RuntimeError("生产模式禁止裸启动，请由受信 Runtime Manager/systemd 启动")
    app = create_runtime_app(config) if role == "control" else create_runtime_app(config, role=role)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.service.host,
            port=config.service.port,
            workers=1,
        )
    )
    owner = RuntimeProcessOwner(config)
    owner.acquire()
    try:
        for message in _browser_access_messages(
            config.service.host,
            config.service.port,
        ):
            print(message, file=sys.stderr, flush=True)
        await server.serve()
        return 0
    finally:
        owner.release()
