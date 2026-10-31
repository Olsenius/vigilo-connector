"""Skulefråvær: meldingstråd og svar (POST /api/student-absence-message-threads/{id}/reply).

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import CHILDREN
from vigilo_connector.client import WriteOutcomeUnknown

ABSENCE = {'absenceId': 'test-absence', 'absenceType': 'school', 'childId': 'test-child',
           'organizationalUnitId': 'test-school', 'fromDate': '2026-12-21T00:00:00',
           'toDate': '2026-12-22T00:00:00', 'isEditable': True,
           'messageThreadType': 'absenceMessage', 'relatedMessageThreadId': 'test-thread',
           'lastReplyDate': None, 'timeCreated': '2026-10-31T08:00:00Z'}
# Detaljsvaret (live): barnet i student.id, isReplyDisabled og trådflagg.
DETAIL = {'absenceId': 'test-absence', 'organizationalUnitId': 'test-school',
          'messageThreadType': 'absenceMessage', 'relatedMessageThreadId': 'test-thread',
          'threadUId': 'test-thread', 'isReplyDisabled': False, 'isMessageThreadDeleted': False,
          'student': {'id': 'test-child'}}
THREAD = {'pageId': 1, 'absenceId': 'test-absence', 'messageThreadId': 'test-thread',
          'organizationalUnitId': 'test-school', 'isReplyDisabled': False, 'isEditable': True,
          'absenceDeletedAt': None, 'numberOfParticipants': 3, 'type': 'absenceMessage',
          'child': {'id': 'test-child', 'firstName': 'Testbarn A'},
          'messages': [
              {'id': 'test-m1', 'type': 'absenceCreated', 'createdAt': '2026-10-31T08:00:00Z',
               'title': 'Fråvær', 'text': 'Sjuk', 'attachments': [],
               'sender': {'person': {'id': 'test-parent', 'firstName': 'Test', 'lastName': 'Forelder'}}},
              {'id': 'test-m2', 'type': 'message', 'createdAt': '2026-10-31T09:00:00Z',
               'title': None, 'text': 'God betring', 'attachments': [{'id': 'test-file'}],
               'sender': {'person': {'id': 'test-teacher', 'firstName': 'Test', 'lastName': 'Lærar'}}},
          ]}


def api(requests, detail=None, thread=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(200)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/student-absences':
            return httpx.Response(200, json={'pageId': 1, 'absences': [ABSENCE]})
        if path == '/api/student-absences/test-absence':
            return httpx.Response(200, json=DETAIL if detail is None else detail)
        if path == '/api/student-absence-message-threads/test-thread':
            return httpx.Response(200, json=THREAD if thread is None else thread)
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content)


def test_school_absences_expose_thread_id(client):
    transport(client, api([]))
    absence = client.school_absences('test-child')['absences'][0]
    assert absence['messageThreadId'] == 'test-thread'


def test_thread_matches_app_query(client):
    requests = []
    transport(client, api(requests))
    result = client.absence_thread('test-child', 'test-absence')
    query = dict(next(r for r in requests
                      if r.url.path == '/api/student-absence-message-threads/test-thread').url.params)
    assert query == {'organizationalUnitId': 'test-school', 'pageSize': '20'}
    assert result['canReply'] is True and result['messageThreadId'] == 'test-thread'
    assert [(m['id'], m['sender'], m['attachmentCount']) for m in result['messages']] == [
        ('test-m1', 'Test Forelder', 0), ('test-m2', 'Test Lærar', 1)]
    assert 'test-file' not in json.dumps(result)


def test_thread_page_id_is_passed(client):
    requests = []
    transport(client, api(requests))
    client.absence_thread('test-child', 'test-absence', page_id=2)
    thread = next(r for r in requests if 'message-threads' in r.url.path)
    assert thread.url.params['pageId'] == '2'


def test_reply_matches_app_body(client):
    requests = []
    transport(client, api(requests))
    result = client.reply_absence_thread('test-child', 'test-absence', 'Takk')
    request, body = writes(requests)
    assert request.method == 'POST'
    assert request.url.path == '/api/student-absence-message-threads/test-thread/reply'
    # Som SimpleMessageBody; appen sender ingen einingsparameter på svaret.
    assert body == {'body': 'Takk', 'attachments': []}
    assert dict(request.url.params) == {}
    assert result['status'] == 'sent'


@pytest.mark.parametrize('args,detail,thread,match', [
    ({'body': ' '}, None, None, 'body'),
    ({'absence_id': 'test-other'}, None, None, 'Fann ikkje fråværet'),
    ({'child_id': 'test-unknown'}, None, None, 'Fann ikkje barnet'),
    ({}, {**DETAIL, 'student': {'id': 'test-child-c'}}, None, 'høyrer ikkje til barnet'),
    ({}, {**DETAIL, 'relatedMessageThreadId': None}, None, 'ingen meldingstråd'),
    ({}, {**DETAIL, 'isReplyDisabled': True}, None, 'tillèt ikkje svar'),
    ({}, {**DETAIL, 'isReplyDisabled': None}, None, 'tillèt ikkje svar'),
    ({}, {**DETAIL, 'messageThreadType': 'absenceNotificationMessage'}, None, 'tillèt ikkje svar'),
    ({}, {**DETAIL, 'isMessageThreadDeleted': True}, None, 'tillèt ikkje svar'),
    ({}, None, {**THREAD, 'isReplyDisabled': True}, 'tillèt ikkje svar'),
    ({}, None, {**THREAD, 'absenceDeletedAt': '2026-10-31T10:00:00Z'}, 'tillèt ikkje svar'),
    ({}, None, {**THREAD, 'child': {'id': 'test-child-b'}}, 'høyrer ikkje til fråværet'),
    ({}, None, {**THREAD, 'absenceId': 'test-other'}, 'høyrer ikkje til fråværet'),
])
def test_invalid_reply_is_rejected_without_write(client, args, detail, thread, match):
    requests = []
    transport(client, api(requests, detail=detail, thread=thread))
    with pytest.raises(ValueError, match=match):
        client.reply_absence_thread(**{'child_id': 'test-child', 'absence_id': 'test-absence',
                                       'body': 'Takk', **args})
    assert all(r.method == 'GET' for r in requests)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='Meldingen kan være sendt'):
        client.reply_absence_thread('test-child', 'test-absence', 'Takk')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_reply_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'get_absence_thread', 'reply_absence_thread'} <= names
    result = wire('reply_absence_thread', {'child_id': 'test-child', 'absence_id': 'test-absence',
                                           'body': 'Takk'})
    assert result.get('isError') is not True, result
    assert writes(requests)[1]['body'] == 'Takk'


def test_reply_disabled_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests, detail={**DETAIL, 'isReplyDisabled': True}))
    error = visible_error(wire('reply_absence_thread', {
        'child_id': 'test-child', 'absence_id': 'test-absence', 'body': 'Takk'}))
    assert 'svar' in error and all(r.method == 'GET' for r in requests)
