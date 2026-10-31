"""Eksplisitt markering som lest/sett (read-status og *-viewings).

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_absence_thread import ABSENCE
from tests.test_consents import OPEN
from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import CHILDREN
from tests.test_registrations import user  # noqa: F401
from vigilo_connector.client import WriteOutcomeUnknown

POST = {'id': 'test-post', 'title': 'Testoppslag', 'childIds': ['test-child'], 'isSeen': False}
ROUTINE = {'id': 'test-routine', 'name': 'Sovetid', 'isSeen': False}


def api(requests, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(200)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/messages/threads/test-thread':
            return httpx.Response(200, json={'threadUid': 'test-thread', 'isReplyDisabled': False})
        if path == '/api/student-absences':
            return httpx.Response(200, json={'pageId': 1, 'absences': [ABSENCE]})
        if path == '/api/posts/test-post':
            return httpx.Response(200, json=POST)
        if path.startswith('/api/routines/'):
            return httpx.Response(200, json={**ROUTINE, 'id': path.rsplit('/', 1)[1]})
        if path == '/api/consent-forms':
            return httpx.Response(200, json={'consentForms': [OPEN], 'consentInformation': None})
        return httpx.Response(404)
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content)


@pytest.mark.parametrize('kind,ids,child,method,path,query,body', [
    ('message_thread', ['test-thread'], None, 'PUT',
     '/api/messages/threads/test-thread/read-status', {}, {'isRead': True}),
    ('absence', ['test-absence'], 'test-child', 'PUT',
     '/api/student-absences/test-absence/read-status', {'organizationalUnitId': 'test-school'},
     {'isRead': True}),
    ('post', ['test-post'], 'test-child', 'POST', '/api/post-viewings', {},
     {'postIds': ['test-post']}),
    ('routine', ['test-r1', 'test-r2'], None, 'POST', '/api/routine-viewings', {},
     {'routineIds': ['test-r1', 'test-r2']}),
    ('consent_form', ['test-form'], 'test-child', 'POST', '/api/consent-form-viewings', {},
     {'clientPersonId': 'test-child', 'consentFormIds': ['test-form']}),
])
def test_marks_match_app_requests(client, kind, ids, child, method, path, query, body):
    requests = []
    transport(client, api(requests))
    result = client.mark_read(kind, ids, child)
    request, sent = writes(requests)
    assert (request.method, request.url.path, dict(request.url.params)) == (method, path, query)
    assert sent == body
    assert result['status'] == 'marked_read'


@pytest.mark.parametrize('kind,ids,child,match', [
    ('unread', ['x'], None, 'kind må vere'),
    ('routine', [], None, 'ikkje-tom'),
    ('routine', ['a', 'a'], None, 'same ID'),
    ('routine', [f'test-{i}' for i in range(51)], None, 'Høgst 50'),
    ('message_thread', ['a', 'b'], None, 'nøyaktig éin'),
    ('message_thread', ['test-other'], None, '404'),
    ('absence', ['test-other'], 'test-child', 'Fann ikkje fråværet'),
    ('absence', ['test-absence'], None, 'child_id'),
    ('post', ['test-post'], None, 'child_id'),
    ('post', ['test-post'], 'test-child-b', 'Fann ikkje oppslaget'),
    ('consent_form', ['test-other-form'], 'test-child', 'Fann ikkje samtykkeskjemaet'),
])
def test_invalid_marks_never_write(client, kind, ids, child, match):
    requests = []
    transport(client, api(requests))
    with pytest.raises((ValueError, httpx.HTTPStatusError), match=match):
        client.mark_read(kind, ids, child)
    assert all(r.method == 'GET' for r in requests)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='isSeen/isRead'):
        client.mark_read('post', ['test-post'], 'test-child')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_reads_never_mark_anything(client):
    requests = []
    transport(client, api(requests))
    client.get_thread('test-thread')
    client.school_absences('test-child')
    client.list_consents('test-child')
    assert all(r.method == 'GET' for r in requests)


def test_mark_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    result = wire('mark_read', {'kind': 'routine', 'ids': ['test-r1']})
    assert result.get('isError') is not True, result
    assert writes(requests)[1] == {'routineIds': ['test-r1']}
    error = visible_error(wire('mark_read', {'kind': 'message_thread', 'ids': ['a', 'b']}))
    assert 'éin' in error
