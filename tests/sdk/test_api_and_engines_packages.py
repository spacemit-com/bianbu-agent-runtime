from __future__ import annotations

from bianbu_agent.server.app import create_app as root_create_app
from bianbu_agent.engines.contracts import AgentEngine as RootAgentEngine


def test_sdk_exposes_framework_neutral_api_and_engine_packages() -> None:
    import bianbu_agent.api as resource_api
    from bianbu_agent.api import install_api_routes
    from bianbu_agent.engines import (
        AgentEngine,
        decode_tool_name,
        encode_tool_name,
    )

    qualified = "vendor.files/read_file"
    encoded = encode_tool_name(qualified)

    assert callable(root_create_app)
    assert callable(install_api_routes)
    assert not hasattr(resource_api, "create_app")
    assert AgentEngine is RootAgentEngine
    assert encoded == "vendor__files__read_file"
    assert "/" not in encoded
    assert decode_tool_name(encoded) == qualified
    assert decode_tool_name("standalone_tool") == "standalone_tool"

    long_qualified = "vendor." + "namespace." * 10 + "/" + "tool_" * 20
    long_encoded = encode_tool_name(long_qualified)
    assert len(long_encoded) <= 64
    assert decode_tool_name(long_encoded) == long_qualified
