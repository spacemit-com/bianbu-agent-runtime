"""Replay declared tool policies without rolling back the operator's edits.

The Runtime owns the declaration of what an Agent may do: the packaged Agent
declaration describes it, and every logical Agent carries its own ``tools`` /
``mcps`` / ``permissions`` block.  The security console owns the operator's
edits to those same rows.

A package upgrade has to make the new declaration effective, and it must not
undo a tightening an operator applied in the console.  A stored row alone
cannot say which of the two it is, so the Runtime keeps the values it declared
last time and replays over the rows nobody touched:

* no stored row: declare it
* stored row already equals the declaration: nothing to do
* stored row differs but still equals what this Runtime declared last: replay
* stored row differs otherwise: the operator owns it, leave it alone

A missing baseline (first release of this bookkeeping) treats existing rows as
replayable: the declaration ships with the package, and tool policies an older
release wrote as its own default must not survive an upgrade just because the
table cannot say who wrote them.
"""
from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

BASELINE_DIRECTORY = "security"
BASELINE_FILENAME = "declared-capabilities.json"


@dataclass(frozen=True)
class ReplayPlan:
    """Declared rows to write, plus the rows the operator owns."""

    writes: dict[str, str] = field(default_factory=dict)
    preserved: dict[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.writes


def plan_replay(*, declared: Mapping[str, str], current: Mapping[str, str],
                previous: Mapping[str, str] | None) -> ReplayPlan:
    """Decide which declared rows may overwrite what securityd stores."""
    writes: dict[str, str] = {}
    preserved: dict[str, str] = {}
    for tool, policy in declared.items():
        name = str(tool)
        want = str(policy)
        stored = current.get(name)
        if stored is None:
            writes[name] = want
            continue
        have = str(stored)
        if have == want:
            continue
        if previous is None or str(previous.get(name, "")) == have:
            writes[name] = want
            continue
        preserved[name] = have
    return ReplayPlan(writes=writes, preserved=preserved)


class DeclarationBaseline:
    """The tool policies this Runtime declared the last time it started."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @classmethod
    def in_data_dir(cls, data_dir: Path | str) -> "DeclarationBaseline":
        return cls(Path(data_dir).expanduser() / BASELINE_DIRECTORY / BASELINE_FILENAME)

    def load(self) -> dict[str, dict[str, str]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return {}
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(document, Mapping):
            return {}
        known: dict[str, dict[str, str]] = {}
        for agent_id, rows in document.items():
            if isinstance(rows, Mapping):
                known[str(agent_id)] = {str(tool): str(policy)
                                        for tool, policy in rows.items()}
        return known

    def declared_for(self, agent_id: str) -> dict[str, str] | None:
        return self.load().get(str(agent_id))

    def record(self, agent_id: str, declared: Mapping[str, str]) -> None:
        document = self.load()
        document[str(agent_id)] = {str(tool): str(policy)
                                   for tool, policy in declared.items()}
        self._store(document)

    def forget(self, agent_id: str) -> None:
        """Drop the local trace of an Agent that no longer owns declarations."""

        document = self.load()
        if document.pop(str(agent_id), None) is not None:
            self._store(document)

    def _store(self, document: Mapping[str, Mapping[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".declared-", suffix=".json")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2,
                          sort_keys=True)
                stream.write("\n")
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


__all__ = ["DeclarationBaseline", "ReplayPlan", "plan_replay"]
