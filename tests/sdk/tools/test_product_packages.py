from pathlib import Path

import pytest

from bianbu_agent.tools.builtins import load_builtin, builtin_source

from bianbu_agent.tools import ToolLoadContext

PRODUCT_TOOLS = (
    "kanban_heartbeat",
    "process",
    "terminal",
    "web_extract",
    "web_search",
)


class _Runtime:
    def __init__(self, name: str) -> None:
        self.name = name
        self.interaction = self
        self.console_state = self
        self.agents = self
        self.processes = self
        self.sessions = self
        self.scripts = self
        self.workspace = self

    async def dispatch_upstream(self, arguments):
        return f"{self.name}:ok"

    async def ask(self, arguments):
        return f"{self.name}:ok"

    async def manage(self, arguments):
        return f"{self.name}:ok"

    async def run(self, arguments):
        return f"{self.name}:ok"

    async def start(self, arguments):
        return f"{self.name}:ok"

    async def decorate(self, arguments, result):
        return result

    def search(self, session_id, arguments):
        return f"{self.name}:ok"

    async def execute(self, arguments):
        return f"{self.name}:ok"

    async def restore(self, arguments):
        return f"{self.name}:ok"


class _Factory:
    async def for_call(self, package, call_context):
        return _Runtime(package.name)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", PRODUCT_TOOLS)
async def test_product_package_loads_without_legacy_or_central_executor(
    name: str,
    tmp_path: Path,
) -> None:
    source = builtin_source(name)
    contribution = load_builtin(
        name,
        context=ToolLoadContext(workspace_root=tmp_path / "workspace"),
    )

    assert callable(contribution.handler)
    text = source.read_text(encoding="utf-8")
    assert "bianbu_agent.tools.executors" not in text
    assert "create_file_backed_tool" not in text
