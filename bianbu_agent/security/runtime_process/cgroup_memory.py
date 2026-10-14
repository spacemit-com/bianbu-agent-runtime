"""Worker scope 的内存记账取证。

Worker 跑在自己的 transient scope 里，memcg OOM kill 对 RPC 客户端是不可见的：
连接直接断掉，整轮只剩一个裸的 ``EOFError``。这里保留 Console 解释该故障所需
的证据（RUNTIME-WORKER-001）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")

# 只有能证明 hit 到限额时才归因：``memory.peak`` 贴近 ``memory.max`` 是内核回收
# scope 之后仍可用的最好信号，所以这种情形按「疑似」上报，不当作确定结论。
_LIMIT_RATIO = 0.95


@dataclass(frozen=True, slots=True)
class CgroupMemorySample:
    oom_kill: int = 0
    oom_group_kill: int = 0
    peak: int | None = None
    limit: int | None = None
    current: int | None = None

    @property
    def near_limit(self) -> bool:
        return bool(self.limit and self.peak and self.peak >= self.limit * _LIMIT_RATIO)


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text().split()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return int(raw[0])
    except ValueError:
        return None


def _cgroup_dir(cgroup_path: str | Path) -> Path | None:
    relative = str(cgroup_path).strip().lstrip("/")
    if not relative:
        return None
    parts = Path(relative).parts
    if any(part in {"..", ""} for part in parts):
        return None
    return CGROUP_ROOT.joinpath(*parts)


def read_cgroup_memory(cgroup_path: str | Path) -> CgroupMemorySample | None:
    """采样一个 cgroup 的 ``memory.*``；scope 已消失时返回 None。"""

    directory = _cgroup_dir(cgroup_path)
    if directory is None:
        return None
    events = directory / "memory.events"
    try:
        lines = events.read_text().splitlines()
    except OSError:
        return None
    oom_kill = 0
    oom_group_kill = 0
    for line in lines:
        name, _, raw = line.partition(" ")
        if name not in {"oom_kill", "oom_group_kill"}:
            continue
        try:
            value = int(raw.strip() or 0)
        except ValueError:
            continue
        if name == "oom_kill":
            oom_kill = value
        else:
            oom_group_kill = value
    return CgroupMemorySample(
        oom_kill=oom_kill,
        oom_group_kill=oom_group_kill,
        peak=_read_int(directory / "memory.peak"),
        limit=_read_int(directory / "memory.max"),
        current=_read_int(directory / "memory.current"),
    )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "未知"
    gib = value / (1024 ** 3)
    if gib >= 1:
        return f"{gib:.2f} GiB"
    return f"{value / (1024 ** 2):.0f} MiB"


def memory_limit_evidence(sample: CgroupMemorySample | None, *, unit: str) -> str | None:
    """把 memcg OOM 证据翻译成可读原因；没有证据时返回 None。"""

    if sample is None:
        return None
    scope = f"unit={unit}, " if unit else ""
    if sample.oom_kill:
        return (
            f"Worker 因内存限额被杀（{scope}MemoryMax={format_bytes(sample.limit)}，"
            f"MemoryPeak={format_bytes(sample.peak)}，oom_kill={sample.oom_kill}）。"
            "长会话请另起新会话，或为该专家放宽 resources.memory_max。"
        )
    if sample.near_limit and sample.limit and sample.peak:
        return (
            f"Worker 内存曾达到限额的 {sample.peak / sample.limit:.0%}"
            f"（{scope}MemoryMax={format_bytes(sample.limit)}，"
            f"MemoryPeak={format_bytes(sample.peak)}）；"
            "连接随后断开，疑似被内存限额杀死（scope 内暂无 oom_kill 记录，"
            "按内存压力佐证归因）。"
        )
    return None


__all__ = [
    "CGROUP_ROOT",
    "CgroupMemorySample",
    "format_bytes",
    "memory_limit_evidence",
    "read_cgroup_memory",
]
