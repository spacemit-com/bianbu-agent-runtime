#!/usr/bin/env python3
"""复现：工具输出里的 Unicode 行边界字符让 Runtime 启动失败。

背景与实机取证见历史缺陷记录（Session transcript NEL 字符导致启动失败）。

机制：Session transcript 是 JSON Lines，一行一条记录，行间固定用 ``"\\n"`` 分隔
（见 ``SessionLogStore._append_transcript``）。读取侧如果改用 ``str.splitlines()``，
会额外在 ``U+000B``/``U+000C``/``U+001C``-``U+001E``/``U+0085``/``U+2028``/``U+2029``
处断行；其中 ``U+0085``、``U+2028``、``U+2029`` 恰好不会被 ``json.dumps`` 转义，
会原样落盘，于是一条完整记录被切成几段非法 JSON。``SessionLogStore.open()`` 在
启动时遍历全部会话，任一条解析失败就让整个 Runtime 抛 ``RuntimeStartupError``
退出，systemd 于是无限重启。

脚本走真实写入路径：每个候选字符用独立目录写入一条 ``tool_end`` 事件，写入后
重新 ``open()`` 一次，报告哪些字符会让重启读日志失败。

用法::

    # 全部候选字符（默认）
    ./scripts/reproduce-session-transcript-nel-startup-failure.py

    # 只测某个字符
    ./scripts/reproduce-session-transcript-nel-startup-failure.py --character 0x85

退出码：复现到至少一个崩溃返回 1，全部正常返回 0。
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bianbu_agent.runs.events import RunEvent
from bianbu_agent.sessions.log_store import SessionLogStore

SESSION_ID = "01" + "0" * 24
RUN_ID = "run-repro"


@dataclass(frozen=True)
class Candidate:
    label: str
    character: str


CANDIDATES = (
    Candidate("U+000B VT", "\x0b"),
    Candidate("U+000C FF", "\x0c"),
    Candidate("U+001C FS", "\x1c"),
    Candidate("U+001D GS", "\x1d"),
    Candidate("U+001E RS", "\x1e"),
    Candidate("U+0085 NEL", "\u0085"),
    Candidate("U+2028 LS", "\u2028"),
    Candidate("U+2029 PS", "\u2029"),
)


async def _write_tool_output(root: Path, character: str) -> None:
    """用真实存储路径写一条带该字符的 terminal 工具结果。"""

    store = SessionLogStore(root)
    await store.open()
    await store.start(
        RUN_ID,
        SESSION_ID,
        {"prompt": "复现行边界字符", "thread_id": SESSION_ID, "metadata": {}},
    )
    await store.append(
        RUN_ID,
        RunEvent(
            "tool_end",
            {
                "call_id": "call-repro",
                "name": "terminal",
                "result": json.dumps(
                    {"output": f"a{character}b", "exit_code": 0, "status": "completed"},
                    ensure_ascii=False,
                ),
            },
        ),
    )
    await store.close()


async def _reopen(root: Path) -> int:
    """模拟 Runtime 重启：重新打开会话日志，返回恢复出的事件数。"""

    store = SessionLogStore(root)
    await store.open()
    events = await store.read_session_events(SESSION_ID)
    await store.close()
    return len(events)


def _is_stored_verbatim(root: Path, character: str) -> bool:
    """该字符是否未经转义就落进 transcript（决定它会不会被 splitlines 咬到）。"""

    import zstandard

    encoded = (root / SESSION_ID / "session.jsonl.zstd").read_bytes()
    decoder = zstandard.ZstdDecompressor().decompressobj()
    return character in decoder.decompress(encoded).decode("utf-8")


def _probe(candidate: Candidate, workdir: Path) -> tuple[bool, bool, str]:
    """返回 (重启读回是否正常, 字符是否裸落盘, 报错文本)。"""

    root = workdir / candidate.label.replace(" ", "-")
    root.mkdir(parents=True, exist_ok=True)
    asyncio.run(_write_tool_output(root, candidate.character))
    bare = _is_stored_verbatim(root, candidate.character)
    try:
        asyncio.run(_reopen(root))
    except Exception as exc:  # noqa: BLE001 - 复现脚本要展示原始异常
        return False, bare, f"{type(exc).__name__}: {exc}"
    return True, bare, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--character",
        help="只测单个字符，取值如 0x85 / 0x2028 / U+2029（默认测全部候选）",
    )
    args = parser.parse_args()

    if args.character:
        raw = args.character.strip().replace("U+", "").replace("u+", "")
        code = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 16)
        selected = (Candidate(f"U+{code:04X}", chr(code)),)
    else:
        selected = CANDIDATES

    workdir = Path(tempfile.mkdtemp(prefix="repro-transcript-boundary-"))
    print(f"临时目录：{workdir}")
    print(f"{'字符':<14}{'落入 transcript':<18}{'重启读回':<12}报错")
    print("-" * 88)

    reproduced = 0
    try:
        for candidate in selected:
            reopened_ok, stored_bare, error = _probe(candidate, workdir)
            stored = "原样落盘" if stored_bare else "已被转义"
            verdict = "正常" if reopened_ok else "启动失败"
            if not reopened_ok:
                reproduced += 1
            print(f"{candidate.label:<14}{stored:<18}{verdict:<12}{error}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print()
    if reproduced:
        print(f"复现成功：{reproduced}/{len(selected)} 个字符会让 Runtime 重启失败。")
        return 1
    print("未复现：所有候选字符都能正常读回。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
