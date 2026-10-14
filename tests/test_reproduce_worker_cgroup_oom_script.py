from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "reproduce-worker-cgroup-oom.py"

spec = importlib.util.spec_from_file_location("reproduce_worker_cgroup_oom_script", SCRIPT)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_reading_matches_board_watch_output() -> None:
    sample = mod.Sample(
        current=407322624, peak=596000000, limit=2147483648, oom_kill=0, processes=21,
    )

    assert mod.format_reading(sample) == (
        "Worker 内存  388 MiB / 2048 MiB  (19%)\n"
        "历史峰值     568 MiB\n"
        "[########................................]"
    )


def test_reading_without_limit_and_at_the_limit() -> None:
    unlimited = mod.Sample(current=1048576, peak=1048576, limit=None, oom_kill=0, processes=1)
    assert mod.format_reading(unlimited).splitlines()[0] == "Worker 内存  1 MiB / 无上限"
    assert mod.format_reading(unlimited).splitlines()[2] == "[" + "." * mod.BAR_WIDTH + "]"

    capped = mod.Sample(current=2048 * 1048576, peak=2048 * 1048576, limit=2048 * 1048576, oom_kill=3, processes=9)
    assert mod.format_reading(capped).splitlines()[0] == "Worker 内存  2048 MiB / 2048 MiB  (100%)"
    assert mod.format_reading(capped).splitlines()[2] == "[" + "#" * mod.BAR_WIDTH + "]"


def test_parse_limit_treats_max_as_unlimited() -> None:
    assert mod.parse_limit("max") is None
    assert mod.parse_limit("2147483648\n") == 2147483648
    with pytest.raises(ValueError):
        mod.parse_limit("2G")


