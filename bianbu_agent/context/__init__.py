"""Context budgeting and checkpoint safety boundaries."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING


if TYPE_CHECKING:  # pragma: no cover - typing only
    from bianbu_agent.context.compression import (
        CompactionPlan,
        CompactionResult,
        ContextCompactor,
        compression_metadata,
        deterministic_summary,
    )
    from bianbu_agent.context.config import (
        CompressionSettings,
        CompressionSettingsProvider,
    )
    from bianbu_agent.context.tool_results import compact_tool_result


__all__ = [
    "CompactionPlan",
    "CompactionResult",
    "CompressionSettings",
    "CompressionSettingsProvider",
    "ContextCompactor",
    "compact_tool_result",
    "compression_metadata",
    "deterministic_summary",
]

# The names above are re-exported lazily: `compression` pulls in the execution
# stack (langchain/langgraph), and the file-management CLI must be able to list
# resources without loading it. Reading an attribute still resolves the same
# object, so `from bianbu_agent.context import ContextCompactor` keeps working.
_EXPORTS = {
    "CompactionPlan": "bianbu_agent.context.compression",
    "CompactionResult": "bianbu_agent.context.compression",
    "ContextCompactor": "bianbu_agent.context.compression",
    "compression_metadata": "bianbu_agent.context.compression",
    "deterministic_summary": "bianbu_agent.context.compression",
    "CompressionSettings": "bianbu_agent.context.config",
    "CompressionSettingsProvider": "bianbu_agent.context.config",
    "compact_tool_result": "bianbu_agent.context.tool_results",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
