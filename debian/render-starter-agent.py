#!/usr/bin/python3.14
"""Render the packaged starter Agent template."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

import yaml


PACKAGED_MODEL = {"provider": "custom:bibit", "name": "deepseek-flash"}


def render_starter_agent(source: Path, destination: Path) -> None:
    document: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("agent"), dict):
        raise ValueError("starter Agent source must contain an agent mapping")

    document["agent"]["model"] = dict(PACKAGED_MODEL)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    os.chmod(destination, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    render_starter_agent(args.source, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
