"""Turn an ``npx`` MCP declaration into a local npm installation.

``npx`` resolves its package on every start: the first start downloads the
whole dependency tree, which on this board took 23s for a 116MB tree and once
exceeded the MCP startup budget entirely, leaving the server unusable and a
half-extracted npm cache behind.  Installing the package once, at install
time, keeps the declaration working offline and makes every start a local
process launch.

Installing happens in the background, so this module also has to survive being
killed: every step either lands in a staging directory that is renamed into
place as one atomic move, or leaves a leftover that the next install clears
before it starts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from bianbu_agent.mcp.managed import npm_package_name


#: npm resolves over the network; this bound exists to stop a wedged install,
#: not to cap a healthy one.
NPM_INSTALL_TIMEOUT_SECONDS = 900.0
_STDERR_DETAIL_CHARS = 400


@dataclass(frozen=True, slots=True)
class VendoredNpmServer:
    package: str
    root: Path
    script: Path
    interpreter: Path

    @property
    def command_line(self) -> str:
        return f"{self.interpreter} {self.script}"


class NpmUnavailableError(RuntimeError):
    """npm or node is missing, so an npm-backed MCP cannot be prepared."""


def vendor_npm_server(spec: str, target: Path) -> VendoredNpmServer:
    """Install ``spec`` into ``target`` and return how to launch it.

    The install lands in a sibling directory and is renamed into place only
    after npm succeeds, so an interrupted install can never leave a partially
    extracted tree that a later start would trust.
    """

    npm = shutil.which("npm")
    if npm is None:
        raise NpmUnavailableError(
            f"npm is required to install MCP server package {spec!r}"
        )
    node = shutil.which("node")
    if node is None:
        raise NpmUnavailableError(
            f"node is required to run MCP server package {spec!r}"
        )
    package = npm_package_name(spec)
    if not package:
        raise ValueError(f"unsupported npm package spec: {spec!r}")

    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    discard_stale_staging(destination)
    staging = destination.parent / f".{destination.name}.install-{uuid.uuid4().hex}"
    try:
        _run_npm_install(npm, spec, staging)
        script = _resolve_executable(staging / "node_modules" / package, package)
        _publish(staging, destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return VendoredNpmServer(
        package=package,
        root=destination,
        script=destination / script.relative_to(staging),
        interpreter=Path(node),
    )


def vendored_npm_server(spec: str, target: Path) -> VendoredNpmServer | None:
    """The installation already present at ``target``, when it is complete.

    A published tree is renamed into place only after npm succeeded, so the
    destination existing is the proof that the install finished.  Reusing it
    keeps a Runtime start from reinstalling a dependency tree that has not
    changed: the npm work is the reason preparing an MCP server is slow, and
    it only has to happen once.
    """

    package = npm_package_name(spec)
    if not package:
        return None
    destination = Path(target)
    node = shutil.which("node")
    if node is None or not destination.is_dir():
        return None
    try:
        script = _resolve_executable(
            destination / "node_modules" / package,
            package,
        )
    except (OSError, RuntimeError):
        return None
    return VendoredNpmServer(
        package=package,
        root=destination,
        script=script,
        interpreter=Path(node),
    )


def discard_stale_staging(target: Path) -> None:
    """Remove what a killed install left next to ``target``.

    npm runs in a ``.<name>.install-<id>`` sibling and the old tree is retired
    as ``.<name>.retired-<id>``.  Both are renamed into place or dropped before
    the install returns, so anything still named that way belongs to an install
    that was interrupted.  The K3 board had a killed install leave an empty
    staging directory behind; from then on every later install looked like it
    had a tree to fall back on.
    """

    destination = Path(target)
    for pattern in (
        f".{destination.name}.install-*",
        f".{destination.name}.retired-*",
    ):
        for leftover in destination.parent.glob(pattern):
            shutil.rmtree(leftover, ignore_errors=True)


def remove_vendored_server(target: Path) -> bool:
    """Drop a vendored installation; missing directories are not an error."""

    destination = Path(target)
    discard_stale_staging(destination)
    if not destination.exists():
        return False
    shutil.rmtree(destination)
    return True


def _run_npm_install(npm: str, spec: str, staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
    environment.pop("NODE_OPTIONS", None)
    command = [
        npm,
        "install",
        "--prefix",
        str(staging),
        "--no-audit",
        "--no-fund",
        "--omit=dev",
        spec,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=staging,
            env=environment,
            capture_output=True,
            text=True,
            timeout=NPM_INSTALL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"npm install {spec!r} timed out after "
            f"{NPM_INSTALL_TIMEOUT_SECONDS:.0f}s"
        ) from exc
    except OSError as exc:
        raise NpmUnavailableError(f"could not run npm: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"npm install {spec!r} failed (exit {completed.returncode}): "
            f"{detail[-_STDERR_DETAIL_CHARS:]}"
        )


def _resolve_executable(package_root: Path, package: str) -> Path:
    """The script npm would run for ``npx <package>``.

    Mirrors npm's own rule: a string ``bin`` is the package's single
    executable, and an object ``bin`` is selected by the package's base name.
    """

    manifest_path = package_root / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"npm install produced no package directory for {package!r}"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid package.json in {package!r}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError(f"invalid package.json in {package!r}: not an object")

    entries = manifest.get("bin")
    if isinstance(entries, str):
        relative = entries
    elif isinstance(entries, dict) and entries:
        base = package.rpartition("/")[2]
        if base in entries:
            relative = entries[base]
        elif len(entries) == 1:
            relative = next(iter(entries.values()))
        else:
            names = ", ".join(sorted(str(key) for key in entries))
            raise RuntimeError(
                f"package {package!r} declares several executables ({names}); "
                "declare the one to run with an explicit command instead"
            )
    else:
        raise RuntimeError(f"package {package!r} declares no executable")
    if not isinstance(relative, str) or not relative.strip():
        raise RuntimeError(f"package {package!r} declares an invalid executable")

    resolved = (package_root / relative).resolve()
    if not resolved.is_relative_to(package_root.resolve()):
        raise RuntimeError(f"package {package!r} declares an executable outside it")
    if not resolved.is_file():
        raise RuntimeError(f"package {package!r} executable {relative!r} is missing")
    return resolved


def _publish(staging: Path, destination: Path) -> None:
    retired = destination.parent / f".{destination.name}.retired-{uuid.uuid4().hex}"
    if destination.exists():
        os.replace(destination, retired)
    try:
        os.replace(staging, destination)
    except OSError:
        if retired.exists():
            os.replace(retired, destination)
        raise
    finally:
        shutil.rmtree(retired, ignore_errors=True)


__all__ = [
    "NPM_INSTALL_TIMEOUT_SECONDS",
    "NpmUnavailableError",
    "VendoredNpmServer",
    "discard_stale_staging",
    "remove_vendored_server",
    "vendor_npm_server",
    "vendored_npm_server",
]
