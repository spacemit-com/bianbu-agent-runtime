from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any

import yaml

from bianbu_agent._internal.filesystem import create_file_if_absent
from bianbu_agent.errors import RuntimeConfigError


try:
    RUNTIME_VERSION = version("bianbu-agent")
except PackageNotFoundError:
    RUNTIME_VERSION = "0.1.0"


_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "data_dir",
        "service",
        "language",
        "require_file_backed_capabilities",
        "plugin_stop_timeout_seconds",
        "allow_unmanaged_capabilities",
        "presets",
        "runtime",
        "security",
        "plugins",
    }
)
_SERVICE_KEYS = frozenset({"title", "host", "port"})
_PRESET_KEYS = frozenset({"tools", "skills"})
_RUNTIME_KEYS = frozenset({"control", "worker"})
_CONTROL_RUNTIME_KEYS = frozenset({"execution", "worker_socket_root", "startup_timeout_seconds", "handshake_timeout_seconds", "heartbeat_interval_seconds", "heartbeat_timeout_seconds", "restart_policy"})
_EXECUTION_KEYS = frozenset({"requested_mode"})
_WORKER_KEYS = frozenset({"execution", "entrypoint", "startup_timeout_seconds", "max_concurrent_runs"})
_RESTART_KEYS = frozenset({"enabled", "max_restarts", "backoff_seconds"})
_SECURITY_KEYS = frozenset({"required", "securityd_uds", "client_dir", "securityd_home"})
_OPTIONAL_DEFAULTS: dict[str, Any] = {
    "service": {},
    "language": "zh",
    "require_file_backed_capabilities": True,
    "plugin_stop_timeout_seconds": 5.0,
    "allow_unmanaged_capabilities": False,
    "presets": {},
    "runtime": {},
    "security": {},
    "plugins": {},
}
_DEFAULT_YAML = """# Bianbu Agent runtime configuration. Do not store secrets here.
data_dir: .

service:
  title: Bianbu Agent
  host: 0.0.0.0
  port: 9899

language: zh
require_file_backed_capabilities: true
plugin_stop_timeout_seconds: 5.0
allow_unmanaged_capabilities: false

# Capabilities every Agent receives whatever its own selectors say.  Experts
# cannot turn them off, so a package that declares no tools still reaches the
# APM handbook and the file/process tools it needs to install its own Powers.
# Only entries the device actually ships are materialized.
presets:
  tools:
    - skills_list
    - skill_view
    - skill_manage
    - terminal
    - process
    - read_file
    - write_file
    - search_files
    - patch
  skills:
    - using-apm

plugins: {}

runtime:
  control:
    execution:
      requested_mode: host
    startup_timeout_seconds: 10
    handshake_timeout_seconds: 10
    heartbeat_interval_seconds: 5
    heartbeat_timeout_seconds: 15
  worker:
    execution:
      requested_mode: host
    startup_timeout_seconds: 10
    # Leave unset to admit independent Sessions without a global Run cap.
    max_concurrent_runs: null
"""


@dataclass(frozen=True, slots=True)
class RuntimeServiceConfig:
    title: str = "Bianbu Agent"
    host: str = "0.0.0.0"
    port: int = 9899

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _require_text(self.title, "service.title"))
        object.__setattr__(self, "host", _require_text(self.host, "service.host"))
        _require_port(self.port)


@dataclass(frozen=True, slots=True)
class RuntimeExecutionConfig:
    """One security.runtime execution preference.

    The value is deliberately only a preference.  The effective mode is
    returned by securityd and is never inferred from this configuration.
    """

    requested_mode: str = "host"

    def __post_init__(self) -> None:
        value = _require_text(self.requested_mode, "requested_mode").lower()
        if value not in {"sandbox", "host"}:
            raise RuntimeConfigError("requested_mode must be one of: sandbox, host")
        object.__setattr__(self, "requested_mode", value)


