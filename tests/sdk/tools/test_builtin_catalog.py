from pathlib import Path

import pytest

from bianbu_agent import ToolCallContext
from bianbu_agent.tools.catalog import ToolDirectoryCatalog


@pytest.mark.asyncio
async def test_builtins_load_from_code_without_a_user_directory(tmp_path: Path):
    catalog = ToolDirectoryCatalog(include_builtins=True).build(
        workspace_root=tmp_path / 'workspace')
    from types import SimpleNamespace
    from bianbu_agent.server.runtime import _validate_file_backed
    _validate_file_backed(SimpleNamespace(tools=catalog.tools, skills={}))
    assert len(catalog.tools) == 34
    assert not (tmp_path / 'tools').exists()
    (tmp_path / 'workspace' / 'hello.txt').write_text('hello builtin')
    result = await catalog.tools['read_file'].handler(
        ToolCallContext('run', 'session', 'agent'), {'path': 'hello.txt'})
    assert 'hello builtin' in result.content
    assert catalog.tools['read_file'].spec.metadata['builtin'] is True
    assert '/bianbu_agent/tools/files/' in catalog.tools['read_file'].spec.metadata['source_path']
