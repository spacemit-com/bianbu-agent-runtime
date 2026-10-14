from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from bianbu_agent.plugins import (
    PluginDependency,
    PluginHealth,
    PluginManifest,
    PluginRegistry,
    ToolContribution,
    ToolResult,
    ToolSpec,
)
from bianbu_agent.plugins.host import PluginHost, PluginState


@dataclass
class _Plugin:
    manifest: PluginManifest
    calls: list[str]
    fail_on: str = ""

    async def setup(self, context):
        self.context = context
        self.calls.append(f"{self.manifest.id}.setup")
        if self.fail_on == "setup":
            raise RuntimeError("setup boom")
        return None

    async def start(self) -> None:
        self.calls.append(f"{self.manifest.id}.start")
        if self.fail_on == "start":
            raise RuntimeError("start boom")

    async def health(self) -> PluginHealth:
        return PluginHealth.healthy()

    async def reconfigure(self, config: Mapping[str, object]) -> None:
        self.calls.append(f"{self.manifest.id}.reconfigure")

    async def stop(self) -> None:
        self.calls.append(f"{self.manifest.id}.stop")


def _manifest(plugin_id: str, dependency: str = "") -> PluginManifest:
    return PluginManifest(
        id=plugin_id,
        version="1.0.0",
        dependencies=(PluginDependency(dependency),) if dependency else (),
        config_schema={
            "type": "object",
            "properties": {"mode": {"type": "string"}},
            "additionalProperties": False,
        },
    )


def _host(tmp_path: Path, calls: list[str], *, failures: dict[str, str] | None = None):
    registry = PluginRegistry()
    failures = failures or {}
    for manifest in (_manifest("a.core"), _manifest("b.feature", "a.core")):
        registry.register(
            lambda context, manifest=manifest: _Plugin(
                manifest,
                calls,
                failures.get(manifest.id, ""),
            ),
            manifest,
        )
    return PluginHost(
        registry=registry,
        enabled_ids={"a.core", "b.feature"},
        data_root=tmp_path,
    )


@pytest.mark.asyncio
async def test_host_starts_in_dependency_order_and_stops_in_reverse(tmp_path: Path) -> None:
    calls: list[str] = []
    host = _host(tmp_path, calls)

    generation = await host.start()

    assert calls[:4] == [
        "a.core.setup",
        "a.core.start",
        "b.feature.setup",
        "b.feature.start",
    ]
    assert generation.id == 1
    assert generation.statuses["a.core"].state is PluginState.RUNNING
    await host.stop()
    assert calls[-2:] == ["b.feature.stop", "a.core.stop"]
    await host.stop()
    assert calls.count("a.core.stop") == 1


