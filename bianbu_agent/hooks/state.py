"""Ephemeral command state, scoped to a plugin generation and session.

State is never projected as run metadata or written to disk. Per-session locks
make read/modify/write exchanges atomic across that plugin's event handlers.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary


class CommandSessionState:
    def __init__(self):
        self._values = {}
        self._locks = WeakValueDictionary()
        self._size = 0

    @asynccontextmanager
    async def session(self, key):
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            yield

    def get(self, key):
        return self._values.get(key, ({}, 0))[0]

    def replace(self, key, value):
        if not isinstance(value, dict):
            raise ValueError('state must be an object')
        size = len(json.dumps(value, ensure_ascii=False).encode()) if value else 0
        total = self._size - self._values.get(key, ({}, 0))[1] + size
        if total > 16 * 1024 * 1024 or (value and key not in self._values and len(self._values) >= 1024):
            raise ValueError('Plugin command session state limit exceeded')
        if value:
            self._values[key] = (value, size)
        else:
            self._values.pop(key, None)
        self._size = total

    def clear(self):
        self._values.clear()
        self._size = 0
