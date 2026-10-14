"""Worker scope 内存记账取证（RUNTIME-WORKER-001）。"""

from __future__ import annotations

from pathlib import Path

from bianbu_agent.security.runtime_process import cgroup_memory


def _scope(tmp_path: Path, name: str = "bianbu-agent-x.scope") -> Path:
    scope = tmp_path / "system.slice" / name
    scope.mkdir(parents=True)
    return scope


def test_read_cgroup_memory_reports_oom_kill_and_peak(tmp_path, monkeypatch):
    monkeypatch.setattr(cgroup_memory, "CGROUP_ROOT", tmp_path)
    scope = _scope(tmp_path)
    (scope / "memory.events").write_text(
        "low 0\nhigh 0\nmax 3\noom 0\noom_kill 1\noom_group_kill 0\n"
    )
    (scope / "memory.peak").write_text("2073649152\n")
    (scope / "memory.max").write_text("2147483648\n")
    (scope / "memory.current").write_text("1234\n")

    sample = cgroup_memory.read_cgroup_memory("/system.slice/bianbu-agent-x.scope")

    assert sample is not None
    assert sample.oom_kill == 1
    assert sample.peak == 2073649152
    assert sample.limit == 2147483648
    assert sample.current == 1234
    reason = cgroup_memory.memory_limit_evidence(sample, unit="bianbu-agent-x.scope")
    assert reason is not None
    assert "内存限额" in reason
    assert "2.00 GiB" in reason and "1.93 GiB" in reason


def test_peak_near_limit_is_reported_as_suspected(tmp_path, monkeypatch):
    monkeypatch.setattr(cgroup_memory, "CGROUP_ROOT", tmp_path)
    scope = _scope(tmp_path)
    (scope / "memory.events").write_text("oom_kill 0\n")
    (scope / "memory.peak").write_text("2100000000\n")
    (scope / "memory.max").write_text("2147483648\n")

    sample = cgroup_memory.read_cgroup_memory("/system.slice/bianbu-agent-x.scope")

    reason = cgroup_memory.memory_limit_evidence(sample, unit="bianbu-agent-x.scope")
    assert reason is not None
    assert "疑似" in reason


def test_healthy_scope_has_no_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(cgroup_memory, "CGROUP_ROOT", tmp_path)
    scope = _scope(tmp_path)
    (scope / "memory.events").write_text("oom_kill 0\n")
    (scope / "memory.peak").write_text("100000000\n")
    (scope / "memory.max").write_text("2147483648\n")

    sample = cgroup_memory.read_cgroup_memory("/system.slice/bianbu-agent-x.scope")

    assert sample is not None
    assert cgroup_memory.memory_limit_evidence(sample, unit="scope") is None


def test_missing_or_traversing_cgroup_paths_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(cgroup_memory, "CGROUP_ROOT", tmp_path)
    assert cgroup_memory.read_cgroup_memory("/system.slice/gone.scope") is None
    assert cgroup_memory.read_cgroup_memory("../../etc") is None
    assert cgroup_memory.read_cgroup_memory("") is None
    assert cgroup_memory.memory_limit_evidence(None, unit="scope") is None