def test_scope_from_pid_reads_the_worker_cgroup(monkeypatch, tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    cgroup = proc_root / "2010337" / "cgroup"
    cgroup.parent.mkdir(parents=True)
    cgroup.write_text(
        "0::/system.slice/bianbu-agent-com.bianbu.runtime-8ebb6067185e.scope\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "PROC_ROOT", proc_root)
    monkeypatch.setattr(mod, "CGROUP_ROOT", cgroup_root)

    scope = mod.scope_from_pid(2010337)

    assert scope == cgroup_root / "system.slice/bianbu-agent-com.bianbu.runtime-8ebb6067185e.scope"
    assert mod.scope_from_pid(999999) is None


def test_scope_from_hash_survives_worker_restart(monkeypatch, tmp_path: Path) -> None:
    slice_dir = tmp_path / "system.slice"
    slice_dir.mkdir()
    previous = slice_dir / "bianbu-agent-com.bianbu.runtime-57e3612e3cf1.scope"
    previous.mkdir()
    current = slice_dir / "bianbu-agent-com.bianbu.runtime-8ebb6067185e.scope"
    current.mkdir()
    monkeypatch.setattr(mod, "CGROUP_ROOT", tmp_path)

    assert mod.scope_from_hash("8ebb6067185e") == current
    assert mod.scope_from_hash("deadbeefdead") is None


def test_resolve_scope_prefers_the_live_pid(monkeypatch, tmp_path: Path) -> None:
    live = tmp_path / "live.scope"
    monkeypatch.setattr(mod, "scope_from_pid", lambda pid: live if pid == 42 else None)

    assert mod.resolve_scope({"pid": 42, "instance_id": "agent-8ebb6067185e"}) == live
    monkeypatch.setattr(mod, "scope_from_pid", lambda pid: None)
    monkeypatch.setattr(mod, "scope_from_hash", lambda suffix: tmp_path / f"{suffix}.scope")
    assert mod.resolve_scope({"pid": 42, "instance_id": "agent-8ebb6067185e"}) == tmp_path / "8ebb6067185e.scope"


def test_oom_kill_count_spike_is_read_from_memory_events(monkeypatch, tmp_path: Path) -> None:
    scope = tmp_path / "scope"
    scope.mkdir()
    (scope / "memory.current").write_text("2147483648\n", encoding="utf-8")
    (scope / "memory.peak").write_text("2147483648\n", encoding="utf-8")
    (scope / "memory.max").write_text("2147483648\n", encoding="utf-8")
    (scope / "memory.events").write_text(
        "low 0\nhigh 0\nmax 128\noom 4\noom_kill 1\noom_group_kill 0\n", encoding="utf-8",
    )
    (scope / "cgroup.procs").write_text("1\n2\n", encoding="utf-8")

    sample = mod.read_sample(scope)

    assert sample is not None
    assert (sample.current, sample.peak, sample.limit, sample.oom_kill, sample.processes) == (
        2147483648, 2147483648, 2147483648, 1, 2,
    )
    assert mod.read_sample(tmp_path / "missing") is None


def test_run_terminal_event_reads_the_worker_oom_attribution() -> None:
    body = {
        "data": [
            {"seq": 1, "type": "user/message", "run_id": "run-1", "data": {}},
            {"seq": 2, "type": "assistant/text.delta", "run_id": "run-1", "data": {"text": "hi"}},
            {
                "seq": 3,
                "type": "run/failed",
                "run_id": "run-1",
                "data": {"error": {"code": "worker_oom_killed", "message": "Worker 内存曾达到限额的 100%"}},
            },
            {"seq": 4, "type": "run/completed", "run_id": "run-0", "data": {}},
        ],
    }

    event = mod.run_terminal_event(body, "run-1")

    assert event is not None
    assert event["type"] == "run/failed"
    assert event["data"]["error"]["code"] == "worker_oom_killed"
    assert mod.run_terminal_event(body, "run-9") is None


def test_estimate_rounds_uses_the_measured_growth() -> None:
    sample = mod.Sample(current=1000, peak=1000, limit=2000, oom_kill=0, processes=1)

    assert mod.estimate_rounds(sample, 34, 1.0) == 29
    assert mod.estimate_rounds(sample, 0, 1.0) is None
    assert mod.estimate_rounds(sample, 10000, 1.0) == 0


def _fake_scope(tmp_path: Path) -> Path:
    scope = tmp_path / "bianbu-agent-com.bianbu.runtime-8ebb6067185e.scope"
    scope.mkdir()
    return scope


def _patch_runtime(monkeypatch, tmp_path: Path, terminal_events: list[dict]) -> dict[str, int]:
    scope = _fake_scope(tmp_path)
    monkeypatch.setattr(mod, "resolve_scope", lambda execution: scope)
    monkeypatch.setattr(mod, "scope_processes", lambda path, limit=3: ())
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    readings = [
        mod.Sample(current=700 * 1048576, peak=700 * 1048576, limit=2048 * 1048576, oom_kill=0, processes=5),
        mod.Sample(current=740 * 1048576, peak=740 * 1048576, limit=2048 * 1048576, oom_kill=0, processes=5),
        mod.Sample(current=780 * 1048576, peak=780 * 1048576, limit=2048 * 1048576, oom_kill=0, processes=5),
    ]
    state = {"index": 0, "prompts": 0}

    def read_sample(path):
        state["index"] = min(state["index"] + 1, len(readings) - 1)
        return readings[state["index"]]

    monkeypatch.setattr(mod, "read_sample", read_sample)

    def request_json(base, method, path, payload=None, timeout=30.0):
        if method == "GET" and path.startswith("/agents/"):
            return {"data": {"execution": {"pid": 42, "instance_id": "agent-8ebb6067185e", "state": "running"}}}
        if method == "POST" and path == "/sessions":
            return {"data": {"id": "session-oom"}}
        if method == "POST" and path.endswith("/prompt"):
            state["prompts"] += 1
            return {"data": {"accepted": True, "run_id": f"run-{state['prompts']}"}}
        if method == "GET" and "/events" in path:
            index = min(state["prompts"] - 1, len(terminal_events) - 1)
            return {"data": [terminal_events[index]], "last_seq": 10 * state["prompts"]}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(mod, "request_json", request_json)
    return state


def test_drive_stops_when_worker_is_oom_killed(monkeypatch, tmp_path: Path, capsys) -> None:
    events = [{
        "type": "run/failed",
        "run_id": "run-1",
        "data": {"error": {"code": "worker_oom_killed", "message": "Worker 内存曾达到限额的 100%", "details": {"exception": "EOFError"}}},
    }]
    state = _patch_runtime(monkeypatch, tmp_path, events)
    report = tmp_path / "report.json"

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--rounds", "3", "--report", str(report)])

    assert code == 0
    assert state["prompts"] == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["rounds"][0]["error"]["code"] == "worker_oom_killed"
    assert payload["rounds"][0]["memory_after"] >= payload["rounds"][0]["memory_before"] > 0
    assert payload["rounds"][0]["memory_peak"] >= payload["rounds"][0]["memory_after"]
    captured = capsys.readouterr().out
    assert "复现成功" in captured
    assert "worker_oom_killed" in captured


def test_drive_stops_before_the_limit_without_until_oom(monkeypatch, tmp_path: Path, capsys) -> None:
    events = [{
        "type": "run/completed",
        "run_id": "run-1",
        "data": {"status": "completed", "content": "x", "usage": {"output_tokens": 3000}},
    }]
    state = _patch_runtime(monkeypatch, tmp_path, events)

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--rounds", "3", "--stop-at", "30"])

    assert code == 0
    assert state["prompts"] == 1
    captured = capsys.readouterr().out
    assert "--stop-at" in captured
    assert "Worker 内存" in captured


def test_watch_only_mode_never_sends_a_prompt(monkeypatch, tmp_path: Path, capsys) -> None:
    state = _patch_runtime(monkeypatch, tmp_path, [])

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--watch", "--rounds", "2"])

    assert code == 0
    assert state["prompts"] == 0
    assert capsys.readouterr().out.count("Worker 内存") == 2


def _patch_scope_loss(monkeypatch, tmp_path: Path, terminal: dict) -> None:
    """scope 在轮次中途被回收：读数为 None，但终态事件仍然能拿到。"""

    scope = _fake_scope(tmp_path)
    monkeypatch.setattr(mod, "resolve_scope", lambda execution: scope)
    monkeypatch.setattr(mod, "scope_processes", lambda path, limit=3: ())
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    prompts = {"count": 0, "calls": 0}

    def read_sample(path):
        # 前两次（起始读数、本轮起始）还能读到，之后 scope 被内核摘掉。
        prompts["calls"] += 1
        if prompts["calls"] > 2:
            return None
        return mod.Sample(
            current=100 * 1048576, peak=100 * 1048576, limit=2048 * 1048576,
            oom_kill=0, processes=1,
        )

    monkeypatch.setattr(mod, "read_sample", read_sample)

    def request_json(base, method, path, payload=None, timeout=30.0):
        if method == "GET" and path.startswith("/agents/"):
            return {"data": {"execution": {"pid": 42, "instance_id": "agent-8ebb6067185e", "state": "running"}}}
        if method == "POST" and path == "/sessions":
            return {"data": {"id": "session-oom"}}
        if method == "POST" and path.endswith("/prompt"):
            prompts["count"] += 1
            return {"data": {"accepted": True, "run_id": "run-1"}}
        if method == "GET" and "/events" in path:
            return {"data": [terminal], "last_seq": 10}
        raise AssertionError(f"unexpected request: {method} {path}")

    monkeypatch.setattr(mod, "request_json", request_json)


def test_scope_loss_with_oom_attribution_counts_as_reproduced(monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_scope_loss(monkeypatch, tmp_path, {
        "type": "run/failed",
        "run_id": "run-1",
        "data": {"error": {"code": "worker_oom_killed", "message": "Worker 内存曾达到限额的 100%"}},
    })

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--until-oom"])

    assert code == 0
    captured = capsys.readouterr().out
    assert "复现成功" in captured
    assert "继续等这一轮的终态事件" in captured


def test_scope_loss_without_oom_attribution_is_a_restart(monkeypatch, tmp_path: Path, capsys) -> None:
    _patch_scope_loss(monkeypatch, tmp_path, {
        "type": "run/completed",
        "run_id": "run-1",
        "data": {"status": "completed", "content": "x"},
    })

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--until-oom"])

    assert code == 3
    captured = capsys.readouterr().out
    assert "复现成功" not in captured
    assert "没有 worker_oom_killed 归因" in captured


def test_keep_going_skips_transient_provider_failures(monkeypatch, tmp_path: Path, capsys) -> None:
    """provider ReadError 这类瞬时失败不该中断自然累积。"""

    events = [
        {"type": "run/failed", "run_id": "run-1", "data": {"error": {"code": "run_failed", "message": "ReadError"}}},
        {"type": "run/completed", "run_id": "run-2", "data": {"status": "completed", "content": "x"}},
    ]
    state = _patch_runtime(monkeypatch, tmp_path, events)

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--rounds", "2", "--keep-going", "--stop-at", "0"])

    assert code == 0
    assert state["prompts"] == 2
    assert "--keep-going 跳过" in capsys.readouterr().out


def test_transient_failure_stops_without_keep_going(monkeypatch, tmp_path: Path) -> None:
    events = [
        {"type": "run/failed", "run_id": "run-1", "data": {"error": {"code": "run_failed", "message": "ReadError"}}},
    ]
    state = _patch_runtime(monkeypatch, tmp_path, events)

    code = mod.main(["--agent-id", "com.bianbu.278200e1617c", "--rounds", "2"])

    assert code == 2
    assert state["prompts"] == 1
