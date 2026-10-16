"""Lesing av hentemeldingar (check-in-notes); berre syntetiske data."""
import json
from datetime import datetime

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from vigilo_connector import client as client_module

CHILDREN = [
    {'childId': 'test-child', 'firstName': 'Testbarn A', 'organizationalUnits': [
        {'id': 'test-school', 'name': 'Testskule', 'type': 'school'},
        {'id': 'test-sfo', 'name': 'Test-SFO', 'type': 'afterSchool'},
    ]},
    {'childId': 'test-child-b', 'firstName': 'Testbarn B', 'organizationalUnits': [
        {'id': 'test-school', 'name': 'Testskule', 'type': 'school'},
    ]},
]
NOTE = {'id': 'test-note', 'category': 'afterSchoolCheckOut', 'childPersonId': 'test-child',
        # Observert wire-format: lokale datoar utan sone, førekomstar som UTC for midnatt i Oslo.
        'fromDate': '2026-10-05T00:00:00', 'toDate': '2026-10-06T00:00:00',
        'dateOccurrences': ['2026-10-04T22:00:00Z'],
        'recurrencePattern': {'recurrenceType': 'daily', 'interval': 1, 'daysOfWeek': None},
        'timeOfDay': '14:30:00', 'noteText': 'Testhenting', 'isRecurring': False,
        'organizationalUnitId': 'test-sfo', 'canUpdate': True, 'canDelete': True,
        'type': {'id': 'test-type', 'name': 'Hentes av', 'useComment': True},
        'registeredBy': {'personId': 'test-guardian', 'firstName': 'Test', 'lastName': 'Føresett'}}


def api(requests, notes=None, config=None, types=None):
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert 'userId' not in request.url.params
        path = request.url.path
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/check-in-notes':
            return httpx.Response(200, json={'items': [NOTE] if notes is None else notes})
        if path == '/api/check-in-note-configuration':
            return httpx.Response(200, json=config or {'enabledCategories': ['afterSchoolCheckOut']})
        if path == '/api/check-in-note-types':
            return httpx.Response(200, json={'items': types or [
                {'id': 'test-type', 'name': 'Hentes av', 'category': 'afterSchoolCheckOut',
                 'useComment': True, 'isActive': True, 'isDeleted': False},
                {'id': 'test-general', 'name': 'Generell', 'category': 'general',
                 'useComment': False, 'isActive': True, 'isDeleted': False},
            ]})
        if path == '/api/check-in-note-configuration/registration-deadlines':
            return httpx.Response(200, json={
                'organizationalUnitId': request.url.params['organizationalUnitId'],
                'noteCategory': request.url.params['noteCategory'],
                'numberOfDaysBefore': 0, 'timeOfDay': '12:00'})
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def test_month_uses_app_range_with_exclusive_end(client):
    requests = []
    transport(client, api(requests))
    result = client.pickup_notes('test-child', '2026-12')
    notes = [r for r in requests if r.url.path == '/api/check-in-notes']
    assert len(notes) == 1
    assert dict(notes[0].url.params) == {
        'childPersonId': 'test-child', 'fromDate': '2026-12-01', 'toDate': '2027-01-01'}
    assert 'x-organizational-unit-id' not in notes[0].headers
    assert result['notes'] == [{**NOTE, 'typeAbbreviation': 'HA'}]
    assert result['afterSchoolUnits'] == [{'id': 'test-sfo', 'name': 'Test-SFO'}]
    assert result['toDateExclusive'] == '2027-01-01'


def test_default_month_is_current_oslo_month(client, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            assert str(tz) == 'Europe/Oslo'
            return datetime(2026, 11, 1, 0, 30, tzinfo=tz)
    monkeypatch.setattr(client_module, 'datetime', Clock)
    requests = []
    transport(client, api(requests))
    assert client.pickup_notes('test-child')['month'] == '2026-11'


def test_child_without_after_school_unit_makes_no_note_request(client):
    requests = []
    transport(client, api(requests))
    result = client.pickup_notes('test-child-b', '2026-10')
    assert result['notes'] == [] and result['afterSchoolUnits'] == []
    assert [r.url.path for r in requests] == ['/api/children']


@pytest.mark.parametrize('month', ['2026-13', '2026-1', '26-10', '2026-10-01', 9999, '9999-12'])
def test_invalid_month_is_rejected_before_any_request(client, month):
    requests = []
    transport(client, api(requests))
    with pytest.raises(ValueError):
        client.pickup_notes('test-child', month)
    assert requests == []


def test_unknown_child_is_rejected_before_note_request(client):
    requests = []
    transport(client, api(requests))
    with pytest.raises(ValueError, match='list_children'):
        client.pickup_notes('test-unknown', '2026-10')
    assert [r.url.path for r in requests] == ['/api/children']


def test_invalid_note_response_is_an_error(client):
    requests = []
    transport(client, api(requests, notes='not-a-list'))
    with pytest.raises(ValueError, match='Ugyldig'):
        client.pickup_notes('test-child', '2026-10')


def test_options_use_unit_header_and_deadline_per_enabled_category(client):
    requests = []
    transport(client, api(requests))
    result = client.pickup_note_options('test-child', 'test-sfo')
    config = next(r for r in requests if r.url.path == '/api/check-in-note-configuration')
    assert config.headers['x-organizational-unit-id'] == 'test-sfo'
    assert config.headers['authorization'] == 'Bearer old'
    types = next(r for r in requests if r.url.path == '/api/check-in-note-types')
    assert dict(types.url.params) == {}
    deadlines = [r for r in requests if r.url.path.endswith('/registration-deadlines')]
    assert [dict(r.url.params) for r in deadlines] == [
        {'organizationalUnitId': 'test-sfo', 'noteCategory': 'afterSchoolCheckOut'}]
    assert result['enabledCategories'] == ['afterSchoolCheckOut']
    assert {t['id']: t['categoryEnabled'] for t in result['types']} == {
        'test-type': True, 'test-general': False}
    assert result['registrationDeadlines'][0]['timeOfDay'] == '12:00'


@pytest.mark.parametrize('unit', ['test-school', 'test-other', ''])
def test_options_require_after_school_unit_of_child(client, unit):
    requests = []
    transport(client, api(requests))
    with pytest.raises(ValueError):
        client.pickup_note_options('test-child', unit)
    assert all(r.url.path == '/api/children' for r in requests)


def test_unit_header_cannot_override_authentication(client):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={})
    transport(client, handler)
    client.get_json('/api/test', headers={'Authorization': 'Bearer evil', 'X-Test': 'ok'})
    assert seen[0].headers['authorization'] == 'Bearer old'
    assert seen[0].headers['x-test'] == 'ok'


