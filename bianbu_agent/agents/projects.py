"""Agent project scaffolding, validation, and deterministic packaging."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import stat
import shutil
import tempfile
import zipfile

from bianbu_agent._internal.filesystem import digest_directory
from bianbu_agent.agents.contracts import AgentSpec
from bianbu_agent.agents.manifest import AgentManifest
from bianbu_agent.agents.bundled_tools import load_bundled_tools
from bianbu_agent.agents.packages import (
    MAX_AGENT_PACKAGE_FILE_BYTES,
    MAX_AGENT_PACKAGE_MEMBERS,
    MAX_AGENT_PACKAGE_PATH_BYTES,
    MAX_AGENT_PACKAGE_TOTAL_BYTES,
    AgentPackage,
    AgentPackageManager,
    register_project_path,
)


_IGNORED_ROOTS = frozenset({"dist", ".bianbu-agent-dev", ".bianbu-agent-data", ".git"})
_PROJECT_ONLY_FILES = frozenset({"runtime.yaml", "README.md", ".gitignore"})
_DEFAULT_PORT = 9899


@dataclass(frozen=True, slots=True)
class AgentProject:
    root: Path
    manifest: AgentManifest
    files: tuple[Path, ...]
    content_digest: str


@dataclass(frozen=True, slots=True)
class AgentArtifact:
    path: Path
    agent_id: str
    version: str
    content_digest: str


def init_agent_project(
    path: str | Path,
    *,
    agent_id: str,
    name: str,
    name_zh: str = "",
    port: int = _DEFAULT_PORT,
) -> AgentProject:
    """Create a minimal, self-contained Agent project without overwriting files."""

    root = Path(path).expanduser().resolve()
    manifest = AgentManifest(
        schema_version=2,
        version="0.1.0",
        spec=AgentSpec(
            id=agent_id,
            name=_required_text(name, "name"),
            name_zh=name_zh.strip(),
            description="",
            soul="",
            tools=(f"{agent_id}/greet",),
        ),
    )
    files = {
        "agent.yaml": manifest.to_yaml(),
        "runtime.yaml": _runtime_template(manifest.spec.id, port),
        "README.md": _readme_template(manifest.spec.id),
        ".gitignore": "dist/\n.bianbu-agent-dev/\n.bianbu-agent-data/\n",
        "tools/greet/tool.py": _greeting_tool_template(),
    }
    if root.exists():
        if not root.is_dir():
            raise FileExistsError(f"Agent project path already exists: {root}")
        if any(root.iterdir()):
            raise FileExistsError(f"Agent project directory is not empty: {root}")
    else:
        root.mkdir(parents=True)
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(content)
        except FileExistsError as exc:
            raise FileExistsError(f"Agent project file already exists: {target}") from exc
    return check_agent_project(root)


def check_agent_project(path: str | Path) -> AgentProject:
    """Validate one project and return the exact files included in its package."""

    root = Path(path).expanduser().resolve()
    manifest = AgentManifest.from_path(root / "agent.yaml")
    selected = _project_files(root)
    if root / "agent.yaml" not in selected:
        raise ValueError("Agent project package is missing agent.yaml")
    load_bundled_tools(root, manifest, workspace_root=root / ".bianbu-agent-data" / "workspace")
    return AgentProject(
        root=root,
        manifest=manifest,
        files=selected,
        content_digest=digest_directory(root, selected),
    )


def build_agent_project(
    path: str | Path,
    *,
    output: str | Path | None = None,
) -> AgentArtifact:
    """Build one deterministic `.agent` ZIP without replacing existing output."""

    project = check_agent_project(path)
    artifact_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else project.root
        / "dist"
        / f"{project.manifest.spec.id}-{project.manifest.version}.agent"
    )
    if artifact_path.suffix != ".agent":
        raise ValueError("Agent package output must use the .agent suffix")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if artifact_path.exists() or artifact_path.is_symlink():
        raise FileExistsError(f"Agent package already exists: {artifact_path}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=artifact_path.parent,
        prefix=f".{artifact_path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for source in project.files:
                relative = source.relative_to(project.root).as_posix()
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100644 << 16)
                with source.open("rb") as source_stream, archive.open(
                    info, "w", force_zip64=True
                ) as target_stream:
                    shutil.copyfileobj(source_stream, target_stream)
        try:
            os.link(temporary, artifact_path)
        except FileExistsError as exc:
            raise FileExistsError(f"Agent package already exists: {artifact_path}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return AgentArtifact(
        path=artifact_path,
        agent_id=project.manifest.spec.id,
        version=project.manifest.version,
        content_digest=project.content_digest,
    )


def sync_agent_project(
    path: str | Path,
    manager: AgentPackageManager,
) -> AgentPackage:
    """Publish current project contents for development, regardless of version bump."""

    project = check_agent_project(path)
    return manager.sync(project.root, files=project.files)


def _required_text(value: str, field: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"Agent project {field} must not be empty")
    return normalized


def _project_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    namespace: dict[str, str] = {}
    total_size = 0
    total_path_bytes = 0

    def visit(directory: Path, relative_root: Path) -> None:
        nonlocal total_size, total_path_bytes
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            raise ValueError(f"cannot inspect Agent project directory: {directory}") from exc
        for entry in entries:
            relative = relative_root / entry.name
            if not relative_root.parts and entry.name in _IGNORED_ROOTS:
                continue
            if entry.name == "__pycache__" and entry.is_dir(follow_symlinks=False):
                continue
            relative_name = relative.as_posix()
            if relative_name in _PROJECT_ONLY_FILES:
                continue
            if entry.is_symlink():
                raise ValueError(f"symbolic links are not allowed: {relative_name}")
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"cannot inspect Agent project entry: {relative_name}") from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise ValueError(f"unsupported Agent project entry: {relative_name}")
            register_project_path(namespace, relative.parts)
            total_size += entry_stat.st_size
            total_path_bytes += len(relative_name.encode("utf-8"))
            if entry_stat.st_size > MAX_AGENT_PACKAGE_FILE_BYTES:
                raise ValueError(f"Agent project file is too large: {relative_name}")
            if total_size > MAX_AGENT_PACKAGE_TOTAL_BYTES:
                raise ValueError("Agent project package is too large")
            if len(files) >= MAX_AGENT_PACKAGE_MEMBERS:
                raise ValueError("Agent project package has too many files")
            if total_path_bytes > MAX_AGENT_PACKAGE_PATH_BYTES:
                raise ValueError("Agent project path metadata is too large")
            files.append(root / relative)

    if not root.is_dir():
        raise ValueError(f"Agent project root is not a directory: {root}")
    visit(root, Path())
    return tuple(sorted(files, key=lambda path: path.relative_to(root).as_posix()))


def suggested_agent_id(path: str | Path) -> str:
    return f"com.bianbu.{secrets.token_hex(6)}"


def _greeting_tool_template() -> str:
    return '''from bianbu_agent.tools import tool


def create_tool(context):
    @tool
    def greet(name: str) -> str:
        """Return a friendly greeting for the given name."""
        return f"Hello, {name}!"

    return greet
'''


def _runtime_template(agent_id: str, port: int) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("development Runtime port must be between 1 and 65535")
    return f"""schema_version: 1
