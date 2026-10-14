"""Package-owned resource definitions; application writes belong in AgentHome."""
from __future__ import annotations

import os
from pathlib import Path

RESOURCE_KINDS = frozenset({'agents', 'mcps', 'models', 'skills', 'plugins'})
SYSTEM_RESOURCE_ROOT = Path('/usr/share/bianbu-agent-runtime')


def system_resource_dir(kind: str) -> Path:
    if kind not in RESOURCE_KINDS:
        raise ValueError(f'unsupported system resource kind: {kind}')
    root = Path(os.environ.get('BIANBU_SYSTEM_RESOURCE_ROOT') or SYSTEM_RESOURCE_ROOT)
    return root.expanduser().absolute() / kind


def is_system_resource(path: Path, kind: str) -> bool:
    return Path(path).absolute().is_relative_to(system_resource_dir(kind))
