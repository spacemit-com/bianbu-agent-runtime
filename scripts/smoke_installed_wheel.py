"""Build, install, and exercise the published wheel outside this checkout.

Run with ``uv run python scripts/smoke_installed_wheel.py``.  The child process has
only a fresh virtual environment containing the built wheel, then starts the packaged
FastAPI application under Uvicorn and checks the public contract end to end.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import textwrap


PROJECT_ROOT = Path(__file__).parents[1]


def _bound_listener():
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    return listener

_SMOKE_PROGRAM = textwrap.dedent(
    """
    import asyncio
    from importlib.util import find_spec
    import socket
    from dataclasses import dataclass
    from pathlib import Path

    import httpx
    import uvicorn

    import bianbu_agent
    from bianbu_agent import (
        AgentSpec,
        BianbuAgentRuntime,
        InMemoryAgentRegistry,
        RunEvent,
        RuntimeConfig,
        PluginManager,
        PluginSpec,
        create_app,
    )
    from bianbu_agent.plugins import (
        PluginManager as PackagePluginManager,
        PluginSpec as PackagePluginSpec,
    )


    @dataclass
    class SmokeModel:
        model: str = "smoke-model"


    class SmokeEngine:
        async def stream(self, prompt, *, control, **_):
            yield RunEvent("token", {"text": "wheel smoke"})
            yield RunEvent("done", {})


    __BOUND_LISTENER__


    async def main():
        package_path = Path(bianbu_agent.__file__).resolve()
        assert "site-packages" in package_path.parts, package_path
        assert PluginManager is PackagePluginManager
        assert PluginSpec is PackagePluginSpec
        assert find_spec("bianbu_agent.plugin_manager") is None
        assert find_spec("bianbu_agent.plugin_specs") is None
        runtime = BianbuAgentRuntime(
            config=RuntimeConfig(data_dir=Path.cwd() / "runtime-data"),
            default_model=SmokeModel(),
            agent_id="default",
            agent_registry=InMemoryAgentRegistry((AgentSpec(id="default", name="Default"),)),
            engine_builder=lambda *_: SmokeEngine(),
        )
        listener = _bound_listener()
        selected_port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(runtime), host="127.0.0.1", port=selected_port, log_level="error"
            )
        )
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(15):
                while not server.started:
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{selected_port}", timeout=10
            ) as client:
                openapi = await client.get("/openapi.json")
                assert openapi.status_code == 200
                paths = openapi.json()["paths"]
                assert "/api/v1/runs" in paths
                assert "/api/v1/providers" in paths
                assert "/api/v1/agent-top" in paths
                assert "/api/v1/capabilities/{kind}" in paths
                assert "/api/v1/local-models" in paths
                assert "/api/v1/models" not in paths
                assert "/api/v1/files" not in paths
                assert "/api/v1/capability-bindings" not in paths
                assert not any(
                    path.startswith("/api/v1/plugin-consoles") for path in paths
                )
                assert set(paths["/api/v1/mcp-health-checks"]) >= {"post"}
                operation_count = sum(
                    method in {"get", "post", "put", "patch", "delete"}
                    for operations in paths.values()
                    for method in operations
                )
                assert operation_count == 47, operation_count

                mcp_health = await client.post("/api/v1/mcp-health-checks")
                assert mcp_health.status_code == 200, mcp_health.text
                assert mcp_health.json() == {"data": []}, mcp_health.text

                created = await client.post(
                    "/api/v1/runs",
                    json={
                        "session_id": "wheel-smoke",
                        "prompt": "verify installed wheel",
                        "idempotency_key": "wheel:smoke:20260810",
                    },
                )
                assert created.status_code == 202, created.text
                run_id = created.json()["data"]["id"]
                for _ in range(100):
                    snapshot = await client.get(f"/api/v1/runs/{run_id}")
                    assert snapshot.status_code == 200, snapshot.text
                    if snapshot.json()["data"]["status"] == "completed":
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("Run did not complete")

                events = await client.get(f"/api/v1/runs/{run_id}/events?cursor=0")
                assert events.status_code == 200
                assert "id: 1" in events.text and "wheel smoke" in events.text
                providers = await client.get("/api/v1/providers")
                assert providers.status_code == 200 and providers.json() == {"data": []}
                web = await client.get("/chat")
                assert web.status_code == 200 and 'id="chat-input"' in web.text
                assert (await client.get("/api/health")).status_code == 404
                assert (await client.post("/api/message", json={})).status_code == 404
        finally:
            server.should_exit = True
            await task
            listener.close()


    asyncio.run(main())
    print(
        "installed-wheel smoke: OpenAPI, Run/events, MCP health, Agent Top, "
        "Web UI, old paths absent"
    )
    """
).replace("__BOUND_LISTENER__", inspect.getsource(_bound_listener).strip())


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bianbu-agent-wheel-smoke-") as temporary:
        root = Path(temporary)
        distribution = root / "dist"
        venv = root / "venv"
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(distribution)],
            cwd=PROJECT_ROOT,
            check=True,
        )
        wheel = next(distribution.glob("bianbu_agent-*.whl"))
        subprocess.run(["uv", "venv", str(venv)], check=True)
        python = venv / "bin" / "python"
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)],
            check=True,
        )
        environment = dict(os.environ)
        environment["PYTHONNOUSERSITE"] = "1"
        environment.pop("PYTHONPATH", None)
        subprocess.run(
            [str(python), "-c", _SMOKE_PROGRAM],
            cwd=root,
            env=environment,
            check=True,
        )


if __name__ == "__main__":
    main()
