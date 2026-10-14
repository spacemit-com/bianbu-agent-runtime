"""Download generated HTML without executing it on the Console's origin."""
from __future__ import annotations

import os
from pathlib import Path
import stat
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response

router = APIRouter()
_MAX_HTML_BYTES = 16 * 1024 * 1024


def _read_html(root: Path, path: str) -> bytes:
    parts = path.split('/')
    if (any(part in {'', '.', '..'} for part in parts) or '\\' in path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)):
        raise HTTPException(400, 'Expected a relative workspace file path')
    if Path(parts[-1]).suffix.lower() not in {'.html', '.htm'}:
        raise HTTPException(404, 'HTML artifact not found')

    # Pin every directory with openat and reject symlinks during the actual
    # open, rather than resolve/check and later reopening a mutable path.
    directory = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(404, 'HTML artifact not found')
            if info.st_size > _MAX_HTML_BYTES:
                raise HTTPException(413, 'HTML artifact exceeds the 16 MiB download limit')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                content = stream.read(_MAX_HTML_BYTES + 1)
            if len(content) > _MAX_HTML_BYTES:
                raise HTTPException(413, 'HTML artifact exceeds the 16 MiB download limit')
            return content
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise HTTPException(404, 'HTML artifact not found') from exc
    finally:
        if directory is not None:
            os.close(directory)


@router.get('/workspace/download')
def download_workspace_html(request: Request, path: str = Query(min_length=1, max_length=4096)) -> Response:
    root = request.app.state.runtime.config.data_dir / 'workspace'
    content = _read_html(root, path)
    return Response(content, media_type='application/octet-stream', headers={
        'Content-Disposition': "attachment; filename*=UTF-8''" + quote(path.rsplit('/', 1)[-1], safe=''),
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'",
        'Cache-Control': 'no-store',
    })
