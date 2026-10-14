import httpx
import pytest

from bianbu_agent.capture import llm


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [False, True])
async def test_capture_does_not_consume_async_response_before_caller(tmp_path, enabled):
    consumed = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in [b'data: {"a":1}\n\n', b'data: [DONE]\n\n']:
                consumed.append(part)
                yield part
    hooks = llm.ProviderCaptureHTTPHooks(llm.ProviderCaptureStore(tmp_path), 'test')
    token = llm._ACTIVE_CAPTURE_RUN.set('test-run' if enabled else None)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())),
            event_hooks={'request': [hooks.on_request_async], 'response': [hooks.on_response_async]},
        ) as client:
            async with client.stream('GET', 'https://example.invalid') as response:
                assert consumed == []
                assert await response.aread() == b'data: {"a":1}\n\ndata: [DONE]\n\n'
        record = hooks.pop('test-run')
        if enabled:
            assert record['response']['body'] == ''
        else:
            assert record is None
    finally:
        llm._ACTIVE_CAPTURE_RUN.reset(token)


def test_capture_does_not_consume_sync_response_before_caller(tmp_path):
    consumed = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            consumed.append(True)
            yield b'{"ok":true}'
    hooks = llm.ProviderCaptureHTTPHooks(llm.ProviderCaptureStore(tmp_path), 'test')
    token = llm._ACTIVE_CAPTURE_RUN.set('sync-run')
    try:
        with httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Stream())),
            event_hooks={'request': [hooks.on_request], 'response': [hooks.on_response]},
        ) as client:
            with client.stream('GET', 'https://example.invalid') as response:
                assert consumed == []
                assert response.read() == b'{"ok":true}'
        assert hooks.pop('sync-run')['response']['body'] == ''
    finally:
        llm._ACTIVE_CAPTURE_RUN.reset(token)