@dataclass(frozen=True, slots=True)
class RuntimeRestartConfig:
    enabled: bool = True
    max_restarts: int = 5
    backoff_seconds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0)

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise RuntimeConfigError("runtime.control.restart_policy.enabled must be boolean")
        if isinstance(self.max_restarts, bool) or not isinstance(self.max_restarts, int) or self.max_restarts < 0:
            raise RuntimeConfigError("runtime.control.restart_policy.max_restarts must be a non-negative integer")
        if isinstance(self.backoff_seconds, (str, bytes)):
            raise RuntimeConfigError("runtime.control.restart_policy.backoff_seconds must be a sequence")
        try:
            values = tuple(float(value) for value in self.backoff_seconds)
        except (TypeError, ValueError) as exc:
            raise RuntimeConfigError("runtime.control.restart_policy.backoff_seconds must be a sequence of numbers") from exc
        if not values or any(not math.isfinite(value) or value <= 0 for value in values):
            raise RuntimeConfigError("runtime.control.restart_policy.backoff_seconds must be positive")
        object.__setattr__(self, "backoff_seconds", values)


@dataclass(frozen=True, slots=True)
class RuntimeControlConfig:
    execution: RuntimeExecutionConfig = field(default_factory=lambda: RuntimeExecutionConfig("host"))
    worker_socket_root: str | Path | None = None
    startup_timeout_seconds: float = 10.0
    handshake_timeout_seconds: float = 10.0
    heartbeat_interval_seconds: float = 5.0
    heartbeat_timeout_seconds: float = 15.0
    restart_policy: RuntimeRestartConfig = field(default_factory=RuntimeRestartConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.execution, RuntimeExecutionConfig):
            raise RuntimeConfigError("runtime.control.execution must be RuntimeExecutionConfig")
        if self.worker_socket_root is not None:
            object.__setattr__(self, "worker_socket_root", _resolve_path(self.worker_socket_root))
        for name in ("startup_timeout_seconds", "handshake_timeout_seconds", "heartbeat_interval_seconds", "heartbeat_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) <= 0:
                raise RuntimeConfigError(f"runtime.control.{name} must be positive and finite")
            object.__setattr__(self, name, float(value))
        if not isinstance(self.restart_policy, RuntimeRestartConfig):
            raise RuntimeConfigError("runtime.control.restart_policy must be RuntimeRestartConfig")


@dataclass(frozen=True, slots=True)
class RuntimeWorkerConfig:
    execution: RuntimeExecutionConfig = field(default_factory=RuntimeExecutionConfig)
    entrypoint: tuple[str, ...] = ()
    startup_timeout_seconds: float = 10.0
    max_concurrent_runs: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.entrypoint, (str, bytes)):
            raise RuntimeConfigError("runtime.worker.entrypoint must be a sequence")
        try:
            raw_values = tuple(self.entrypoint)
        except TypeError as exc:
            raise RuntimeConfigError("runtime.worker.entrypoint must be a sequence") from exc
        if any(not isinstance(value, str) or not value.strip() or "\x00" in value for value in raw_values):
            raise RuntimeConfigError("runtime.worker.entrypoint contains an invalid value")
        object.__setattr__(self, "entrypoint", tuple(value.strip() for value in raw_values))
        if not isinstance(self.execution, RuntimeExecutionConfig):
            raise RuntimeConfigError("runtime.worker.execution must be RuntimeExecutionConfig")
        if isinstance(self.startup_timeout_seconds, bool) or not isinstance(self.startup_timeout_seconds, (int, float)) or not math.isfinite(float(self.startup_timeout_seconds)) or float(self.startup_timeout_seconds) <= 0:
            raise RuntimeConfigError("runtime.worker.startup_timeout_seconds must be positive")
        object.__setattr__(self, "startup_timeout_seconds", float(self.startup_timeout_seconds))
        if self.max_concurrent_runs is not None and (
            isinstance(self.max_concurrent_runs, bool)
            or not isinstance(self.max_concurrent_runs, int)
            or self.max_concurrent_runs <= 0
        ):
            raise RuntimeConfigError(
                "runtime.worker.max_concurrent_runs must be "
                "positive or null"
            )


