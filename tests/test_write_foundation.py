"""Syntetiske kontrakttester for felles skriving og nødvendige leseflyter."""
import json

import httpx
import pytest

from vigilo_connector.auth import AuthError
from vigilo_connector.client import VigiloClient, WriteOutcomeUnknown
from tests.test_parent_actions import FakeStore, transport


@pytest.fixture
def client():
    c = VigiloClient(FakeStore())
    yield c
    c.close()


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
@pytest.mark.parametrize('payload', [None, {}, {'note': 'Testmerknad <&>'}])
def test_write_verbs_body_and_endpoint_headers(client, method, payload):
    def handler(request):
        assert request.method == method
        assert request.headers['X-Organizational-Unit-Id'] == 'test-unit'
        assert request.headers['authorization'] == 'Bearer old'
        if payload is None:
            assert request.content == b''
            assert 'content-type' not in request.headers
        else:
            assert request.headers['content-type'] == 'application/json; charset=UTF-8'
            assert json.loads(request.content) == payload
        return httpx.Response(204)
    transport(client, handler)
    client._write_json(method, '/api/test', payload,
                       headers={'X-Organizational-Unit-Id': 'test-unit'})


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 400, 403, 409, 422, 500, 503, 'timeout'])
def test_write_does_not_replay_or_follow_redirect(client, method, status):
    calls = []
    def handler(request):
        calls.append(request)
        if status == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        return httpx.Response(status, headers={'Location': '/api/other'})
    transport(client, handler)
    error = (WriteOutcomeUnknown if status == 'timeout' or 300 <= status < 400 or status >= 500
             else httpx.HTTPStatusError)
    with pytest.raises(error):
        client._write_json(method, '/api/test')
    assert len(calls) == 1


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE'])
def test_new_write_operation_does_not_opt_into_auth_replay(client, method):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401)
    transport(client, handler)
    with pytest.raises(AuthError):
        client._write_json(method, '/api/test')
    assert len(calls) == 1
    assert client._store.refreshes == [False]


@pytest.mark.parametrize('status', [403, 500])
def test_error_exposes_only_correlation_not_response_body(client, status):
    def handler(request):
        return httpx.Response(status, headers={'X-Correlation-Id': 'test-correlation-123'},
                              json={'note': 'PRIVATE-SYNTHETIC-CONTENT', 'token': 'fake-secret'})
    transport(client, handler)
    error = WriteOutcomeUnknown if status == 500 else httpx.HTTPStatusError
    with pytest.raises(error) as exc:
        client._write_json('PUT', '/api/test')
    assert 'test-correlation-123' in str(exc.value)
    assert 'PRIVATE' not in str(exc.value)
    assert 'fake-secret' not in str(exc.value)


def test_children_preserves_all_units_groups_and_state(client, monkeypatch):
    units = [
        {'id': 'test-school', 'name': 'Testskole', 'type': 'school', 'isActive': True,
         'groups': [{'id': 'test-group-a', 'name': 'Testgruppe A'},
                    {'id': 'test-group-b', 'name': 'Testgruppe B'}]},
        {'id': 'test-sfo', 'name': 'Test-SFO', 'type': 'afterSchool', 'isActive': True},
        {'id': 'test-childcare', 'name': 'Testbarnehage', 'type': 'childcare', 'isActive': False},
    ]
    monkeypatch.setattr(client, 'web_children', lambda: [
        {'id': 'test-child', 'firstName': 'Testbarn A', 'organizationalUnits': units},
        {'id': 'test-child-without-unit'},
    ])
    rows = client.children()
    assert [r['organizationalUnitId'] for r in rows] == [u['id'] for u in units] + [None]
    assert rows[0]['groups'] == units[0]['groups']
    assert rows[1]['organizationalUnitType'] == 'afterSchool'
    assert rows[2]['organizationalUnit']['isActive'] is False
    assert all(r['childId'] == 'test-child' for r in rows[:3])


def test_paging_and_participants_preserve_raw_state(client):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/participants'):
            assert dict(request.url.params) == {'childPersonId': 'test-child'}
            return httpx.Response(200, json={'recipients': [{'id': 'test-recipient'}]})
        assert request.url.params['pageId'] == '2'
        assert request.url.params['pageSize'] == '7'
        if request.url.path == '/api/message-threads':
            assert request.url.params['includeAfterSchoolMessages'] == 'true'
            return httpx.Response(200, json={'pageId': 2, 'messageThreads': [
                {'threadUid': 'test-thread', 'isDeleted': False}]})
        return httpx.Response(200, json={'pageId': 2, 'isReplyDisabled': True,
                                        'messages': [{'body': 'Test eldre melding'}]})
    transport(client, handler)
    page = client.get_message_threads_page('test-child', page_size=7, page_id=2)
    assert page['pageId'] == 2
    assert page['messageThreads'][0]['isDeleted'] is False
    thread = client.get_thread('test-thread', page_size=7, page_id=2)
    assert thread['isReplyDisabled'] is True
    assert thread['messages'][0]['body'] == 'Test eldre melding'
    assert client.message_participants('test-thread', 'test-child')['recipients'][0]['id'] == 'test-recipient'
    assert calls[-1].url.path == '/api/message-threads/test-thread/participants'


@pytest.mark.parametrize('size,page', [(0, None), (True, None), (1, -1), (1, True), (1, '2')])
def test_invalid_paging_never_reaches_api(client, size, page):
    transport(client, lambda r: pytest.fail('Invalid paging reached API'))
    with pytest.raises(ValueError):
        client.get_thread('test-thread', size, page)


@pytest.mark.parametrize('state', [
    {'threadUid': 'test-thread', 'isReplyDisabled': True},
    {'threadUid': 'test-thread', 'isReplyDisabled': False, 'isDeleted': True},
    {'threadUid': 'test-thread'},
    {'threadUid': 'test-thread', 'isReplyDisabled': None},
    {'threadUid': 'different-thread', 'isReplyDisabled': False},
    {'messageThreadId': 'test-absence', 'messages': []},
])
def test_reply_requires_confirmed_ordinary_replyable_thread(client, state):
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/api/messages/threads/test-thread'
        return httpx.Response(200, json=state)
    transport(client, handler)
    with pytest.raises(ValueError):
        client.reply_message('test-thread', 'Testsvar')


@pytest.mark.parametrize('status', [403, 404, 500])
def test_failed_reply_preflight_never_posts(client, status):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        return httpx.Response(status)
    transport(client, handler)
    with pytest.raises(httpx.HTTPStatusError):
        client.reply_message('test-thread', 'Testsvar')
    assert len(calls) == 1
