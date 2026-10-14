"""System-located Agent declarations, scoped to a Runtime profile."""

import hashlib
import os
from pathlib import Path


# The published declaration is written by root but read by the Runtime user and
# by sandboxed Workers, so it must not live inside the Runtime data directory:
# "/var/lib/bianbu-agent-runtime" is 0750 bianbu-runtime:bianbu-runtime, and a
# 0750 ancestor hides the tree from every reader no matter how the declaration
# itself is chmodded.  This root is package-created as root:root 0755.
SYSTEM_PROFILE_ROOT = Path("/var/lib/bianbu-agent-profiles")


def system_agent_directory(data_dir: Path) -> Path:
    # Scope by the canonical profile, not process UID: sandbox Workers may run
    # as a different UID while still serving the same user's Runtime profile.
    # AgentHome supplies its canonical path. Do not follow user-controlled
    # symlinks again in the privileged publisher: that could redirect a user's
    # system publication into another user's profile namespace.
    profile = os.path.abspath(Path(data_dir).expanduser()).encode()
    return SYSTEM_PROFILE_ROOT / hashlib.sha256(profile).hexdigest() / "agents"
