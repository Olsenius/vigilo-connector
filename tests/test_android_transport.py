import json

import httpx
import pytest

from vigilo_connector import auth
from vigilo_connector.client import VigiloClient, WriteOutcomeUnknown
from vigilo_connector.config import API_BASE, APP_VERSION, AUTH_BASE, WEB_API_BASE
from vigilo_connector.transport import JSON_CONTENT_TYPE, android_client, android_json
from tests.test_parent_actions import FakeStore, transport


def assert_android_headers(request):
    assert request.headers['user-agent'] == 'okhttp/4.10.0'
    assert request.headers['accept-encoding'] == 'gzip'
    assert 'accept' not in request.headers
    assert 'cookie' not in request.headers
    assert 'X-Organizational-Unit-Id' not in request.headers
    assert 'X-Tenant-Id' not in request.headers


@pytest.fixture
def client():
    c = VigiloClient(FakeStore())
    yield c
    c.close()


@pytest.mark.parametrize('path,params', [
    ('/api/children', {'userId': 'test-user', 'childId': 'test-child'}),
    ('/api/users/test-user?userId=a&userId=b&childId=test-child', None),
])
def test_app_headers_and_user_id_removed_at_transport_boundary(client, path, params):
    def handler(request):
        assert_android_headers(request)
        assert request.headers['appVersion'] == APP_VERSION == 'Android 3.3.0-4'
        assert request.headers['authorization'] == 'Bearer old'
        assert 'userId' not in request.url.params
        assert request.url.params['childId'] == 'test-child'
        assert request.url.path == path.split('?')[0]
        assert 'content-type' not in request.headers
        assert request.extensions['timeout'] == {'connect': 100, 'read': 100, 'write': 10, 'pool': 10}
        return httpx.Response(200)
    transport(client, handler)
    assert client.get(path, params).status_code == 200


def test_query_removal_does_not_mutate_callers_params(client):
    params = {'userId': 'test-user', 'childId': 'test-child'}
    transport(client, lambda request: httpx.Response(200))
    client.get('/api/children', params)
    assert params == {'userId': 'test-user', 'childId': 'test-child'}


def test_read_methods_do_not_require_jwt_subject_or_send_user_id(client):
    paths = []
    def handler(request):
        assert 'userId' not in request.url.params
        paths.append(request.url.path)
        return httpx.Response(200, json={})
    transport(client, handler)
    client.get_children()
    client.get_message_threads('test-child')
    client.get_thread('test-thread')
    assert paths == ['/api/children', '/api/message-threads', '/api/messages/threads/test-thread']


def test_json_headers_and_gson_escaping(client):
    payload = {'body': "Blåbær <tag> & a='b'\u2028\u2029", 'attachments': []}
    def handler(request):
        assert_android_headers(request)
        assert request.headers['content-type'] == JSON_CONTENT_TYPE
        assert json.loads(request.content) == payload
        assert b'\\u003ctag\\u003e' in request.content
        assert b'\\u0026' in request.content
        assert b'\\u003d\\u0027b\\u0027' in request.content
        assert b'\\u2028\\u2029' in request.content
        assert 'Blåbær'.encode() in request.content
        assert int(request.headers['content-length']) == len(request.content)
        return httpx.Response(204)
    transport(client, handler)
    client._post_message('/api/messages', payload)


@pytest.mark.parametrize('kind', ['app', 'oauth'])
def test_server_cookies_are_not_persisted_or_replayed(kind):
    seen = []
    def handler(request):
        assert_android_headers(request)
        seen.append(request)
        return httpx.Response(200, headers={'Set-Cookie': 'test-session=synthetic; Path=/'})
    with android_client(API_BASE, app_api=kind == 'app', transport=httpx.MockTransport(handler)) as c:
        c.get('/first')
        c.get('/second')
        assert not list(c.cookies.jar)
    assert len(seen) == 2


