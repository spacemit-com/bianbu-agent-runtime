from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "k3-agent-memory-runtime-start.sh"


def test_runtime_only_start_preserves_agent_memory_confirmation_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert (
        'confirm_token_file="${BIANBU_MEMORY_CONFIRM_TOKEN_FILE:-$state_dir/confirm-token}"'
        in source
    )
    assert 'BIANBU_MEMORY_CONFIRM_TOKEN_FILE="$confirm_token_file"' in source
    validation = source.index("AgentMemory confirmation token file is missing")
    stop_old_runtime = source.index('if [[ -s "$runtime_pid_file" ]]')
    assert validation < stop_old_runtime


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_runtime_only_start_script_has_valid_bash_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
