from pathlib import Path


def test_ebpf_collector_script_has_cgroup_filter_and_json_output():
    script = Path(__file__).parents[2] / "scripts" / "syscall-ebpf-collector.py"
    text = script.read_text(encoding="utf-8")
    assert "cgroupid" in text
    assert "--cgroup" in text
    assert '"syscall"' in text
    assert '"number"' in text


def test_ebpf_collector_script_exposes_denylist():
    script = Path(__file__).parents[2] / "scripts" / "syscall-ebpf-collector.py"
    text = script.read_text(encoding="utf-8")
    assert "finit_module" in text
    assert "--policy-version" in text
