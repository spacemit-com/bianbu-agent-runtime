from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from bianbu_agent.api.workspace import router
from bianbu_agent.server.auth import AdminAuthMiddleware, auth_router


@pytest.fixture
def download_app(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    report = workspace / 'AI 资讯' / 'report.html'
    report.parent.mkdir()
    report.write_text('<!doctype html><html><script>window.report=true;</script>AI资讯</html>')
    app = FastAPI()
    app.state.runtime = SimpleNamespace(config=SimpleNamespace(data_dir=tmp_path))
    app.add_middleware(AdminAuthMiddleware)
    app.include_router(auth_router)
    app.include_router(router, prefix='/api/v1')
    return app, workspace


@pytest.mark.asyncio
async def test_html_download_is_attachment_with_exact_bytes(download_app):
    app, workspace = download_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/v1/workspace/download', params={'path': 'AI 资讯/report.html'})
    assert response.status_code == 200
    assert response.content == (workspace / 'AI 资讯/report.html').read_bytes()
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['content-disposition'] == "attachment; filename*=UTF-8''report.html"
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'sandbox' in response.headers['content-security-policy']
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['../secret.html', 'AI 资讯/../../secret.html', '/tmp/secret.html',
                                  'AI 资讯/./report.html', 'AI 资讯//report.html', 'x\\..\\secret.html',
                                  'report.html\x00', 'AI 资讯/report.html\n'])
async def test_download_rejects_traversal_and_malformed_paths(download_app, path):
    app, workspace = download_app
    (workspace.parent / 'secret.html').write_text('outside secret')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/v1/workspace/download', params={'path': path})
    assert response.status_code == 400
    assert b'outside secret' not in response.content


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['linked.html', 'linked-dir/secret.html', 'AI 资讯/report.txt', 'missing.html', 'directory.html'])
async def test_download_rejects_symlinks_non_html_and_non_files(download_app, path):
    app, workspace = download_app
    outside = workspace.parent / 'outside'
    outside.mkdir()
    (outside / 'secret.html').write_text('outside secret')
    (workspace / 'linked.html').symlink_to(outside / 'secret.html')
    (workspace / 'linked-dir').symlink_to(outside, target_is_directory=True)
    (workspace / 'AI 资讯/report.txt').write_text('not an HTML artifact')
    (workspace / 'directory.html').mkdir()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/v1/workspace/download', params={'path': path})
    assert response.status_code == 404
    assert b'outside secret' not in response.content


@pytest.mark.asyncio
async def test_download_applies_enabled_admin_auth_and_session_cookie(download_app, monkeypatch):
    app, _ = download_app
    monkeypatch.setattr('bianbu_agent.server.auth.ADMIN_AUTH_ENABLED', True)
    monkeypatch.setenv('BIANBU_RUNTIME_MODE', 'production')
    monkeypatch.setenv('BIANBU_RUNTIME_ADMIN_TOKEN', 'download-test-token')
    path = '/api/v1/workspace/download?path=AI%20%E8%B5%84%E8%AE%AF/report.html'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers={'Authorization': 'Bearer wrong'})).status_code == 401
        assert (await client.get(path, headers={'Authorization': 'Bearer download-test-token'})).status_code == 200
        login = await client.post('/api/v1/auth/session', json={'token': 'download-test-token'})
        assert login.status_code == 200
        assert (await client.get(path)).status_code == 200


@pytest.mark.asyncio
async def test_download_rejects_symlink_workspace_root(download_app):
    app, workspace = download_app
    real = workspace.rename(workspace.parent / 'real-workspace')
    workspace.symlink_to(real, target_is_directory=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/v1/workspace/download', params={'path': 'AI 资讯/report.html'})).status_code == 404


@pytest.mark.asyncio
async def test_download_bounds_size_and_never_reads_a_fifo(download_app):
    import os
    from bianbu_agent.api.workspace import _MAX_HTML_BYTES

    app, workspace = download_app
    os.mkfifo(workspace / 'pipe.html')
    with (workspace / 'large.html').open('wb') as output:
        output.truncate(_MAX_HTML_BYTES + 1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        assert (await client.get('/api/v1/workspace/download', params={'path': 'pipe.html'})).status_code == 404
        assert (await client.get('/api/v1/workspace/download', params={'path': 'large.html'})).status_code == 413


@pytest.mark.asyncio
async def test_download_unicode_filename_and_no_second_percent_decoding(download_app):
    app, workspace = download_app
    (workspace / '报告.html').write_text('<html>中文</html>')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/v1/workspace/download', params={'path': '报告.html'})
        assert response.status_code == 200
        assert response.headers['content-disposition'] == "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.html"
        response = await client.get('/api/v1/workspace/download', params={'path': '%2e%2e/secret.html'})
        assert response.status_code == 404
