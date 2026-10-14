"""Explicit code-owned tools. Importing this registry never executes tool factories."""
from __future__ import annotations

import ast
import hashlib
from dataclasses import replace
from importlib import import_module
from pathlib import Path
from types import MappingProxyType

from bianbu_agent.config import RUNTIME_VERSION
from bianbu_agent.tools.loader import DirectoryToolLoader
from bianbu_agent.tools.models import ToolLoadContext, ToolSourceInspection

# Public tool IDs stay stable; module paths describe their responsibility.
BUILTIN_MODULES = MappingProxyType({
    'browser_back': "bianbu_agent.tools.browser.browser_back",
    'browser_click': "bianbu_agent.tools.browser.browser_click",
    'browser_console': "bianbu_agent.tools.browser.browser_console",
    'browser_get_images': "bianbu_agent.tools.browser.browser_get_images",
    'browser_navigate': "bianbu_agent.tools.browser.browser_navigate",
    'browser_press': "bianbu_agent.tools.browser.browser_press",
    'browser_scroll': "bianbu_agent.tools.browser.browser_scroll",
    'browser_snapshot': "bianbu_agent.tools.browser.browser_snapshot",
    'browser_type': "bianbu_agent.tools.browser.browser_type",
    'kanban_attach': "bianbu_agent.tools.kanban.kanban_attach",
    'kanban_attach_url': "bianbu_agent.tools.kanban.kanban_attach_url",
    'kanban_attachments': "bianbu_agent.tools.kanban.kanban_attachments",
    'kanban_block': "bianbu_agent.tools.kanban.kanban_block",
    'kanban_comment': "bianbu_agent.tools.kanban.kanban_comment",
    'kanban_complete': "bianbu_agent.tools.kanban.kanban_complete",
    'kanban_create': "bianbu_agent.tools.kanban.kanban_create",
    'kanban_heartbeat': "bianbu_agent.tools.kanban.kanban_heartbeat",
    'kanban_link': "bianbu_agent.tools.kanban.kanban_link",
    'kanban_list': "bianbu_agent.tools.kanban.kanban_list",
    'kanban_show': "bianbu_agent.tools.kanban.kanban_show",
    'kanban_unblock': "bianbu_agent.tools.kanban.kanban_unblock",
    'patch': "bianbu_agent.tools.files.patch",
    'process': "bianbu_agent.tools.processes.process",
    'read_image': "bianbu_agent.tools.vision.read_image",
    'read_file': "bianbu_agent.tools.files.read_file",
    'search_files': "bianbu_agent.tools.files.search_files",
    'skill_manage': "bianbu_agent.tools.skills.skill_manage",
    'skill_view': "bianbu_agent.tools.skills.skill_view",
    'skills_list': "bianbu_agent.tools.skills.skills_list",
    'terminal': "bianbu_agent.tools.processes.terminal",
    'todo': "bianbu_agent.tools.kanban.todo",
    'web_extract': "bianbu_agent.tools.web.web_extract",
    'web_search': "bianbu_agent.tools.web.web_search",
    'write_file': "bianbu_agent.tools.files.write_file",
})


def builtin_source(name: str) -> Path:
    return Path(__file__).parent / Path(*BUILTIN_MODULES[name].split('.')[2:]).with_suffix('.py')


def inspect_builtin(name: str) -> ToolSourceInspection:
    source = builtin_source(name)
    # Include helpers in this responsibility group in the pinned digest.
    digest = hashlib.sha256()
    for path in sorted(source.parent.glob('*.py')):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return ToolSourceInspection(name, RUNTIME_VERSION, source.parent, source, digest.hexdigest())


def builtin_description(name: str) -> str:
    tree = ast.parse(builtin_source(name).read_text(encoding='utf-8'))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_docstring(node) or ''
    return ''


def load_builtin(name: str, *, context: ToolLoadContext):
    inspection = inspect_builtin(name)
    contribution = DirectoryToolLoader(catalog_plugin_id='bianbu.catalog.builtin').contribution_from_module(
        import_module(BUILTIN_MODULES[name]), inspection, context=context,
        data_root=Path(context.services.get('home_root', context.workspace_root.parent)) / 'runtime' / 'tool-data' / name,
    )
    return replace(contribution, spec=replace(contribution.spec,
        metadata={**contribution.spec.metadata, 'builtin': True}))
