from __future__ import annotations

import re
from pathlib import Path

import pytest

from bianbu_agent.provisioning import new_agent_id


def test_new_agent_id_uses_bianbu_reverse_domain_and_random_hex() -> None:
    first = new_agent_id()
    second = new_agent_id()

    assert re.fullmatch(r"com\.bianbu\.[0-9a-f]{12}", first)
    assert re.fullmatch(r"com\.bianbu\.[0-9a-f]{12}", second)
    assert first != second


@pytest.mark.parametrize("launcher", ["run.sh"])
def test_development_launchers_share_bibit_agent_provisioner(launcher: str) -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / launcher).read_text(encoding="utf-8")

    assert script.count("scripts/provision-starter-agent.py") == 1
    # run.sh also passes this template to the security-manifest builder.
    assert "resources/default-profile/starter-agent.yaml" in script
    assert "COM_BIANBU_AGENT_ID" in script
    assert "BIANBU_WORKER_AGENT_ID" in script