@dataclass(frozen=True, slots=True)
class RuntimeProcessConfig:
    control: RuntimeControlConfig = field(default_factory=RuntimeControlConfig)
    worker: RuntimeWorkerConfig = field(default_factory=RuntimeWorkerConfig)

    def __post_init__(self) -> None:
        if not isinstance(self.control, RuntimeControlConfig) or not isinstance(self.worker, RuntimeWorkerConfig):
            raise RuntimeConfigError("runtime.control/worker has an invalid type")


@dataclass(frozen=True, slots=True)
class RuntimeSecurityConfig:
    """The installed Security client boundary used by a managed Worker.

    ``securityd_home`` is intentionally optional and is only a development
    compatibility path. Production images must ship ``client_dir`` and the
    system UDS; the source checkout must never be needed by a Worker.
    """

    required: bool = True
    securityd_uds: str = "/run/bianbu/securityd.sock"
    client_dir: str = "/usr/lib/bianbu-securityd/client"
    securityd_home: str | Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.required, bool):
            raise RuntimeConfigError("security.required must be boolean")
        object.__setattr__(self, "securityd_uds", _require_text(self.securityd_uds, "security.securityd_uds"))
        if not self.securityd_uds.startswith("/"):
            raise RuntimeConfigError("security.securityd_uds must be an absolute path")
        object.__setattr__(self, "client_dir", _require_text(self.client_dir, "security.client_dir"))
        if not self.client_dir.startswith("/"):
            raise RuntimeConfigError("security.client_dir must be an absolute path")
        if self.securityd_home is not None:
            object.__setattr__(self, "securityd_home", _resolve_path(self.securityd_home))


@dataclass(frozen=True, slots=True)
class RuntimePresetsConfig:
    """Capabilities every Agent receives whatever its own selectors say.

    The values are configuration, not code: a device profile decides which
    Tools and Skills are always on, and an entry this device does not ship is
    skipped rather than materialized.  Experts cannot deselect a preset, so a
    package that declares no Tools still reaches the APM handbook and the
    file/process Tools it needs to install its own Powers.
    """

    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("tools", "skills"):
            object.__setattr__(self, name, _preset_selectors(getattr(self, name), name))