data_dir: ./.bianbu-agent-data

service:
  title: Bianbu Agent Development Runtime
  host: 127.0.0.1
  port: {port}

language: zh
require_file_backed_capabilities: true
allow_unmanaged_capabilities: true

runtime:
  control:
    execution:
      requested_mode: host
  worker:
    execution:
      requested_mode: host

security:
  required: false
"""


def _readme_template(agent_id: str) -> str:
    return f"""# {agent_id}

This directory is a Bianbu Agent project. `agent.version` is the version of
this Agent's content; update it when publishing changed content.

## Model/provider setup

Before building, configure a provider in the project Runtime's YAMLModelStore:

```bash
bianbu-agent --config runtime.yaml model bootstrap-chat
```

Enter your provider ID, OpenAI-compatible Base URL and API key at the prompts.
The key is stored privately under `.bianbu-agent-data/models/`, outside the
Agent package. Select that provider and its remote model in `agent.yaml`:

```yaml
agent:
  # Keep the generated id, version, name and tools fields.
  model:
    provider: your-provider-id
    name: your-remote-model-name
```

`tools/greet/tool.py` implements the existing `create_tool(context)` factory
using `from bianbu_agent.tools import tool`. Its qualified selection is
`{agent_id}/greet`; the installed Agent owns its code and capability.

```bash
bianbu-agent check
bianbu-agent build
bianbu-agent install dist/{agent_id}-0.1.0.agent --config runtime.yaml
bianbu-agent run
bianbu-agent status
bianbu-agent logs --follow
bianbu-agent uninstall {agent_id}
```

`run` starts a project-local Runtime using the installed package. If no package
is installed, it publishes the checked project first. To use source edits after
installation, bump `agent.version`, build a new artifact, and install it with
`--upgrade` while the Runtime is stopped (`bianbu-agent --config runtime.yaml stop`).

Open the printed Runtime URL, select this Agent, create a Session and ask it to
greet a name using its Tool. Query correlated events with
`bianbu-agent logs --agent-id {agent_id} --session-id SESSION_ID --run-id RUN_ID`.
Uninstall removes the package and its Tools; Sessions, checkpoints and logs remain.
"""


__all__ = [
    "AgentArtifact",
    "AgentProject",
    "build_agent_project",
    "check_agent_project",
    "init_agent_project",
    "suggested_agent_id",
    "sync_agent_project",
]
