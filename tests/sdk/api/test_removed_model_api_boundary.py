from __future__ import annotations

from bianbu_agent.api import contracts
from bianbu_agent.api import models


def test_withdrawn_model_and_capability_binding_api_symbols_are_absent() -> None:
    removed_contracts = (
        "CapabilityBindingOptions",
        "CapabilityBindingRequest",
        "CapabilityBindingView",
        "CapabilityName",
        "ModelCreateRequest",
        "ModelUpdateRequest",
        "ModelView",
    )
    removed_helpers = (
        "capability_binding_view",
        "create_model_resource",
        "delete_capability_binding",
        "delete_model_resource",
        "get_model_resource",
        "list_capability_bindings",
        "list_model_resources",
        "model_view",
        "put_capability_binding",
        "update_model_resource",
    )

    assert [name for name in removed_contracts if hasattr(contracts, name)] == []
    assert [name for name in removed_helpers if hasattr(models, name)] == []