def test_pickup_tools_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_pickup_notes', 'pickup_note_options'} <= names
    result = wire('list_pickup_notes', {'child_id': 'test-child', 'month': '2026-10'})
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['notes'][0]['id'] == 'test-note'
    result = wire('pickup_note_options', {'child_id': 'test-child', 'organizational_unit_id': 'test-sfo'})
    assert json.loads(result['content'][0]['text'])['enabledCategories'] == ['afterSchoolCheckOut']
    assert all(r.method == 'GET' for r in requests)


def test_invalid_month_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    assert 'YYYY-MM' in visible_error(wire('list_pickup_notes', {'child_id': 'test-child', 'month': '2026-13'}))
    assert requests == []


WEEKLY = {**NOTE, 'id': 'test-weekly', 'isRecurring': True,
          'fromDate': '2026-10-01T00:00:00', 'toDate': '2027-01-01T00:00:00',
          # 26.10. etter overgang til vintertid: midnatt i Oslo er 23:00 UTC.
          'dateOccurrences': ['2026-10-04T22:00:00Z', '2026-10-25T23:00:00Z']}


@pytest.mark.parametrize('day,expected', [
    ('2026-10-05', ['test-note', 'test-weekly']),
    ('2026-10-26', ['test-weekly']),
    ('2026-10-04', []),
    ('2026-10-25', []),
])
def test_day_filter_uses_oslo_local_occurrence_date(client, day, expected):
    requests = []
    transport(client, api(requests, notes=[NOTE, WEEKLY]))
    result = client.pickup_notes('test-child', day=day)
    assert [note['id'] for note in result['notes']] == expected
    assert result['day'] == day and result['month'] == '2026-10'
    notes = [r for r in requests if r.url.path == '/api/check-in-notes']
    assert notes[0].url.params['fromDate'] == '2026-10-01'


@pytest.mark.parametrize('day,month', [('2026-10-32', None), ('16.10.2026', None),
                                       ('2026-10-16', '2026-11')])
def test_invalid_day_is_rejected_before_any_request(client, day, month):
    requests = []
    transport(client, api(requests))
    with pytest.raises(ValueError):
        client.pickup_notes('test-child', month, day)
    assert requests == []


@pytest.mark.parametrize('name,code', [
    ('Går hjem selv til avtalt klokkeslett', 'GHS'), ('Bestemmer selv sluttid', 'BSS'),
    ('Blir hentet', 'BH'), ('Henting', 'HEN'), ('  Blir   hentet ', 'BH'), ('Ab', 'AB'),
])
def test_type_abbreviation_matches_app(name, code):
    assert client_module.VigiloClient._pickup_abbreviation(name) == code


def test_day_marks_last_updated_note_active_like_app(client):
    # Observert i appen: nyare fast melding overstyrer eldre fast, nyare enkeltdag begge.
    old_weekly = {**WEEKLY, 'id': 'test-old', 'timeOfDay': '15:30:00',
                  'lastUpdatedTime': '2026-08-01T08:00:00Z'}
    new_weekly = {**WEEKLY, 'id': 'test-new', 'timeOfDay': '15:00:00',
                  'lastUpdatedTime': '2026-09-01T08:00:00Z'}
    single = {**NOTE, 'id': 'test-single', 'lastUpdatedTime': '2026-10-01T08:00:00Z',
              'type': {'id': 'test-bss', 'name': 'Bestemmer selv sluttid'}}
    for notes, expected in (([old_weekly, new_weekly], 'test-new'),
                            ([single, old_weekly, new_weekly], 'test-single')):
        transport(client, api([], notes=[dict(n) for n in notes]))
        result = client.pickup_notes('test-child', day='2026-10-05')
        assert result['activeNoteId'] == expected
        assert [n['id'] for n in result['notes'] if n['active']] == [expected]
    assert {n['id']: n['typeAbbreviation'] for n in result['notes']} == {
        'test-single': 'BSS', 'test-old': 'HA', 'test-new': 'HA'}


def test_tie_and_missing_update_time_keep_first_note(client):
    a = {**NOTE, 'id': 'test-a', 'lastUpdatedTime': '2026-10-01T10:00:00Z'}
    b = {**NOTE, 'id': 'test-b', 'lastUpdatedTime': '2026-10-01T10:00:00Z'}
    c = {**NOTE, 'id': 'test-c', 'lastUpdatedTime': None}
    transport(client, api([], notes=[a, b, c]))
    result = client.pickup_notes('test-child', day='2026-10-05')
    assert result['activeNoteId'] == 'test-a'


def test_month_view_has_codes_but_no_active_flag(client):
    transport(client, api([]))
    note = client.pickup_notes('test-child', '2026-10')['notes'][0]
    assert note['typeAbbreviation'] == 'HA' and 'active' not in note
