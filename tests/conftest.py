"""Keep the general test suite independent from a host security daemon."""
from __future__ import annotations

import inspect
import os
import platform
import shutil

import pytest


def _patch_playwright_for_riscv64() -> None:
    """Run the browser suites on riscv64 boards with the system Chromium.

    Playwright ships neither a driver nor a Chromium build for riscv64, so it
    reports the platform as ``<unknown>`` and ``chromium.launch()`` fails even
    though the board ships a working ``chromium``. Point the driver at the
    system Node (the bundled one links an ICU version the board does not have)
    and every launch at the system Chromium. Every other platform is untouched.
    """

    if platform.machine() != "riscv64":
        return
    node = shutil.which("node")
    chrome = shutil.which("chromium") or shutil.which("chromium-browser")
    if node and "PLAYWRIGHT_NODEJS_PATH" not in os.environ:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = node
    if not chrome:
        return
    from playwright.async_api import BrowserType as AsyncBrowserType
    from playwright.sync_api import BrowserType as SyncBrowserType

    def _patch(browser_type) -> None:
        original = browser_type.launch

        if inspect.iscoroutinefunction(original):

            async def launch(self, **kwargs):
                kwargs.setdefault("executable_path", chrome)
                kwargs.setdefault("args", ["--no-sandbox"])
                return await original(self, **kwargs)

        else:

            def launch(self, **kwargs):
                kwargs.setdefault("executable_path", chrome)
                kwargs.setdefault("args", ["--no-sandbox"])
                return original(self, **kwargs)

        browser_type.launch = launch

    _patch(AsyncBrowserType)
    _patch(SyncBrowserType)


_patch_playwright_for_riscv64()


@pytest.fixture(autouse=True)
def _disable_external_security_daemon(monkeypatch):
    """Tests opt in explicitly when they exercise bianbu-security behavior."""
    monkeypatch.setenv("BIANBU_SECURITY", "0")


@pytest.fixture(autouse=True)
def _isolate_system_mcp_catalog(monkeypatch, tmp_path):
    """Installed board MCPs are not fixtures; integration tests opt in explicitly."""
    from bianbu_agent.mcp import system
    monkeypatch.setattr(system, 'SYSTEM_MCP_ROOT', tmp_path / 'system-mcps')


@pytest.fixture
def allow_model_dlp(monkeypatch):
    """Capability/approval unit fixtures have no daemon; their text is synthetic.

    Opt in explicitly. DLP boundary tests provide their own scan verdicts.
    """
    from bianbu_agent.security import security_runtime
    original = security_runtime.security_client_call
    def call(method, *args, **kwargs):
        if method == 'dlp_scan':
            return {'findings': []}
        return original(method, *args, **kwargs)
    monkeypatch.setattr(security_runtime, 'security_client_call', call)