def test_range_does_not_enable_gzip_automatically():
    def handler(request):
        assert request.headers['range'] == 'bytes=0-15'
        assert 'accept-encoding' not in request.headers
        return httpx.Response(206)
    with android_client(API_BASE, transport=httpx.MockTransport(handler)) as c:
        c.get('/file', headers={'Range': 'bytes=0-15'})


def test_explicit_encoding_is_preserved():
    def handler(request):
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200)
    with android_client(API_BASE, transport=httpx.MockTransport(handler)) as c:
        c.get('/file', headers={'Accept-Encoding': 'identity'})


def test_filestorage_bypasses_auth_refresh_and_retains_query(client):
    def handler(request):
        assert_android_headers(request)
        assert 'authorization' not in request.headers
        assert 'appVersion' not in request.headers
        assert request.url.params['userId'] == 'test-user'
        return httpx.Response(401)
    transport(client, handler)
    assert client.get('/api/FILESTORAGE/test-file?userId=test-user').status_code == 401
    assert client._store.refreshes == []


def test_filestorage_does_not_strip_auth_from_unrelated_query(client):
    def handler(request):
        assert request.headers['authorization'] == 'Bearer old'
        assert request.headers['appVersion'] == APP_VERSION
        return httpx.Response(200)
    transport(client, handler)
    client.get('/api/children?search=filestorage')


def test_web_api_keeps_separate_headers_and_user_id(client):
    def handler(request):
        assert request.headers['user-agent'].startswith('python-httpx/')
        assert 'appVersion' not in request.headers
        assert request.url.params['userId'] == 'test-user'
        return httpx.Response(200)
    client._web.close()
    client._web = httpx.Client(base_url=WEB_API_BASE, transport=httpx.MockTransport(handler))
    client.web_get('/api/test', {'userId': 'test-user'})


@pytest.mark.parametrize('status', [307, 308])
@pytest.mark.parametrize('operation', ['message', 'absence'])
def test_write_redirects_do_not_replay_post(client, status, operation):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': '/another-write'})
    transport(client, handler)
    with pytest.raises(WriteOutcomeUnknown):
        if operation == 'message':
            client._post_message('/api/messages', {'body': 'Testmelding'})
        else:
            client._post_absence('/api/student-absences', {'note': 'Testfravær'})
    assert len(requests) == 1


def test_oauth_uses_android_transport_without_app_or_bearer_headers(monkeypatch):
    def handler(request):
        assert_android_headers(request)
        assert str(request.url) == AUTH_BASE + '/connect/token'
        assert request.headers['authorization'] == 'Basic synthetic'
        assert 'appVersion' not in request.headers
        assert request.headers['content-type'] == 'application/x-www-form-urlencoded'
        assert request.content == b'refresh_token=synthetic&grant_type=refresh_token'
        return httpx.Response(200, json={'access_token': 'synthetic'})
    monkeypatch.setattr(auth, '_basic_auth', lambda: 'synthetic')
    monkeypatch.setattr(auth, 'android_client', lambda base_url: android_client(
        base_url, transport=httpx.MockTransport(handler)))
    assert auth.refresh('synthetic') == {'access_token': 'synthetic'}


def test_token_refresh_uses_android_margin(monkeypatch, tmp_path):
    monkeypatch.setattr(auth.time, 'time', lambda: 1000)
    monkeypatch.setattr(auth, 'refresh', lambda token: {'access_token': 'new', 'expires_in': 3600})
    store = auth.TokenStore(tmp_path / 'tokens.json')
    store.path.write_text(json.dumps({'access_token': 'old', 'refresh_token': 'synthetic',
                                    'obtained_at': 0, 'expires_in': 1016}))
    assert store.access_token() == 'old'
    monkeypatch.setattr(auth.time, 'time', lambda: 1002)
    assert store.access_token() == 'new'


def test_invalid_json_numbers_rejected_before_dispatch():
    with pytest.raises(ValueError):
        android_json({'value': float('nan')})
