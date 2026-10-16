"""Independent synthetic checks for console HTTP metadata and stream safety."""
import json

import httpx
import pytest

from vigilo_connector.transport import android_client

PRIVATE_ID = 'synthetic-private-child-id'
PRIVATE_QUERY = 'synthetic-query-secret'
PRIVATE_BODY = 'synthetic-body-secret'
PRIVATE_HEADER = 'synthetic-header-secret'


def records(capsys):
    captured = capsys.readouterr()
    assert captured.out == '', 'HTTP diagnostics must not corrupt MCP stdout'
    return [json.loads(line) for line in captured.err.splitlines() if line.strip()]


def assert_no_private(records_):
    text = json.dumps(records_)
    for secret in [PRIVATE_ID, PRIVATE_QUERY, PRIVATE_BODY, PRIVATE_HEADER,
                   'synthetic-error-url-secret']:
        assert secret not in text
    assert 'authorization' not in text.lower()
    assert 'sig=' not in text


def test_http_logging_default_off_preserves_response(capsys, monkeypatch):
    monkeypatch.delenv('VIGILO_HTTP_LOG', raising=False)
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'ok': True}))) as client:
        assert client.get('/api/children').json() == {'ok': True}
    assert records(capsys) == []


def test_console_logging_redacts_request_and_response_data(capsys, monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'] == PRIVATE_HEADER
        assert request.content == PRIVATE_BODY.encode()
        return httpx.Response(201, json={'note': PRIVATE_BODY}, headers={'X-Private': PRIVATE_HEADER})
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(handler)) as client:
        response = client.post(f'/api/messages/{PRIVATE_ID}/reply?sig={PRIVATE_QUERY}',
                               headers={'Authorization': PRIVATE_HEADER}, content=PRIVATE_BODY)
        assert response.status_code == 201
        assert response.json() == {'note': PRIVATE_BODY}
    output = records(capsys)
    assert len(output) == 2
    assert_no_private(output)
    encoded = json.dumps(output)
    assert 'POST' in encoded
    assert '201' in encoded
    assert 'request' in encoded
    assert 'response' in encoded
    assert len(calls) == 1


def test_transport_error_is_logged_without_exception_text_or_retry(capsys, monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError('https://synthetic-error-url-secret?sig=secret', request=request)
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError):
            client.get('/api/children')
    output = records(capsys)
    assert len(output) == 2
    assert_no_private(output)
    assert 'ConnectError' in json.dumps(output)
    assert len(calls) == 1


def test_response_stream_remains_unread_until_caller_reads(capsys, monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    state = {'reads': 0, 'closed': 0}
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            state['reads'] += 1
            yield PRIVATE_BODY.encode()
        def close(self):
            state['closed'] += 1
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Body()))) as client:
        with client.stream('GET', '/api/children') as response:
            assert state['reads'] == 0
            assert response.is_stream_consumed is False
            assert response.read() == PRIVATE_BODY.encode()
    assert state == {'reads': 1, 'closed': 1}
    output = records(capsys)
    assert len(output) == 2
    assert_no_private(output)


def test_logging_sink_failure_does_not_change_http_result(monkeypatch):
    import sys
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    def broken_write(text):
        raise OSError('synthetic closed console')
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(lambda request: httpx.Response(204))) as client:
        with monkeypatch.context() as local:
            local.setattr(sys.stderr, 'write', broken_write)
            assert client.get('/api/children').status_code == 204


def test_parallel_logging_produces_complete_json_lines(capsys, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda index: client.get(
                f'/api/messages/threads/{PRIVATE_ID}-{index}?query={PRIVATE_QUERY}'), range(24)))
    assert all(response.status_code == 200 for response in responses)
    output = records(capsys)
    assert len(output) == 48
    assert_no_private(output)


