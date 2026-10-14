"""打包的启动路径不得默认给 Runtime / Worker 施加 CPU、内存配额。

单板设备上没有需要保护的其它租户，但两处 policy 默认都会造成实际损失：

- 2 GiB ``MemoryMax``：长会话、Console 轮询和 transcript 读回都会正常涨过
  2 GiB，撞上就被 OOM kill 或重启循环（历史缺陷记录见仓库 issue tracking）。
- 1 核 ``CPUQuota``：Worker 的 Hook 子进程（每次调用都新起一个 Python 进程）、
  engine 构建和工具调用在 8 核板子上排队抢同一颗核，把命令 Hook 的墙钟时间
  从 ~0.43s 抬到 ~0.99s。

所以生产 unit 既不给自己的 cgroup 设 ``MemoryMax``，也显式关掉 securityd 默认
下发的每 Worker 内存/CPU 上限；开发启动脚本同样默认 ``off``。需要复现限额时用
``BIANBU_WORKER_MEMORY_MAX=2G``、``BIANBU_WORKER_CPU_QUOTA=200%`` 显式打开。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "debian" / "bianbu-agent-runtime.service"


def _unit_lines() -> list[str]:
    return [
        line.strip()
        for line in UNIT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_packaged_unit_does_not_cap_the_control_plane_cgroup() -> None:
    """``MemoryMax`` 在本 unit 上只覆盖控制面，长会话会因此无限重启。"""

    assert not [line for line in _unit_lines() if line.startswith("MemoryMax")]


def test_packaged_unit_disables_the_per_worker_resource_caps() -> None:
    assert "Environment=BIANBU_WORKER_MEMORY_MAX=off" in _unit_lines()
    assert "Environment=BIANBU_WORKER_CPU_QUOTA=off" in _unit_lines()


def test_development_launchers_default_to_no_worker_resource_caps() -> None:
    for name in ("run.sh",):
        script = (ROOT / name).read_text(encoding="utf-8")
        for variable in ("BIANBU_WORKER_MEMORY_MAX", "BIANBU_WORKER_CPU_QUOTA"):
            assert (
                f'export {variable}="${{{variable}:-off}}"'
                in script
            ), f"{name} 必须默认不施加 Worker {variable}"
