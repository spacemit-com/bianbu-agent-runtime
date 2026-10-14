"""Read host upgrade evidence independently of Worker lifetimes."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


DETAIL = Path('/var/lib/bibit-intent/system-update.json')
TERMINAL = {'succeeded', 'succeeded_with_skips', 'failed', 'cancelled'}


async def upgrade_snapshot() -> dict[str, object]:
    result: dict[str, object] = {'maintenance': False, 'state': 'none'}
    try:
        descriptor = os.open(DETAIL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor) as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
                return result
            detail = json.loads(stream.read(65536))
        if not isinstance(detail, dict) or not isinstance(detail.get('state'), str):
            return result
    except (OSError, ValueError, UnicodeError):
        return result
    state = detail['state']
    result['state'] = state
    result['job_id'] = detail.get('job_id', '')
    if state in TERMINAL:
        return result
    if state not in {'system_succeeded', 'agent_upgrading'}:
        return result
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            'systemctl', 'is-active', 'bibit-system-update.service',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=2)
        active = output.decode().strip() in {'active', 'activating', 'reloading'}
    except (OSError, asyncio.TimeoutError, UnicodeError):
        active = False
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
    if active:
        result.update(maintenance=True, message='Runtime 正在升级，暂时不能执行操作；请等待服务恢复。')
    else:
        result.update(state='unknown', message='升级任务没有最终结果，请检查升级日志。')
    return result


class UpgradeMaintenanceMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith('/api/') and request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            snapshot = await upgrade_snapshot()
            if snapshot['maintenance']:
                return JSONResponse(
                    {'error': {'code': 'runtime_upgrading', 'message': snapshot['message'], 'details': snapshot}},
                    status_code=503, headers={'Retry-After': '5'},
                )
        return await call_next(request)
