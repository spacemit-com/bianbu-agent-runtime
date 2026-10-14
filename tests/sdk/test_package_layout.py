from __future__ import annotations

import importlib.util
from pathlib import Path

import bianbu_agent


PACKAGE_ROOT = Path(bianbu_agent.__file__).parent


def test_package_root_contains_only_stable_facade_and_configuration_modules() -> None:
    assert {path.name for path in PACKAGE_ROOT.glob("*.py")} == {
        "__init__.py",
        "config.py",
        "errors.py",
        "home.py",
        "memory_identity.py",
        "provisioning.py",
        "system_resources.py",
    }


def test_security_implementation_is_owned_by_security_package() -> None:
    security_root = PACKAGE_ROOT / "security"
    assert security_root.is_dir()
    assert {
        path.name for path in security_root.glob("*.py")
    } == {
        "__init__.py",
        "agent_registration.py",
        "dbus_client_helper.py",
        "execution_runtime.py",
        "network_learning_collector.py",
        "network_learning_privileged.py",
        "security_codesign.py",
        "security_errors.py",
        "security_dlp.py",
        "security_provider_policy.py",
        "security_runtime.py",
        "security_sandbox.py",
        "security_skill_dlp.py",
        "security_tool_runner.py",
        "security_middleware.py",
        "seccomp_audit.py",
        "syscall_catalog.py",
        "syscall_collector.py",
    }


def test_removed_implementation_module_paths_do_not_resolve() -> None:
    removed = (
        "agent_manager",
        "app",
        "control",
        "default_engine",
        "engine",
        "events",
        "langgraph",
        "message_ids",
        "process",
        "run_manager",
        "runtime",
        "service",
        "service_runtime",
        "stores",
        "execution_runtime",
        "security_codesign",
        "security_errors",
        "security_provider_policy",
        "security_runtime",
        "security_sandbox",
        "security_skill_dlp",
        "security_tool_runner",
    )
    for module in removed:
        assert importlib.util.find_spec(f"bianbu_agent.{module}") is None


def test_production_sources_do_not_implement_clarification_protocol() -> None:
    suffixes = {".py", ".js", ".css", ".html"}
    offenders: list[str] = []
    for path in PACKAGE_ROOT.rglob("*"):
        if (
            path.suffix not in suffixes
            or "vendor" in path.parts
            or path.name == "axios.min.js"
        ):
            continue
        if "clarif" in path.read_text(encoding="utf-8").lower():
            offenders.append(str(path.relative_to(PACKAGE_ROOT)))
    assert offenders == []
