"""HTTP-metadata må aldri ta med request-/responsinnhald eller private URL-ar."""
import json

import httpx
import pytest

from vigilo_connector import http_logging
from vigilo_connector.http_logging import LoggedClient
from vigilo_connector.transport import android_client


def records(capsys):
    output = capsys.readouterr()
    assert output.out == ''
    return [json.loads(line) for line in output.err.splitlines()]


@pytest.mark.parametrize('enabled', [None, '0', 'true'])
def test_logging_default_and_other_values_are_off(monkeypatch, capsys, enabled):
    if enabled is None:
        monkeypatch.delenv('VIGILO_HTTP_LOG', raising=False)
    else:
        monkeypatch.setenv('VIGILO_HTTP_LOG', enabled)
    with android_client('https://api.example', app_api=True,
                        transport=httpx.MockTransport(lambda r: httpx.Response(204))) as client:
        client.get('/api/children', headers={'Authorization': 'Bearer TEST-SECRET'})
    assert records(capsys) == []


def test_logging_redacts_headers_queries_bodies_and_path_ids(monkeypatch, capsys):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    def handler(request):
        assert request.headers['authorization'] == 'Bearer TEST-SECRET'
        assert request.url.params['childId'] == 'TEST-CHILD-ID'
        assert request.content == b'TEST-MESSAGE-BODY'
        return httpx.Response(403, content=b'TEST-RESPONSE-BODY', headers={'Set-Cookie': 'TEST-COOKIE'})
    with android_client('https://private-host.example', app_api=True,
                        transport=httpx.MockTransport(handler)) as client:
        response = client.post('/api/messages/TEST-THREAD-ID/reply?childId=TEST-CHILD-ID&sig=TEST-SIGNATURE',
            headers={'Authorization': 'Bearer TEST-SECRET'}, content=b'TEST-MESSAGE-BODY', follow_redirects=False)
    assert response.status_code == 403
    logged = records(capsys)
    assert [entry['event'] for entry in logged] == ['request', 'response']
    assert logged[0]['callId'] == logged[1]['callId']
    assert logged[0]['route'] == '/api/messages/{threadId}/reply'
    assert logged[1]['status'] == 403
    assert logged[1]['elapsedMs'] >= 0
    serialized = json.dumps(logged)
    assert all(secret not in serialized for secret in (
        'private-host', 'TEST-SECRET', 'TEST-CHILD-ID', 'TEST-THREAD-ID', 'TEST-SIGNATURE',
        'TEST-MESSAGE-BODY', 'TEST-RESPONSE-BODY', 'TEST-COOKIE'))


@pytest.mark.parametrize('origin,path,expected', [
    ('app', '/api/children', '/api/children'),
    ('web', '/api/students/TEST-ID/lessons', '/api/students/{childId}/lessons'),
    ('oauth', '/connect/token', '/connect/token'),
    ('oauth', '/connect/authorize?state=TEST-STATE', '/connect/authorize'),
    ('attachment', '/TEST-FILE.pdf?sig=TEST-SIGNATURE', '/{attachment}'),
    ('app', '/filestorage/TEST-FILE.pdf?sig=TEST-SIGNATURE', '/filestorage/{redacted}'),
    ('app', '/unknown/TEST-PRIVATE-ID', '/{redacted}'),
])
def test_logging_categories_and_safe_routes(monkeypatch, capsys, origin, path, expected):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    with LoggedClient(origin=origin, base_url='https://api.example',
                      transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        client.get(path)
    logged = records(capsys)
    assert logged[0]['route'] == expected
    assert logged[0]['origin'] == ('attachment' if 'filestorage' in path else origin)
    assert 'TEST-' not in json.dumps(logged)


def test_transport_error_logs_only_safe_class_and_is_not_retried(monkeypatch, capsys):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('TEST-SECRET-IN-EXCEPTION', request=request)
    with LoggedClient(origin='web', transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout, match='TEST-SECRET'):
            client.get('https://api.example/api/posts/TEST-POST')
    logged = records(capsys)
    assert [entry['event'] for entry in logged] == ['request', 'error']
    assert logged[0]['callId'] == logged[1]['callId']
    assert logged[1]['errorClass'] == 'ReadTimeout'
    assert logged[1]['status'] is None
    assert 'TEST-' not in json.dumps(logged)
    assert len(calls) == 1


def test_logging_failure_never_changes_request_or_response(monkeypatch):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    def fail(record):
        raise OSError('Synthetic stderr failure')
    monkeypatch.setattr(http_logging, '_emit', fail)
    with LoggedClient(origin='app', transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b'unchanged'))) as client:
        assert client.get('https://api.example/api/children').content == b'unchanged'


def test_logging_hooks_do_not_consume_stream(monkeypatch, capsys):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield b'test-body'
    with LoggedClient(origin='app', transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=Body()))) as client:
        with client.stream('GET', 'https://api.example/api/children') as response:
            assert not response.is_stream_consumed
            assert response.read() == b'test-body'
    assert len(records(capsys)) == 2


def test_redirects_have_distinct_correlated_pairs_without_location(monkeypatch, capsys):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    def handler(request):
        if request.url.path == '/api/children':
            return httpx.Response(302, headers={'Location': '/private/TEST-ID?sig=TEST-SECRET'})
        return httpx.Response(200)
    with LoggedClient(origin='app', follow_redirects=True,
                      transport=httpx.MockTransport(handler)) as client:
        assert client.get('https://api.example/api/children').status_code == 200
    logged = records(capsys)
    assert [item['event'] for item in logged] == ['request', 'response', 'request', 'response']
    assert logged[0]['callId'] == logged[1]['callId']
    assert logged[2]['callId'] == logged[3]['callId']
    assert logged[0]['callId'] != logged[2]['callId']
    assert 'TEST-' not in json.dumps(logged)


def test_late_stream_transport_error_is_logged_once_without_consumption(monkeypatch, capsys):
    monkeypatch.setenv('VIGILO_HTTP_LOG', '1')
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'partial'
            raise httpx.ReadError('TEST-SENSITIVE-ERROR')
    with LoggedClient(origin='attachment', transport=httpx.MockTransport(
            lambda r: httpx.Response(200, stream=BrokenBody()))) as client:
        with client.stream('GET', 'https://api.example/TEST-FILE?sig=TEST-SECRET') as response:
            assert not response.is_stream_consumed
            with pytest.raises(httpx.ReadError):
                response.read()
    logged = records(capsys)
    assert [item['event'] for item in logged] == ['request', 'response', 'error']
    assert len({item['callId'] for item in logged}) == 1
    assert logged[-1]['errorClass'] == 'ReadError'
    assert 'TEST-' not in json.dumps(logged)
