from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "debian" / "bianbu-agent-network-learning.service"


def test_network_learning_collector_unit_is_root_owned_and_restartable() -> None:
    """A collector crash must not silently stop privileged learning capture."""
    unit = UNIT.read_text(encoding="utf-8")

    assert "User=root" in unit
    assert "Group=bianbu-security" in unit
    assert "ExecStart=/usr/bin/python3.14 /usr/lib/bianbu-agent-runtime/network-learning-ebpf-collector.py --socket /run/bianbu/network-learning/collector.sock" in unit
    assert "Restart=on-failure" in unit


def test_network_learning_socket_directory_is_private_to_security_members() -> None:
    """Only the Runtime's security group may reach the collector socket."""
    unit = UNIT.read_text(encoding="utf-8")
    postinst = (ROOT / "debian" / "postinst").read_text(encoding="utf-8")
    register = (ROOT / "debian" / "register-runtime-manifest").read_text(encoding="utf-8")

    expected_directory = "install -d -m 0770 -o root -g bianbu-security /run/bianbu /run/bianbu/network-learning"
    assert expected_directory in unit
    # The directory is live state below /run, so the device start path creates
    # it.  A package transaction on a build host has no securityd group and
    # must not reach /run at all.
    assert expected_directory in register
    assert "/run/bianbu" not in postinst


def test_package_installs_collector_and_unit_with_safe_modes() -> None:
    rules = (ROOT / "debian" / "rules").read_text(encoding="utf-8")

    assert (
        "install -D -m 0755 scripts/network-learning-ebpf-collector.py "
        '\"$(PACKAGE_ROOT)/usr/lib/bianbu-agent-runtime/network-learning-ebpf-collector.py\"'
    ) in rules
    assert "install -D -m 0644 debian/bianbu-agent-network-learning.service" in rules
    assert (
        '"$(PACKAGE_ROOT)/usr/lib/systemd/system/'
        'bianbu-agent-network-learning.service"'
    ) in rules
    assert "dh_installsystemd --name=bianbu-agent-network-learning" in rules


def test_package_removal_stops_collector_without_touching_securityd_database() -> None:
    prerm = (ROOT / "debian" / "prerm").read_text(encoding="utf-8")
    postrm = (ROOT / "debian" / "postrm").read_text(encoding="utf-8")

    assert "systemctl stop bianbu-agent-network-learning.service" in prerm
    assert "capabilities.db" not in prerm
    assert "capabilities.db" not in postrm


def test_runtime_prepares_learning_directory_even_when_collector_cannot_start():
    unit = (ROOT / 'debian/bianbu-agent-runtime.service').read_text()
    assert 'ExecStartPre=+/usr/bin/install -d -m 0770 -o root -g bianbu-security /run/bianbu/network-learning' in unit
    assert 'bianbu-agent-network-learning.service' in next(line for line in unit.splitlines() if line.startswith('Wants='))
