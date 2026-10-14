from __future__ import annotations

from importlib.metadata import entry_points


def test_distribution_does_not_hide_official_plugins_in_entry_points() -> None:
    assert tuple(entry_points(group="bianbu_agent.plugins")) == ()
