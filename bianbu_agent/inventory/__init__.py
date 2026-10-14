"""Installed capability inventory contracts and catalog construction."""

from bianbu_agent.inventory.models import (
    CapabilityKind,
    CapabilityOrigin,
    CapabilitySource,
    InstalledCapability,
    InventorySnapshot,
)


def __getattr__(name: str):
    if name in {"CapabilityCatalog", "CapabilityCatalogBuilder", "CapabilityConflictError"}:
        from bianbu_agent.inventory import catalog

        return getattr(catalog, name)
    raise AttributeError(name)

__all__ = [
    "CapabilityCatalog",
    "CapabilityCatalogBuilder",
    "CapabilityConflictError",
    "CapabilityKind",
    "CapabilityOrigin",
    "CapabilitySource",
    "InstalledCapability",
    "InventorySnapshot",
]