def _preset_selectors(value: object, field_name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise RuntimeConfigError(f"presets.{field_name} must be a sequence")
    try:
        raw_values = tuple(value)
    except TypeError as exc:
        raise RuntimeConfigError(f"presets.{field_name} must be a sequence") from exc
    selectors: list[str] = []
    for item in raw_values:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise RuntimeConfigError(f"presets.{field_name} contains an invalid selector")
        selector = item.strip()
        if selector == "*":
            raise RuntimeConfigError(f"presets.{field_name} must name capabilities explicitly")
        if selector in selectors:
            raise RuntimeConfigError(f"presets.{field_name} must not repeat a selector")
        selectors.append(selector)
    return tuple(selectors)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    data_dir: str | Path
    config_path: str | Path | None = None
    service: RuntimeServiceConfig = field(default_factory=RuntimeServiceConfig)
    plugin_dirs: tuple[str | Path, ...] = ()
    mcp_dirs: tuple[str | Path, ...] = ()
    skill_dirs: tuple[str | Path, ...] = ()
    plugin_configs: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    language: str = "zh"
    require_file_backed_capabilities: bool = True
    plugin_stop_timeout_seconds: float = 5.0
    allow_unmanaged_capabilities: bool = False
    presets: RuntimePresetsConfig = field(default_factory=RuntimePresetsConfig)
    runtime: RuntimeProcessConfig = field(default_factory=RuntimeProcessConfig)
    security: RuntimeSecurityConfig = field(default_factory=RuntimeSecurityConfig)
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int) or self.schema_version not in {1, 2}:
            raise RuntimeConfigError("schema_version must be 1 or 2")
        data_dir = _resolve_path(self.data_dir)
        object.__setattr__(self, "data_dir", data_dir)
        if self.config_path is not None:
            object.__setattr__(self, "config_path", _resolve_path(self.config_path))
        if not isinstance(self.service, RuntimeServiceConfig):
            raise RuntimeConfigError("service must be a RuntimeServiceConfig")
        defaults = {
            "plugin_dirs": (data_dir / "plugins",),
            "mcp_dirs": (data_dir / "mcps",),
            "skill_dirs": (data_dir / "skills",),
        }
        for name, default in defaults.items():
            values = getattr(self, name) or default
            if isinstance(values, (str, Path)):
                raise RuntimeConfigError(f"{name} must be a directory sequence")
            object.__setattr__(self, name, tuple(_resolve_path(value) for value in values))
        if not isinstance(self.plugin_configs, Mapping):
            raise RuntimeConfigError("plugin_configs must be a mapping")
        normalized_plugin_configs: dict[str, MappingProxyType] = {}
        for plugin_id, raw_config in self.plugin_configs.items():
            normalized_id = _require_text(plugin_id, "plugins plugin ID")
            if not isinstance(raw_config, Mapping):
                raise RuntimeConfigError(
                    f"plugins.{normalized_id} must be a mapping"
                )
            normalized_plugin_configs[normalized_id] = MappingProxyType(
                dict(raw_config)
            )
        object.__setattr__(
            self,
            "plugin_configs",
            MappingProxyType(normalized_plugin_configs),
        )
        object.__setattr__(self, "language", _require_language(self.language))
        for name in ("require_file_backed_capabilities", "allow_unmanaged_capabilities"):
            if not isinstance(getattr(self, name), bool):
                raise RuntimeConfigError(f"{name} must be a boolean")
        timeout = self.plugin_stop_timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise RuntimeConfigError("plugin_stop_timeout_seconds must be a number")
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise RuntimeConfigError("plugin_stop_timeout_seconds timeout must be positive and finite")
        object.__setattr__(self, "plugin_stop_timeout_seconds", timeout)
        if not isinstance(self.runtime, RuntimeProcessConfig):
            raise RuntimeConfigError("runtime must be a RuntimeProcessConfig")
        if not isinstance(self.presets, RuntimePresetsConfig):
            raise RuntimeConfigError("presets must be a RuntimePresetsConfig")
        if not isinstance(self.security, RuntimeSecurityConfig):
            raise RuntimeConfigError("security must be a RuntimeSecurityConfig")


def load_runtime_config(path: str | Path) -> RuntimeConfig:
    """Load one strict runtime YAML file without creating files or directories."""

    config_path = _resolve_path(path)
    if not config_path.is_file():
        raise RuntimeConfigError(f"runtime config does not exist: {config_path}")
    document = _read_document(config_path)
    return _parse_document(document, config_path)


def init_runtime_config(path: str | Path) -> Path:
    """Create the documented runtime template exactly once."""

    config_path = _resolve_path(path)
    if not create_file_if_absent(config_path, _DEFAULT_YAML.encode("utf-8")):
        raise RuntimeConfigError(f"runtime config already exists: {config_path}")
    return config_path


def update_runtime_config(
    path: str | Path,
    updates: dict[str, Any],
) -> RuntimeConfig:
    """Validate and atomically persist a partial runtime configuration update."""

    config_path = _resolve_path(path)
    current = _read_document(config_path)
    _parse_document(current, config_path)
    if not isinstance(updates, dict):
        raise RuntimeConfigError("config updates must be an object")
    _require_string_keys(updates)
    _reject_unknown(updates, _TOP_LEVEL_KEYS, "config")
    candidate = dict(current)
    for key, value in updates.items():
        if key == "service" and isinstance(value, dict):
            existing = candidate.get("service", {})
            candidate[key] = {**(existing if isinstance(existing, dict) else {}), **value}
        else:
            candidate[key] = value
    validated = _parse_document(candidate, config_path)
    try:
        payload = yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False).encode("utf-8")
        _replace_file_atomically(config_path, payload)
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(f"could not update config YAML: {exc}") from exc
    return validated


