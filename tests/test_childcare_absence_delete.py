"""Barnehagefråvær: lesing og sletting (DELETE /api/absences/{id}).

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from vigilo_connector.client import WriteOutcomeUnknown

ABSENCE = {'absenceId': 'test-absence', 'childId': 'test-child',
           'organizationalUnitId': 'test-kindergarten', 'fromDate': '2026-12-24T00:00:00',
           'toDate': '2026-12-26T00:00:00', 'isDeletable': True, 'note': None,
           'absenceCode': {'id': 'test-code', 'name': 'Fri'},
           'registeredTime': '2026-10-31T08:00:00Z'}
CHILDREN = [
    {'childId': 'test-child', 'organizationalUnits': [
        {'id': 'test-kindergarten', 'type': 'childcare'}]},
    {'childId': 'test-child-b', 'organizationalUnits': [{'id': 'test-school', 'type': 'school'}]},
]
OTHER = {**ABSENCE, 'absenceId': 'test-other', 'childId': 'test-child-b'}
DETAIL = {**ABSENCE, 'departmentName': 'Testavdeling', 'organizationalUnitName': 'Testbarnehage'}


def api(requests, absences=None, detail=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(204)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/absences':
            return httpx.Response(200, json={'pageId': 1, 'absences': [ABSENCE, OTHER]
                                             if absences is None else absences})
        if path == '/api/absences/test-absence':
            return httpx.Response(200, json=DETAIL if detail is None else detail)
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0]


def test_list_matches_app_query_and_shows_last_day(client):
    requests = []
    transport(client, api(requests))
    result = client.childcare_absences('test-child')
    query = dict(next(r for r in requests if r.url.path == '/api/absences').url.params)
    assert query == {'childIds': 'test-child', 'pageSize': '50'}
    assert result['absences'] == [{
        'absenceId': 'test-absence', 'organizationalUnitId': 'test-kindergarten',
        'fromDate': '2026-12-24', 'lastDay': '2026-12-25', 'absenceCode': 'Fri', 'note': None,
        'isDeletable': True, 'registeredTime': '2026-10-31T08:00:00Z'}]


def test_delete_reads_detail_then_deletes_with_unit_query(client):
    requests = []
    transport(client, api(requests))
    result = client.delete_childcare_absence('test-child', 'test-absence')
    detail = next(r for r in requests if r.url.path == '/api/absences/test-absence')
    assert dict(detail.url.params) == {'organizationalUnitId': 'test-kindergarten'}
    request = writes(requests)
    assert request.method == 'DELETE' and request.url.path == '/api/absences/test-absence'
    assert dict(request.url.params) == {'organizationalUnitId': 'test-kindergarten'}
    assert not request.content
    assert result['status'] == 'deleted' and result['lastDay'] == '2026-12-25'


@pytest.mark.parametrize('child,absence,detail,match', [
    ('test-child', 'test-other', None, 'Fann ikkje fråværet'),
    ('test-unknown', 'test-absence', None, 'Fann ikkje barnet'),
    ('test-child-b', 'test-absence', None, 'inga barnehageeining'),
    ('test-child', ' ', None, 'absence_id'),
    ('test-child', 'test-absence', {**DETAIL, 'isDeletable': False}, 'isDeletable=false'),
    ('test-child', 'test-absence', {**DETAIL, 'isDeletable': None}, 'isDeletable=false'),
    ('test-child', 'test-absence', {**DETAIL, 'childId': 'test-child-b'}, 'høyrer ikkje'),
])
def test_invalid_delete_is_rejected_without_write(client, child, absence, detail, match):
    requests = []
    transport(client, api(requests, detail=detail))
    with pytest.raises(ValueError, match=match):
        client.delete_childcare_absence(child, absence)
    assert all(r.method == 'GET' for r in requests)


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='list_childcare_absences'):
        client.delete_childcare_absence('test-child', 'test-absence')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_delete_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_childcare_absences', 'delete_childcare_absence'} <= names
    result = wire('delete_childcare_absence', {'child_id': 'test-child',
                                               'absence_id': 'test-absence'})
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['status'] == 'deleted'
    assert writes(requests).method == 'DELETE'


def test_not_deletable_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests, detail={**DETAIL, 'isDeletable': False}))
    error = visible_error(wire('delete_childcare_absence', {'child_id': 'test-child',
                                                            'absence_id': 'test-absence'}))
    assert 'isDeletable' in error and all(r.method == 'GET' for r in requests)
