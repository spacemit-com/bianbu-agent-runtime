from pathlib import Path
from textwrap import dedent

import pytest

from bianbu_agent import ToolCallContext
from bianbu_agent.config import RUNTIME_VERSION
from bianbu_agent.tools import DirectoryToolLoader, ToolLoadContext


def _write_tool(
    root: Path,
    *,
    result: str = "ONE",
    function_name: str = "echo_value",
    async_tool: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    keyword = "async " if async_tool else ""
    (root / "tool.py").write_text(
        dedent(
            f'''\
            from bianbu_agent.tools import tool

            def create_tool(context):
                @tool
                {keyword}def {function_name}(value: str) -> str:
                    """Echo one value."""
                    return "{result}:" + value
                return {function_name}
            '''
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_tool", [False, True])
async def test_loader_derives_and_executes_a_decorated_tool(
    tmp_path: Path,
    async_tool: bool,
) -> None:
    root = tmp_path / "tools" / "echo_value"
    _write_tool(root, async_tool=async_tool)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    contribution = DirectoryToolLoader().load(
        root,
        context=ToolLoadContext(workspace_root=workspace),
    )
    result = await contribution.handler(
        ToolCallContext("run-1", "session-1", "agent-1"),
        {"value": "hello"},
    )

    assert result.content == "ONE:hello"
    assert contribution.spec.name == "echo_value"
    assert contribution.spec.description == "Echo one value."
    assert contribution.spec.input_schema["type"] == "object"
    assert contribution.spec.input_schema["properties"]["value"]["type"] == "string"
    assert contribution.spec.input_schema["required"] == ["value"]
    assert contribution.spec.metadata["source_path"] == str(root / "tool.py")
    assert contribution.spec.metadata["version"] == RUNTIME_VERSION
    assert "risk_level" not in contribution.spec.metadata
    assert "permissions" not in contribution.spec.metadata


@pytest.mark.asyncio
async def test_directory_tool_reads_account_and_agent_from_call_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tools" / "echo_value"
    _write_tool(root, async_tool=True)
    source = root / "tool.py"
    source.write_text(
        source.read_text(encoding="utf-8").replace(
            'return "ONE:" + value',
            'return context.call.account_id + ":" + context.call.agent_id + ":" + value',
        ),
        encoding="utf-8",
    )
    contribution = DirectoryToolLoader().load(
        root,
        context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
    )

    result = await contribution.handler(
        ToolCallContext(
            "run", "session", "writer", account_id="system-user-42"
        ),
        {"value": "hello"},
    )

    assert result.content == "system-user-42:writer:hello"


@pytest.mark.asyncio
async def test_loader_uses_content_hash_modules_for_safe_reload(tmp_path: Path) -> None:
    root = tmp_path / "tools" / "echo_value"
    _write_tool(root, result="OLD")
    context = ToolLoadContext(workspace_root=tmp_path / "workspace")
    loader = DirectoryToolLoader()
    old = loader.load(root, context=context)

    _write_tool(root, result="NEW")
    new = loader.load(root, context=context)
    call = ToolCallContext("run", "session", "agent")

    assert (await old.handler(call, {"value": "x"})).content == "OLD:x"
    assert (await new.handler(call, {"value": "x"})).content == "NEW:x"
    assert old.spec.metadata["content_digest"] != new.spec.metadata["content_digest"]
    assert old.spec.metadata["module_name"] != new.spec.metadata["module_name"]


@pytest.mark.asyncio
async def test_loader_supports_relative_imports(tmp_path: Path) -> None:
    root = tmp_path / "tools" / "echo_value"
    _write_tool(root)
    (root / "helper.py").write_text(
        "def decorate(value):\n    return 'REL:' + value\n",
        encoding="utf-8",
    )
    tool_path = root / "tool.py"
    tool_path.write_text(
        tool_path.read_text(encoding="utf-8")
        .replace(
            "from bianbu_agent.tools import tool",
            "from bianbu_agent.tools import tool\nfrom .helper import decorate",
        )
        .replace('return "ONE:" + value', "return decorate(value)"),
        encoding="utf-8",
    )

    contribution = DirectoryToolLoader().load(
        root,
        context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
    )
    result = await contribution.handler(
        ToolCallContext("run", "session", "agent"), {"value": "ok"}
    )

    assert result.content == "REL:ok"


@pytest.mark.asyncio
async def test_identical_tools_in_different_roots_keep_relative_imports_isolated(
    tmp_path: Path,
) -> None:
    loader = DirectoryToolLoader()
    context = ToolLoadContext(workspace_root=tmp_path / "workspace")
    contributions = []
    roots = [tmp_path / owner / "echo_value" for owner in ("one", "two")]
    for root in roots:
        _write_tool(root)
        source = root / "tool.py"
        source.write_text(source.read_text().replace(
            'return "ONE:" + value',
            "from .helper import decorate\n        return decorate(value)",
        ))
        (root / "helper.py").write_text(
            "from pathlib import Path\n"
            "def decorate(value):\n    return str(Path(__file__).parent) + ':' + value\n"
        )
        contributions.append(loader.load(root, context=context))

    call = ToolCallContext("run", "session", "agent")
    for root, contribution in zip(roots, contributions, strict=True):
        assert (await contribution.handler(call, {"value": "hello"})).content == f"{root}:hello"


@pytest.mark.asyncio
async def test_reloading_unchanged_tool_in_same_root_preserves_module_state(tmp_path: Path) -> None:
    root = tmp_path / "echo_value"
    _write_tool(root)
    source = root / "tool.py"
    source.write_text("CALLS = 0\n" + source.read_text().replace(
        'return "ONE:" + value',
        "global CALLS\n        CALLS += 1\n        return str(CALLS) + ':' + value",
    ))
    loader = DirectoryToolLoader()
    context = ToolLoadContext(workspace_root=tmp_path / "workspace")
    first = loader.load(root, context=context)
    second = loader.load(root, context=context)
    call = ToolCallContext("run", "session", "agent")

    assert (await first.handler(call, {"value": "a"})).content == "1:a"
    assert (await second.handler(call, {"value": "b"})).content == "2:b"


def test_inspection_uses_the_fixed_source_and_runtime_version(tmp_path: Path) -> None:
    root = tmp_path / "echo_value"
    _write_tool(root)

    inspection = DirectoryToolLoader.inspect(root)

    assert inspection.name == "echo_value"
    assert inspection.version == RUNTIME_VERSION
    assert inspection.source_path == (root / "tool.py").resolve()
    assert len(inspection.content_digest) == 64


def test_loader_validates_declared_dependencies_before_import(tmp_path: Path) -> None:
    root = tmp_path / "echo_value"
    _write_tool(root)
    (root / "requirements.txt").write_text(
        "definitely-missing-bianbu-package>=1\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="not installed"):
        DirectoryToolLoader().load(
            root,
            context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
        )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "create_tool"),
        ("create_tool = 1\n", "create_tool"),
        ("def create_tool(context):\n    return lambda: None\n", "BaseTool"),
    ],
)
def test_loader_rejects_invalid_factories(
    tmp_path: Path,
    source: str,
    message: str,
) -> None:
    root = tmp_path / "invalid_tool"
    root.mkdir()
    (root / "tool.py").write_text(source, encoding="utf-8")

    with pytest.raises((TypeError, ValueError), match=message):
        DirectoryToolLoader().load(
            root,
            context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
        )


def test_loader_rejects_directory_and_decorated_name_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "wrong_directory"
    _write_tool(root, function_name="echo_value")

    with pytest.raises(ValueError, match="directory name"):
        DirectoryToolLoader().load(
            root,
            context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
        )


def test_loader_requires_a_direct_regular_tool_file(tmp_path: Path) -> None:
    root = tmp_path / "missing"
    (root / "package").mkdir(parents=True)
    (root / "package" / "tool.py").write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="tool.py is required"):
        DirectoryToolLoader.inspect(root)

    outside = tmp_path / "outside.py"
    outside.write_text("", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / "tool.py").symlink_to(outside)
    with pytest.raises(ValueError, match="regular file"):
        DirectoryToolLoader.inspect(linked)