def _read_document(path: Path) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeConfigError(f"runtime config does not exist: {path}") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RuntimeConfigError(f"invalid config YAML at {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeConfigError("config YAML must contain a top-level mapping")
    _require_string_keys(parsed)
    return parsed


def _parse_document(document: dict[str, Any], path: Path) -> RuntimeConfig:
    _reject_unknown(document, _TOP_LEVEL_KEYS, "config")
    for required in ("data_dir",):
        if required not in document:
            raise RuntimeConfigError(f"missing required config key: {required}")
    values = {**_OPTIONAL_DEFAULTS, **document}
    service_value = values["service"]
    if not isinstance(service_value, dict):
        raise RuntimeConfigError("service must be a mapping")
    _require_string_keys(service_value)
    _reject_unknown(service_value, _SERVICE_KEYS, "service")
    service = RuntimeServiceConfig(
        title=service_value.get("title", "Bianbu Agent"),
        host=service_value.get("host", "0.0.0.0"),
        port=service_value.get("port", 9899),
    )
    runtime = _parse_runtime(values.get("runtime", {}), path)
    security = _parse_security(values.get("security", {}), path)
    presets = _parse_presets(values.get("presets", {}))
    plugin_configs = values.get("plugins", {})
    if not isinstance(plugin_configs, dict):
        raise RuntimeConfigError("plugins must be a mapping")
    _require_string_keys(plugin_configs)
    for plugin_id, plugin_config in plugin_configs.items():
        if not isinstance(plugin_config, dict):
            raise RuntimeConfigError(f"plugins.{plugin_id} must be a mapping")
        _require_string_keys(plugin_config)
    # A managed sandbox Worker receives a supervisor-issued canonical data
    # root.  The YAML remains the signed source of configuration; this only
    # relocates its private filesystem root into the namespace and is never
    # accepted from HTTP or an Agent tool.
    worker_data_root = os.environ.get("BIANBU_RUNTIME_DATA_DIR", "").strip()
    data_value = worker_data_root if (
        os.environ.get("BIANBU_RUNTIME_ROLE", "") == "agent-worker"
        and worker_data_root
    ) else values["data_dir"]
    return RuntimeConfig(
        config_path=path,
        schema_version=values.get("schema_version", 1),
        data_dir=_resolve_config_path(data_value, path.parent),
        service=service,
        language=values["language"],
        require_file_backed_capabilities=values["require_file_backed_capabilities"],
        plugin_stop_timeout_seconds=values["plugin_stop_timeout_seconds"],
        allow_unmanaged_capabilities=values["allow_unmanaged_capabilities"],
        runtime=runtime,
        security=security,
        presets=presets,
        plugin_configs=plugin_configs,
    )


def _parse_presets(value: object) -> RuntimePresetsConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeConfigError("presets must be a mapping")
    _require_string_keys(value)
    _reject_unknown(value, _PRESET_KEYS, "presets")
    return RuntimePresetsConfig(
        tools=value.get("tools", ()),
        skills=value.get("skills", ()),
    )


def _parse_security(value: object, path: Path) -> RuntimeSecurityConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeConfigError("security must be a mapping")
    _require_string_keys(value)
    _reject_unknown(value, _SECURITY_KEYS, "security")
    return RuntimeSecurityConfig(
        required=value.get("required", True),
        securityd_uds=value.get("securityd_uds", "/run/bianbu/securityd.sock"),
        client_dir=value.get("client_dir", "/usr/lib/bianbu-securityd/client"),
        securityd_home=value.get("securityd_home"),
    )


def _parse_runtime(value: object, path: Path) -> RuntimeProcessConfig:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise RuntimeConfigError("runtime must be a mapping")
    _require_string_keys(value)
    _reject_unknown(value, _RUNTIME_KEYS, "runtime")
    control_value = value.get("control", {})
    worker_value = value.get("worker", {})
    if not isinstance(control_value, dict) or not isinstance(worker_value, dict):
        raise RuntimeConfigError("runtime.control and runtime.worker must be mappings")
    _require_string_keys(control_value); _reject_unknown(control_value, _CONTROL_RUNTIME_KEYS, "runtime.control")
    _require_string_keys(worker_value); _reject_unknown(worker_value, _WORKER_KEYS, "runtime.worker")

    def execution(raw: object, scope: str, default: str) -> RuntimeExecutionConfig:
        if raw is None: raw = {}
        if not isinstance(raw, dict): raise RuntimeConfigError(f"{scope}.execution must be a mapping")
        _require_string_keys(raw); _reject_unknown(raw, _EXECUTION_KEYS, f"{scope}.execution")
        return RuntimeExecutionConfig(raw.get("requested_mode", default))

    raw_restart = control_value.get("restart_policy", {})
    if not isinstance(raw_restart, dict): raise RuntimeConfigError("runtime.control.restart_policy must be a mapping")
    _require_string_keys(raw_restart); _reject_unknown(raw_restart, _RESTART_KEYS, "runtime.control.restart_policy")
    restart = RuntimeRestartConfig(
        enabled=raw_restart.get("enabled", True),
        max_restarts=raw_restart.get("max_restarts", 5),
        backoff_seconds=tuple(raw_restart.get("backoff_seconds", (1, 2, 5, 10, 30))),
    )
    worker_socket_root = control_value.get("worker_socket_root")
    if worker_socket_root is not None:
        worker_socket_root = _resolve_config_path(worker_socket_root, path.parent)
    control = RuntimeControlConfig(
        execution=execution(control_value.get("execution"), "runtime.control", "host"),
        worker_socket_root=worker_socket_root,
        startup_timeout_seconds=control_value.get("startup_timeout_seconds", 10),
        handshake_timeout_seconds=control_value.get("handshake_timeout_seconds", 10),
        heartbeat_interval_seconds=control_value.get("heartbeat_interval_seconds", 5),
        heartbeat_timeout_seconds=control_value.get("heartbeat_timeout_seconds", 15),
        restart_policy=restart,
    )
    entrypoint = worker_value.get("entrypoint", ())
    if isinstance(entrypoint, str) or not isinstance(entrypoint, (list, tuple)):
        raise RuntimeConfigError("runtime.worker.entrypoint must be a sequence")
    return RuntimeProcessConfig(
        control=control,
        worker=RuntimeWorkerConfig(
            execution=execution(worker_value.get("execution"), "runtime.worker", "host"),
            entrypoint=tuple(entrypoint),
            startup_timeout_seconds=worker_value.get("startup_timeout_seconds", 10),
            max_concurrent_runs=worker_value.get("max_concurrent_runs"),
        ),
    )


def _resolve_config_path(value: object, base: Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise RuntimeConfigError(f"invalid runtime path: {value!r}")
    selected = Path(value).expanduser()
    return (selected if selected.is_absolute() else base / selected).resolve()


def _resolve_path(value: str | Path) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise RuntimeConfigError(f"invalid runtime path: {value!r}") from exc


def _replace_file_atomically(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _require_string_keys(values: dict[Any, Any]) -> None:
    if not all(isinstance(key, str) for key in values):
        raise RuntimeConfigError("config keys must be strings")


def _reject_unknown(values: dict[str, Any], allowed: frozenset[str], scope: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise RuntimeConfigError(f"unknown {scope} key(s): {', '.join(sorted(unknown))}")


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _require_language(value: object) -> str:
    language = _require_text(value, "language")
    if language not in {"zh", "en"}:
        raise RuntimeConfigError("language must be one of: zh, en")
    return language


def _require_port(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeConfigError("service.port must be an integer")
    if not 1 <= value <= 65535:
        raise RuntimeConfigError("service.port must be between 1 and 65535")


__all__ = [
    "RUNTIME_VERSION",
    "RuntimeConfig",
    "RuntimeServiceConfig",
    "RuntimeExecutionConfig",
    "RuntimeRestartConfig",
    "RuntimeControlConfig",
    "RuntimeWorkerConfig",
    "RuntimeProcessConfig",
    "RuntimeSecurityConfig",
    "init_runtime_config",
    "load_runtime_config",
    "update_runtime_config",
]
