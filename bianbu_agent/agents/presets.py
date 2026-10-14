"""Powers every Agent receives regardless of its own selectors.

An expert installed from AgentHub declares the Skills, MCP servers and Plugins
it depends on, but a fresh device usually has none of them installed yet.  The
APM handbook is what lets the running expert close that gap itself, and the
file/process Tools are what let it act on the result, so the Runtime hands both
to every Agent instead of waiting for ``agent.tools``/``agent.skills`` entries
that an installed package may not carry.

Which names are presets is configuration (``presets`` in ``config.yaml``), not
code: the device profile decides, and an entry the device does not ship is
skipped rather than materialized.
"""

from __future__ import annotations

from collections.abc import Mapping


def with_presets(
    selected: tuple[str, ...],
    preset_names: tuple[str, ...],
    available: Mapping[str, object],
) -> tuple[str, ...]:
    """Prepend the configured presets this device ships to the Agent's own pick.

    Presets stay in front and are de-duplicated, so an Agent that already
    selected one keeps a single entry and the always-on names remain visible in
    the materialized order.
    """

    installed = tuple(name for name in preset_names if name in available)
    return tuple(dict.fromkeys((*installed, *selected)))


__all__ = ["with_presets"]
