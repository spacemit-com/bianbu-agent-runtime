#!/usr/bin/env python3
"""Tail per-Worker strace files and emit normalized network events as JSONL."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
if (SOURCE_ROOT / "bianbu_agent").is_dir() and str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from bianbu_agent.security.network_learning_collector import parse_strace_line


def _event_id(path: Path, offset: int, index: int, line: str) -> str:
    payload = f"{path.name}:{offset}:{index}:{line}".encode("utf-8", "replace")
    return "strace-network-" + hashlib.sha256(payload).hexdigest()


def scan(trace_dir: Path, offsets: dict[Path, int]) -> int:
    emitted = 0
    for trace in sorted(trace_dir.glob(".syscall-trace.*")):
        try:
            with trace.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(offsets.get(trace, 0))
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        stream.seek(offset)
                        break
                    for index, event in enumerate(parse_strace_line(line)):
                        output = {
                            "event_id": _event_id(trace, offset, index, line),
                            **event,
                            "backend": "strace",
                        }
                        print(json.dumps(output, ensure_ascii=False,
                                         separators=(",", ":")), flush=True)
                        emitted += 1
                offsets[trace] = stream.tell()
        except OSError:
            continue
    return emitted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    trace_dir = Path(args.trace_dir).expanduser().resolve()
    if not trace_dir.is_dir():
        parser.error("trace directory does not exist")
    offsets: dict[Path, int] = {}
    while True:
        scan(trace_dir, offsets)
        if args.once:
            return 0
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
