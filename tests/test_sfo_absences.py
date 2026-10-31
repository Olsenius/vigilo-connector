"""SFO-fråvær: lesing, registrering (POST /api/after-school-absences) og sletting.

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import CHILDREN
from vigilo_connector.client import WriteOutcomeUnknown

TWO_SFO = [{'childId': 'test-child', 'organizationalUnits': [
    {'id': 'test-sfo', 'type': 'afterSchool'}, {'id': 'test-sfo-2', 'type': 'afterSchool'}]}]
SFO_ABSENCE = {'absenceId': 'test-absence', 'absenceType': 'afterSchool', 'childId': 'test-child',
               'organizationalUnitId': 'test-sfo', 'fromDate': '2026-11-06T00:00:00',
               'toDate': '2026-11-07T00:00:00', 'note': 'Testmerknad', 'isDeletable': True,
               'timeCreated': '2026-10-31T08:00:00Z'}
# Detaljsvaret (live): barnet i student.id, utan childId og absenceType.
SFO_DETAIL = {'absenceId': 'test-absence', 'fromDate': '2026-11-06T00:00:00',
              'toDate': '2026-11-07T00:00:00', 'isDeletable': True, 'note': None,
              'organizationalUnitId': 'test-sfo', 'timeCreated': '2026-10-31T08:00:00Z',
              'student': {'id': 'test-child', 'firstName': 'Testbarn A'}}
SCHOOL_ABSENCE = {**SFO_ABSENCE, 'absenceId': 'test-school-absence', 'absenceType': 'school',
                  'organizationalUnitId': 'test-school'}


def api(requests, children=None, absences=None, detail=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(201)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN if children is None else children)
        if path == '/api/student-absences':
            return httpx.Response(200, json={'pageId': 1, 'absences': [SFO_ABSENCE, SCHOOL_ABSENCE]
                                             if absences is None else absences})
        if path == '/api/after-school-absences/test-absence':
            return httpx.Response(200, json=SFO_DETAIL if detail is None else detail)
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content) if found[0].content else None


def test_list_keeps_only_sfo_and_shows_last_day(client):
    requests = []
    transport(client, api(requests))
    result = client.sfo_absences('test-child')
    query = dict(next(r for r in requests if r.url.path == '/api/student-absences').url.params)
    assert query == {'studentIds': 'test-child', 'pageSize': '50',
                     'includeAfterSchoolAbsences': 'true'}
    assert result['organizationalUnitId'] == 'test-sfo'
    assert result['absences'] == [{
        'absenceId': 'test-absence', 'fromDate': '2026-11-06', 'lastDay': '2026-11-06',
        'note': 'Testmerknad', 'isDeletable': True, 'timeCreated': '2026-10-31T08:00:00Z'}]


def test_register_matches_app_body(client):
    requests = []
    transport(client, api(requests))
    result = client.register_sfo_absence('test-child', '2026-11-06', '2026-11-07', 'Testmerknad')
    request, body = writes(requests)
    assert request.method == 'POST' and request.url.path == '/api/after-school-absences'
    assert 'x-organizational-unit-id' not in request.headers
    # Som CreateSfoAbsenceRequestApiModel: dd.MM.yyyy og eksklusiv sluttdato.
    assert body == {'childId': 'test-child', 'organizationalUnitId': 'test-sfo',
                    'fromDate': '06.11.2026', 'toDate': '08.11.2026', 'note': 'Testmerknad'}
    assert result['status'] == 'registered' and result['toDate'] == '2026-11-07'


def test_register_without_note_omits_it(client):
    requests = []
    transport(client, api(requests))
    client.register_sfo_absence('test-child', '2026-11-06', '2026-11-06')
    assert 'note' not in writes(requests)[1]


@pytest.mark.parametrize('args,children,match', [
    ({'child_id': 'test-child-b'}, None, 'inga SFO-eining'),
    ({'child_id': 'test-unknown'}, None, 'Fann ikkje barnet'),
    ({}, TWO_SFO, 'fleire SFO-einingar'),
    ({'organizational_unit_id': 'test-school'}, None, 'ikkje ei SFO-eining'),
    ({'to_date': '2026-11-05'}, None, 'før from_date'),
    ({'from_date': '06.11.2026'}, None, 'YYYY-MM-DD'),
    ({'note': 5}, None, 'note'),
])
def test_invalid_register_is_rejected_without_write(client, args, children, match):
    requests = []
    transport(client, api(requests, children=children))
    with pytest.raises(ValueError, match=match):
        client.register_sfo_absence(**{'child_id': 'test-child', 'from_date': '2026-11-06',
                                       'to_date': '2026-11-06', **args})
    assert all(r.method == 'GET' for r in requests)


def test_explicit_unit_picks_among_several(client):
    requests = []
    transport(client, api(requests, children=TWO_SFO))
    client.register_sfo_absence('test-child', '2026-11-06', '2026-11-06',
                                organizational_unit_id='test-sfo-2')
    assert writes(requests)[1]['organizationalUnitId'] == 'test-sfo-2'


@pytest.mark.parametrize('code,match', [
    ('absence_dates_overlapped_by_absence', 'overlappar'),
    ('absence_dates_overlapped_by_checkin', 'sjekka inn'),
])
def test_overlap_errors_are_explained(client, code, match):
    transport(client, api([], write=lambda r: httpx.Response(400, json={
        'type': 'validation', 'errorCode': code, 'message': 'privat tekst'})))
    with pytest.raises(ValueError, match=match) as error:
        client.register_sfo_absence('test-child', '2026-11-06', '2026-11-06')
    assert 'privat' not in str(error.value)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='fraværsoversikten'):
        client.register_sfo_absence('test-child', '2026-11-06', '2026-11-06')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_delete_uses_unit_query(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(204)))
    result = client.delete_sfo_absence('test-child', 'test-absence')
    detail = next(r for r in requests if r.url.path == '/api/after-school-absences/test-absence')
    assert dict(detail.url.params) == {'organizationalUnitId': 'test-sfo'}
    request, body = writes(requests)
    assert request.method == 'DELETE' and request.url.path == '/api/absences/test-absence'
    assert dict(request.url.params) == {'organizationalUnitId': 'test-sfo'}
    assert body is None and result['status'] == 'deleted'


@pytest.mark.parametrize('detail,match', [
    ({**SFO_DETAIL, 'isDeletable': False}, 'isDeletable=false'),
    ({**SFO_DETAIL, 'student': {'id': 'test-child-c'}}, 'høyrer ikkje'),
    ({**SFO_DETAIL, 'student': None, 'childId': 'test-child'}, 'høyrer ikkje'),
])
def test_invalid_delete_is_rejected_without_write(client, detail, match):
    requests = []
    transport(client, api(requests, detail=detail))
    with pytest.raises(ValueError, match=match):
        client.delete_sfo_absence('test-child', 'test-absence')
    assert all(r.method == 'GET' for r in requests)


def test_register_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_sfo_absences', 'register_sfo_absence', 'delete_sfo_absence'} <= names
    result = wire('register_sfo_absence', {'child_id': 'test-child', 'from_date': '2026-11-06',
                                           'to_date': '2026-11-06'})
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['status'] == 'registered'
    assert writes(requests)[1]['toDate'] == '07.11.2026'


def test_child_without_sfo_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    error = visible_error(wire('register_sfo_absence', {
        'child_id': 'test-child-b', 'from_date': '2026-11-06', 'to_date': '2026-11-06'}))
    assert 'SFO-eining' in error and all(r.method == 'GET' for r in requests)
