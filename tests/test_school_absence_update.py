"""Skulefråvær: lesing og endring (PUT /api/student-absences/{id}).

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
           'toDate': '2026-12-23T00:00:00', 'isEditable': True,
           'messageThreadType': 'absenceMessage', 'timeCreated': '2026-10-31T08:00:00Z'}
FROM_SCHOOL = {**ABSENCE, 'absenceId': 'test-notice',
               'messageThreadType': 'absenceNotificationMessage'}
# Detaljsvaret (live): barnet i student.id, utan childId og absenceType.
DETAILS = {aid: {k: v for k, v in {**a, 'student': {'id': 'test-child'}}.items()
                 if k not in ('childId', 'absenceType')}
           for aid, a in (('test-absence', ABSENCE), ('test-notice', FROM_SCHOOL))}
SFO = {**ABSENCE, 'absenceId': 'test-sfo-absence', 'absenceType': 'afterSchool',
       'organizationalUnitId': 'test-sfo'}


def api(requests, absences=None, detail=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(204)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/student-absences':
            return httpx.Response(200, json={'pageId': 1, 'absences': [ABSENCE, FROM_SCHOOL, SFO]
                                             if absences is None else absences})
        if path.startswith('/api/student-absences/'):
            absence_id = path.rsplit('/', 1)[1]
            found = DETAILS[absence_id]
            return httpx.Response(200, json=found if detail is None else detail)
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content) if found[0].content else None


def test_list_keeps_school_absences_with_edit_flags(client):
    requests = []
    transport(client, api(requests))
    result = client.school_absences('test-child')
    query = dict(next(r for r in requests if r.url.path == '/api/student-absences').url.params)
    assert query == {'studentIds': 'test-child', 'pageSize': '50',
                     'includeAfterSchoolAbsences': 'false'}
    assert [(a['absenceId'], a['lastDay'], a['canEdit'], a['fromSchool'])
            for a in result['absences']] == [
        ('test-absence', '2026-12-22', True, False), ('test-notice', '2026-12-22', False, True)]


def test_update_matches_app_body(client):
    requests = []
    transport(client, api(requests))
    result = client.update_student_absence('test-child', 'test-absence', '2026-12-21', '2026-12-23')
    detail = next(r for r in requests if r.url.path == '/api/student-absences/test-absence')
    assert dict(detail.url.params) == {'organizationalUnitId': 'test-school'}
    request, body = writes(requests)
    assert request.method == 'PUT' and request.url.path == '/api/student-absences/test-absence'
    # Som UpdateStudentAbsenceRequest: yyyy-MM-dd (ikkje dd.MM.yyyy) og eksklusiv toDate.
    assert body == {'organizationalUnitId': 'test-school',
                    'fromDate': '2026-12-21', 'toDate': '2026-12-24'}
    assert result['status'] == 'updated'
    assert result['previous'] == {'fromDate': '2026-12-21', 'lastDay': '2026-12-22'}


@pytest.mark.parametrize('args,detail,match', [
    ({'absence_id': 'test-notice'}, None, 'fråværsmelding frå skulen'),
    ({}, {**DETAILS['test-absence'], 'isEditable': False}, 'isEditable=false'),
    ({}, {**DETAILS['test-absence'], 'student': {'id': 'test-child-c'}}, 'høyrer ikkje'),
    ({}, {**ABSENCE}, 'høyrer ikkje'),   # childId utan student godtakast ikkje
    ({'absence_id': 'test-sfo-absence'}, None, 'Fann ikkje fråværet'),
    ({'to_date': '2026-12-20'}, None, 'før from_date'),
    ({'from_date': '21.12.2026'}, None, 'YYYY-MM-DD'),
    ({'child_id': 'test-unknown'}, None, 'Fann ikkje barnet'),
])
def test_invalid_update_is_rejected_without_write(client, args, detail, match):
    requests = []
    transport(client, api(requests, detail=detail))
    with pytest.raises(ValueError, match=match):
        client.update_student_absence(**{'child_id': 'test-child', 'absence_id': 'test-absence',
                                         'from_date': '2026-12-21', 'to_date': '2026-12-22',
                                         **args})
    assert all(r.method == 'GET' for r in requests)


def test_overlap_is_explained(client):
    transport(client, api([], write=lambda r: httpx.Response(400, json={
        'type': 'validation', 'validationErrors': {'dateRangeOverlappingErrorCode': {
            'errorCode': 'overlap', 'message': '[privat;tekst]'}}})))
    with pytest.raises(ValueError, match='overlappar') as error:
        client.update_student_absence('test-child', 'test-absence', '2026-12-21', '2026-12-22')
    assert 'privat' not in str(error.value)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='list_school_absences'):
        client.update_student_absence('test-child', 'test-absence', '2026-12-21', '2026-12-22')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_update_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_school_absences', 'update_student_absence'} <= names
    result = wire('update_student_absence', {'child_id': 'test-child', 'absence_id': 'test-absence',
                                             'from_date': '2026-12-21', 'to_date': '2026-12-21'})
    assert result.get('isError') is not True, result
    assert writes(requests)[1]['toDate'] == '2026-12-22'


def test_not_editable_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    error = visible_error(wire('update_student_absence', {
        'child_id': 'test-child', 'absence_id': 'test-notice',
        'from_date': '2026-12-21', 'to_date': '2026-12-21'}))
    assert 'endre' in error and all(r.method == 'GET' for r in requests)
