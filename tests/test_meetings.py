"""Møteinvitasjonar: lesing, val av tidspunkt og avmelding (forms/meetings).

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import CHILDREN
from tests.test_registrations import user  # noqa: F401
from vigilo_connector.client import WriteOutcomeUnknown

MEETING = {
    'id': 'test-meeting', 'title': 'Testsamtale', 'description': 'Syntetisk',
    'location': 'Rom 1', 'meetingType': 'developmentTalk', 'organizationalUnitId': 'test-school',
    'organizationalUnitName': 'Testskule', 'publicationTime': '2026-10-01T08:00:00Z',
    'registrationDeadline': '2099-01-01T00:00:00Z', 'attachments': [], 'reply': None,
    'createdByPerson': {'personId': 'test-employee', 'firstName': 'Test', 'lastName': 'Lærar'},
    'meetingOccurrences': [
        {'meetingOccurrenceId': 'test-occ-1', 'startTime': '2099-01-10T08:00:00Z',
         'endTime': '2099-01-10T08:20:00Z', 'isAvailable': True},
        {'meetingOccurrenceId': 'test-occ-2', 'startTime': '2099-01-10T08:20:00Z',
         'endTime': '2099-01-10T08:40:00Z', 'isAvailable': False},
    ]}
REPLIED = {**MEETING, 'reply': {'meetingOccurrenceId': 'test-occ-2',
                                'replyTime': '2026-10-31T08:00:00Z',
                                'confirmationSentTime': None}}


def api(requests, meetings=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(200)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/forms/meetings/invitations/test-child':
            return httpx.Response(200, json={'items': [MEETING] if meetings is None else meetings})
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content) if found[0].content else None


def test_list_marks_open_and_closed_meetings(client):
    closed = {**MEETING, 'id': 'test-closed', 'registrationDeadline': '2000-01-01T00:00:00Z'}
    no_deadline = {**MEETING, 'id': 'test-open', 'registrationDeadline': None}
    transport(client, api([], meetings=[MEETING, closed, no_deadline]))
    result = client.meeting_invitations('test-child')
    # Som MeetingDetailsScreenKt: utan frist er invitasjonen open.
    assert [m['canReply'] for m in result['meetings']] == [True, False, True]


def test_reply_matches_app_body(client):
    requests = []
    transport(client, api(requests))
    result = client.reply_meeting('test-child', 'test-meeting', 'test-occ-1')
    request, body = writes(requests)
    assert request.method == 'POST'
    assert request.url.path == '/api/forms/meetings/test-meeting/replies'
    assert body == {'childPersonId': 'test-child', 'inviteePersonId': 'test-guardian',
                    'meetingOccurrenceId': 'test-occ-1'}
    assert result['status'] == 'replied' and result['replacedPreviousReply'] is False


def test_reply_replaces_previous_choice(client):
    requests = []
    transport(client, api(requests, meetings=[{**REPLIED, 'reply': {
        'meetingOccurrenceId': 'test-occ-2'}}]))
    result = client.reply_meeting('test-child', 'test-meeting', 'test-occ-1')
    assert result['replacedPreviousReply'] is True


def test_withdraw_uses_user_and_child_path(client):
    requests = []
    transport(client, api(requests, meetings=[REPLIED]))
    result = client.withdraw_meeting_reply('test-child', 'test-meeting')
    request, body = writes(requests)
    assert request.method == 'DELETE'
    assert request.url.path == '/api/forms/meetings/test-meeting/replies/test-guardian/test-child'
    assert body is None and result['status'] == 'withdrawn'


@pytest.mark.parametrize('action,meetings,args,match', [
    ('reply_meeting', None, {'occurrence_id': 'test-occ-2'}, 'ikkje ledig'),
    ('reply_meeting', [REPLIED], {'occurrence_id': 'test-occ-2'}, 'alt valt'),
    ('reply_meeting', None, {'occurrence_id': 'test-unknown'}, 'Ukjent tidspunkt'),
    ('reply_meeting', None, {'occurrence_id': ' '}, 'occurrence_id'),
    ('reply_meeting', [{**MEETING, 'registrationDeadline': '2000-01-01T00:00:00Z'}], {},
     'Fristen'),
    ('reply_meeting', None, {'meeting_id': 'test-other'}, 'Fann ikkje møteinvitasjonen'),
    ('reply_meeting', None, {'child_id': 'test-unknown'}, 'Fann ikkje barnet'),
    ('withdraw_meeting_reply', None, {}, 'ikkje noko svar'),
    ('withdraw_meeting_reply', [{**REPLIED, 'registrationDeadline': '2000-01-01T00:00:00Z'}],
     {}, 'Fristen'),
])
def test_invalid_action_is_rejected_without_write(client, action, meetings, args, match):
    requests = []
    transport(client, api(requests, meetings=meetings))
    call = {'child_id': 'test-child', 'meeting_id': 'test-meeting'}
    if action == 'reply_meeting':
        call['occurrence_id'] = 'test-occ-1'
    with pytest.raises(ValueError, match=match):
        getattr(client, action)(**{**call, **args})
    assert all(r.method == 'GET' for r in requests)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='list_meeting_invitations'):
        client.reply_meeting('test-child', 'test-meeting', 'test-occ-1')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_reply_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_meeting_invitations', 'reply_meeting', 'withdraw_meeting_reply'} <= names
    result = wire('reply_meeting', {'child_id': 'test-child', 'meeting_id': 'test-meeting',
                                    'occurrence_id': 'test-occ-1'})
    assert result.get('isError') is not True, result
    assert writes(requests)[1]['meetingOccurrenceId'] == 'test-occ-1'


def test_unavailable_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    error = visible_error(wire('reply_meeting', {'child_id': 'test-child',
                                                 'meeting_id': 'test-meeting',
                                                 'occurrence_id': 'test-occ-2'}))
    assert 'ledig' in error and all(r.method == 'GET' for r in requests)