def test_console_log_pairs_are_correlated_and_redirects_preserve_wire(capsys, monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={'Location': f'/api/messages/threads/{PRIVATE_ID}?sig={PRIVATE_QUERY}'})
        return httpx.Response(200, content=b'synthetic-response')
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(handler)) as client:
        assert client.get('/api/children').content == b'synthetic-response'
    output = records(capsys)
    assert len(calls) == 2
    assert len(output) == 4
    assert [record['event'] for record in output] == ['request', 'response', 'request', 'response']
    assert output[0]['callId'] == output[1]['callId']
    assert output[2]['callId'] == output[3]['callId']
    assert output[0]['callId'] != output[2]['callId']
    assert output[1]['status'] == 302
    assert output[3]['status'] == 200
    assert all(record['elapsedMs'] >= 0 for record in output if record['event'] != 'request')
    assert_no_private(output)


@pytest.mark.parametrize('origin,path,expected_origin', [
    ('app', '/api/children', 'app'), ('web', '/api/children/my', 'web'),
    ('oauth', '/connect/token', 'oauth'),
    ('attachment', f'/{PRIVATE_ID}/file.pdf', 'attachment'),
    ('app', f'/filestorage/{PRIVATE_ID}/avatar', 'attachment'),
])
def test_all_http_origins_emit_safe_console_metadata(capsys, monkeypatch, origin, path, expected_origin):
    from vigilo_connector.http_logging import LoggedClient
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    with LoggedClient(origin=origin, base_url='https://synthetic.example.test',
                      transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        assert client.get(path, params={'sig': PRIVATE_QUERY}).status_code == 200
    output = records(capsys)
    assert len(output) == 2
    assert all(record['origin'] == expected_origin for record in output)
    assert_no_private(output)


def test_real_oauth_token_request_is_logged_without_body_or_basic_auth(capsys, monkeypatch):
    from vigilo_connector import auth
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    monkeypatch.setattr(auth, '_basic_auth', lambda: PRIVATE_HEADER)
    def handler(request):
        assert request.method == 'POST'
        assert PRIVATE_BODY.encode() in request.content
        return httpx.Response(200, json={'access_token': 'synthetic-new-token'})
    monkeypatch.setattr(auth, 'android_client', lambda base_url: android_client(
        base_url, transport=httpx.MockTransport(handler)))
    result = auth._token_request({'refresh_token': PRIVATE_BODY})
    assert result['access_token'] == 'synthetic-new-token'
    output = records(capsys)
    assert len(output) == 2
    assert all(record['origin'] == 'oauth' for record in output)
    assert 'synthetic-new-token' not in json.dumps(output)
    assert_no_private(output)


def test_gateway_info_logging_does_not_add_unsafe_httpx_urls(capsys, caplog, monkeypatch):
    import logging
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    with caplog.at_level(logging.INFO):
        with android_client('https://app.example.test', app_api=True,
                            transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            client.get(f'/api/messages/threads/{PRIVATE_ID}?sig={PRIVATE_QUERY}')
    assert PRIVATE_ID not in caplog.text
    assert PRIVATE_QUERY not in caplog.text
    assert 'sig=' not in caplog.text
    assert_no_private(records(capsys))


def test_decoding_failure_is_logged_without_reading_or_changing_error(capsys, monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(lambda request: httpx.Response(
                            200, content=b'synthetic-invalid-gzip', headers={'Content-Encoding': 'gzip'}))) as client:
        with pytest.raises(httpx.DecodingError):
            client.get('/api/children')
    output = records(capsys)
    assert output[0]['event'] == 'request'
    assert output[-1]['event'] == 'error'
    assert output[-1]['errorClass'] == 'DecodingError'
    assert 'synthetic-invalid-gzip' not in json.dumps(output)
    assert_no_private(output)


def test_late_stream_read_error_is_logged_once_without_buffering(capsys, monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    read_count = 0
    class FailingBody(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal read_count
            read_count += 1
            yield b'synthetic-first-chunk'
            raise httpx.ReadError('synthetic-error-url-secret')
    with android_client('https://app.example.test', app_api=True,
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=FailingBody()))) as client:
        with client.stream('GET', '/api/children') as response:
            assert read_count == 0
            chunks = response.iter_raw()
            assert next(chunks) == b'synthetic-first-chunk'
            with pytest.raises(httpx.ReadError):
                next(chunks)
    output = records(capsys)
    assert [record['event'] for record in output] == ['request', 'response', 'error']
    assert len({record['callId'] for record in output}) == 1
    assert output[-1]['errorClass'] == 'ReadError'
    assert_no_private(output)
