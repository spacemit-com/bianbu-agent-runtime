"""Strict YAML loading shared by every package manifest and resource header.

Package documents (Agent manifests, SKILL.md frontmatter) are untrusted input:
they arrive from vendor archives and the public catalog.  ``safe_load`` already
refuses arbitrary Python objects, and this loader additionally refuses YAML
aliases and repeated mapping keys so a document cannot smuggle a second value
for a field that a reader resolved earlier.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

import yaml
from yaml.events import AliasEvent


class StrictSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects aliases and repeated mapping keys."""

    def compose_node(self, parent: Any, index: Any) -> yaml.Node:
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise yaml.composer.ComposerError(
                None,
                None,
                "YAML aliases are not allowed",
                event.start_mark,
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, Hashable):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found unhashable key",
                    key_node.start_mark,
                )
            if key in mapping:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_strict_yaml(text: str) -> Any:
    """Load one YAML document, rejecting aliases and duplicate keys."""

    return yaml.load(text, Loader=StrictSafeLoader)


__all__ = ["StrictSafeLoader", "load_strict_yaml"]
