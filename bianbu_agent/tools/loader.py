from __future__ import annotations

import ast
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path


from bianbu_agent._internal.filesystem import digest_directory, regular_files
from bianbu_agent._internal.dependencies import validate_python_requirements
from bianbu_agent._internal.importing import load_package_module
from bianbu_agent.config import RUNTIME_VERSION
from bianbu_agent.inventory.models import CapabilityOrigin
from bianbu_agent.plugins import ToolContribution, ToolResult, ToolSpec
from bianbu_agent.tools.models import (
    ToolLoadContext,
    ToolPackageContext,
    ToolSourceInspection,
)


_CATALOG_PLUGIN_ID = "bianbu.catalog.filesystem"
_SOURCE_NAME = "tool.py"
_FACTORY_NAME = "create_tool"


class DirectoryToolLoader:
    def __init__(self, *, catalog_plugin_id: str = _CATALOG_PLUGIN_ID) -> None:
        self._catalog_plugin_id = str(catalog_plugin_id).strip()

    def load(
        self,
        root: Path,
        *,
        context: ToolLoadContext,
        plugin_id: str | None = None,
        owner_agent_id: str | None = None,
        data_root: Path | None = None,
    ) -> ToolContribution:
        inspection = self.inspect(root)
        requirements = validate_python_requirements(inspection.root)
        # Relative imports resolve through the cached package's __path__.
        # Staging and published roots must never share that module namespace.
        root_digest = hashlib.sha256(os.fsencode(inspection.root)).hexdigest()
        module = load_package_module(
            inspection.root,
            "tool",
            namespace=(
                f"_bianbu_agent_tool_{inspection.name}_{root_digest}_{inspection.content_digest}"
            ),
        )
        return self.contribution_from_module(
            module, inspection, context=context, plugin_id=plugin_id,
            owner_agent_id=owner_agent_id, data_root=data_root,
            requirements=requirements,
        )

    def contribution_from_module(
        self, module, inspection: ToolSourceInspection, *,
        context: ToolLoadContext, plugin_id: str | None = None,
        owner_agent_id: str | None = None, data_root: Path | None = None,
        requirements: tuple[str, ...] = (),
    ) -> ToolContribution:
        """Adapt an imported factory; shared by builtins and external packages."""
        from langchain_core.tools import BaseTool

        factory = getattr(module, _FACTORY_NAME, None)
        if not callable(factory):
            raise TypeError("Tool entrypoint create_tool is not callable")
        owner_plugin_id = (
            str(plugin_id).strip() if plugin_id is not None else self._catalog_plugin_id
        )
        qualified_name = (
            f"{owner_agent_id or owner_plugin_id}/{inspection.name}"
            if plugin_id is not None or owner_agent_id is not None
            else inspection.name
        )
        resolved_data_root = (
            Path(data_root if data_root is not None else inspection.root / "data")
            .expanduser()
            .resolve()
        )
        runtime_factory = context.services.get("runtime_factory")
        resolved_state_root = Path(
            getattr(
                runtime_factory,
                "state_root",
                context.workspace_root.parent / "runtime" / "tool-state",
            )
        ).resolve()
        package = ToolPackageContext(
            name=inspection.name,
            home_root=Path(context.services.get("home_root", context.workspace_root.parent)).resolve(),
            package_root=inspection.root,
            source_digest=inspection.content_digest,
            workspace_root=context.workspace_root,
            data_root=resolved_data_root,
            qualified_name=qualified_name,
            config=context.config,
            state_root=resolved_state_root,
        )
        base_tool = factory(package)
        if not isinstance(base_tool, BaseTool):
            raise TypeError("Tool factory create_tool must return BaseTool")
        if base_tool.name != inspection.name:
            raise ValueError(
                f"Tool directory name {inspection.name!r} does not match "
                f"decorated Tool name {base_tool.name!r}"
            )
        schema = base_tool.get_input_jsonschema()
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ValueError("decorated Tool must expose an object input schema")

        unavailable_reason = ""
        availability = getattr(module, "unavailable_reason", None)
        if callable(availability):
            unavailable_reason = str(availability(package) or "").strip()

        async def handler(call_context, arguments):
            with package.bind_call(call_context):
                result = await base_tool.ainvoke(dict(arguments))
            if isinstance(result, ToolResult):
                return result
            if isinstance(result, str):
                content = result
            else:
                content = json.dumps(result, ensure_ascii=False)
            return ToolResult(content=content)

        metadata: dict[str, object] = {
            "source_path": str(inspection.source_path),
            "version": inspection.version,
            "content_digest": inspection.content_digest,
            "module_name": module.__name__,
            "manifest_path": str(inspection.source_path),
            "origin": (
                CapabilityOrigin.PLUGIN.value
                if plugin_id is not None
                else CapabilityOrigin.STANDALONE.value
            ),
            "plugin_id": plugin_id,
            "data_root": str(resolved_data_root),
            "requirements": requirements,
        }
        if unavailable_reason:
            metadata["unavailable_reason"] = unavailable_reason
            metadata["unavailable_code"] = "tool_requirement_unavailable"
        return ToolContribution(
            plugin_id=owner_plugin_id,
            spec=ToolSpec(
                name=base_tool.name,
                description=base_tool.description,
                input_schema=dict(schema),
                metadata=metadata,
            ),
            handler=handler,
        )

    @staticmethod
    def inspect(root: Path) -> ToolSourceInspection:
        resolved_root = Path(root).expanduser().resolve()
        if not resolved_root.is_dir():
            raise ValueError(f"Tool root is not a directory: {resolved_root}")
        source_path = resolved_root / _SOURCE_NAME
        if source_path.is_symlink():
            raise ValueError(f"Tool source must be a regular file: {source_path}")
        if not source_path.is_file():
            raise ValueError(f"tool.py is required: {source_path}")
        files = tuple(
            path
            for path in regular_files(resolved_root)
            if "__pycache__" not in path.relative_to(resolved_root).parts
            and path.suffix != ".pyc"
        )
        if any(
            path.relative_to(resolved_root).as_posix() == "requirements.txt"
            for path in files
        ):
            validate_python_requirements(resolved_root)
        DirectoryToolLoader._reject_forbidden_imports(resolved_root, files)
        return ToolSourceInspection(
            name=resolved_root.name,
            version=RUNTIME_VERSION,
            root=resolved_root,
            source_path=source_path,
            content_digest=digest_directory(resolved_root, files),
        )

    @staticmethod
    def _reject_forbidden_imports(root: Path, files: tuple[Path, ...]) -> None:
        forbidden_roots = {
            "agent",
            "agent_core",
            "cron",
            "plugins",
            "providers",
            "tools",
        }

        def is_forbidden(module: str) -> bool:
            root_name = module.partition(".")[0]
            return (
                root_name == "bianbu_agent"
                or root_name in forbidden_roots
                or root_name.startswith("hermes_")
                or module == "langchain_core.tools"
                or module.startswith("langchain_core.tools.")
            )

        for path in files:
            if path.suffix != ".py":
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, UnicodeError, SyntaxError) as exc:
                raise ValueError(f"invalid Tool Python source {path}: {exc}") from exc
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "runtime"
                ):
                    relative = path.relative_to(root)
                    raise ValueError(
                        f"forbidden Tool runtime access in {relative}: context.runtime()"
                    )
                imported: tuple[str, ...] = ()
                if isinstance(node, ast.Import):
                    imported = tuple(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        continue
                    if (
                        node.module == "bianbu_agent.tools"
                        and node.names
                        and all(
                            alias.name in {"mutate_kanban", "tool", "get_task_skills"}
                            for alias in node.names
                        )
                    ):
                        continue
                    if node.module:
                        imported = (node.module,)
                elif (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in {"import_module", "__import__"}
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    imported = (node.args[0].value,)
                if any(is_forbidden(name) for name in imported):
                    relative = path.relative_to(root)
                    dependency = next(name for name in imported if is_forbidden(name))
                    raise ValueError(
                        f"forbidden Tool dependency {dependency!r} in {relative}"
                    )


__all__ = ["DirectoryToolLoader"]