@pytest.mark.asyncio
async def test_start_failure_suppresses_dependents_but_not_independent_plugins(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    registry = PluginRegistry()
    manifests = (
        _manifest("a.core"),
        _manifest("b.feature", "a.core"),
        _manifest("c.independent"),
    )
    for manifest in manifests:
        registry.register(
            lambda context, manifest=manifest: _Plugin(
                manifest,
                calls,
                "start" if manifest.id == "a.core" else "",
            ),
            manifest,
        )
    host = PluginHost(
        registry=registry,
        enabled_ids={item.id for item in manifests},
        data_root=tmp_path,
    )

    generation = await host.start()

    assert generation.statuses["a.core"].state is PluginState.FAILED
    assert generation.statuses["b.feature"].state is PluginState.FAILED
    assert generation.statuses["c.independent"].state is PluginState.RUNNING
    assert "b.feature.setup" not in calls
    assert "c.independent.start" in calls


@pytest.mark.asyncio
async def test_unhealthy_plugin_is_not_published(tmp_path: Path) -> None:
    manifest = _manifest("a.unhealthy")

    class UnhealthyPlugin(_Plugin):
        async def health(self) -> PluginHealth:
            return PluginHealth.unhealthy("not ready")

    registry = PluginRegistry()
    registry.register(lambda _context: UnhealthyPlugin(manifest, []), manifest)
    host = PluginHost(
        registry=registry,
        enabled_ids={manifest.id},
        data_root=tmp_path,
    )

    generation = await host.start()

    assert generation.plugins == {}
    assert generation.statuses[manifest.id].state is PluginState.FAILED
    assert generation.statuses[manifest.id].diagnostics[0].code == "plugin_unhealthy"


@pytest.mark.asyncio
async def test_reconfigure_publishes_new_generation_without_mutating_active_lease(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    host = _host(tmp_path, calls)
    await host.start({"a.core": {"mode": "old"}})

    async with host.lease() as old:
        diagnostics = await host.reconfigure({"a.core": {"mode": "new"}})
        assert diagnostics == ()
        assert old.id == 1
        assert host.snapshot().id == 2
        assert old.plugins["a.core"].config["mode"] == "old"
        assert host.snapshot().plugins["a.core"].config["mode"] == "new"
        assert "a.core.stop" not in calls

    assert calls.count("a.core.stop") == 1


@pytest.mark.asyncio
async def test_failed_reconfigure_keeps_current_generation(tmp_path: Path) -> None:
    calls: list[str] = []
    host = _host(tmp_path, calls)
    original = await host.start({"a.core": {"mode": "old"}})

    diagnostics = await host.reconfigure({"a.core": {"mode": 42}})

    assert diagnostics
    assert host.snapshot() is original
    assert host.snapshot().plugins["a.core"].config["mode"] == "old"


def test_snapshot_and_lease_require_a_started_host(tmp_path: Path) -> None:
    host = _host(tmp_path, [])
    with pytest.raises(RuntimeError, match="not started"):
        host.snapshot()


@pytest.mark.asyncio
async def test_host_injects_every_shared_service(tmp_path: Path) -> None:
    manifest = PluginManifest(id="runs.observer", version="1.0.0")
    registry = PluginRegistry()
    registry.register(lambda _context: _Plugin(manifest, []), manifest)
    host = PluginHost(
        registry=registry,
        enabled_ids={manifest.id},
        data_root=tmp_path,
        services={"runs": object(), "tools": object()},
    )

    generation = await host.start()

    assert generation.plugins[manifest.id].instance.context.require("runs") is not None
    assert generation.plugins[manifest.id].instance.context.require("tools") is not None
    await host.stop()


@pytest.mark.asyncio
async def test_reconfigure_contribution_conflict_keeps_old_generation(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class CatalogPlugin(_Plugin):
        async def setup(self, context):
            self.context = context
            self.calls.append(f"{self.manifest.id}.setup")
            name = (
                "shared_tool"
                if context.config.get("mode") == "conflict"
                else self.manifest.id.replace(".", "_")
            )

            async def handler(_tool_context, _arguments):
                return ToolResult("ok")

            context.scope.register_tool(
                ToolContribution(
                    self.manifest.id,
                    ToolSpec(name, "", {"type": "object"}),
                    handler,
                )
            )
            return None

    registry = PluginRegistry()
    for plugin_id in ("a.catalog", "b.catalog"):
        manifest = PluginManifest(
            id=plugin_id,
            version="1.0.0",
            config_schema={
                "type": "object",
                "properties": {"mode": {"type": "string"}},
                "additionalProperties": False,
            },
        )
        registry.register(
            lambda _context, manifest=manifest: CatalogPlugin(manifest, calls),
            manifest,
        )
    host = PluginHost(
        registry=registry,
        enabled_ids={"a.catalog", "b.catalog"},
        data_root=tmp_path,
    )
    original = await host.start()

    diagnostics = await host.reconfigure(
        {
            "a.catalog": {"mode": "conflict"},
            "b.catalog": {"mode": "conflict"},
        }
    )

    assert diagnostics[0].code == "generation_build_failed"
    assert host.snapshot() is original
    assert calls.count("a.catalog.stop") == 1
    assert calls.count("b.catalog.stop") == 1
    await host.stop()




@pytest.mark.asyncio
async def test_empty_message_start_failure_publishes_a_diagnostic_instead_of_raising(
    tmp_path: Path,
) -> None:
    """A transport error with no text must not take down the whole Generation.

    ``httpx.ConnectError`` and anyio's ``BrokenResourceError`` stringify to
    nothing; on K3 that turned one flaky remote MCP server into
    ``failed to verify installed capabilities: message must not be empty`` and
    every Agent lost every Plugin and MCP of that Generation.
    """

    calls: list[str] = []
    registry = PluginRegistry()
    manifest = _manifest("a.flaky")
    registry.register(
        lambda context: _EmptyMessagePlugin(manifest, calls),
        manifest,
    )
    host = PluginHost(registry=registry, enabled_ids={"a.flaky"}, data_root=tmp_path)

    generation = await host.start()

    status = generation.statuses["a.flaky"]
    assert status.state is PluginState.FAILED
    assert status.diagnostics[0].code == "plugin_start_failed"
    assert status.diagnostics[0].message == "BrokenResourceError"
    assert status.health.message == "BrokenResourceError"


class _EmptyMessagePlugin(_Plugin):
    async def start(self) -> None:
        self.calls.append(f"{self.manifest.id}.start")
        raise BrokenResourceError()


class BrokenResourceError(ConnectionError):
    """Stand-in for anyio's error, whose ``str`` is empty like httpx's."""

    def __str__(self) -> str:
        return ""
