"""Endring og sletting av hentemeldingar (PUT/DELETE /api/check-in-notes/{id}).

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import NOTE, api
from vigilo_connector import client as client_module
from vigilo_connector.client import WriteOutcomeUnknown

SERIES = {**NOTE, 'id': 'test-series', 'fromDate': '2026-12-28T00:00:00',
          'toDate': '2027-01-01T00:00:00', 'isRecurring': True, 'timeOfDay': '14:15:00',
          'recurrencePattern': {'recurrenceType': 'weekly', 'interval': 1,
                                'daysOfWeek': ['monday', 'tuesday', 'wednesday', 'thursday']},
          'canUpdate': True, 'canUpdateToDate': True, 'canDelete': True}


def write_api(requests, notes=None, write=None, deadline=None):
    read = api([], notes=[SERIES] if notes is None else notes)
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        if request.method in ('PUT', 'DELETE'):
            return write(request) if write else httpx.Response(204)
        if deadline is not None and request.url.path.endswith('/registration-deadlines'):
            return httpx.Response(200, json=deadline)
        return read(request)
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content) if found[0].content else None


def update(client, **changes):
    args = dict(child_id='test-child', note_id='test-series', month='2026-12')
    args.update(changes)
    return client.update_pickup_note(**args)


def test_unchanged_fields_keep_series_values(client):
    requests = []
    transport(client, write_api(requests))
    result = update(client, time_of_day='14:00')
    request, body = writes(requests)
    assert request.method == 'PUT' and request.url.path == '/api/check-in-notes/test-series'
    assert request.headers['x-organizational-unit-id'] == 'test-sfo'
    # Som appens UpdateCheckoutNoteRequestApiModel: ingen childPersonIds, eksklusiv toDate.
    assert body == {
        'category': 'afterSchoolCheckOut', 'typeId': 'test-type',
        'fromDate': '2026-12-28', 'toDate': '2027-01-01',
        'recurrencePattern': {'recurrenceType': 'weekly', 'interval': 1,
                              'daysOfWeek': ['monday', 'tuesday', 'wednesday', 'thursday']},
        'timeOfDay': '14:00:00', 'noteText': 'Testhenting'}
    assert result['status'] == 'updated' and result['toDate'] == '2026-12-31'
    assert requests[-1].method == 'PUT'


def test_new_days_dates_and_every_other_week(client):
    requests = []
    transport(client, write_api(requests))
    update(client, from_date='2027-01-04', to_date='2027-01-29',
           repeat_days=['friday', 'monday'], every_other_week=True)
    _, body = writes(requests)
    assert body['fromDate'] == '2027-01-04' and body['toDate'] == '2027-01-30'
    assert body['recurrencePattern'] == {
        'recurrenceType': 'weekly', 'interval': 2, 'daysOfWeek': ['monday', 'friday']}


def test_kept_interval_two_stays_every_other_week(client):
    requests = []
    series = {**SERIES, 'recurrencePattern': {**SERIES['recurrencePattern'], 'interval': 2}}
    transport(client, write_api(requests, notes=[series]))
    update(client, note_text='Ny tekst')
    assert writes(requests)[1]['recurrencePattern']['interval'] == 2


def test_empty_repeat_days_turns_repetition_off(client):
    requests = []
    transport(client, write_api(requests))
    update(client, repeat_days=[])
    assert writes(requests)[1]['recurrencePattern'] == {'recurrenceType': 'daily', 'interval': 1}


def test_weekly_series_without_days_is_not_resent(client):
    """Live 2026-10-16: weekly utan daysOfWeek gjeld berre startdagen."""
    requests = []
    broken = {**SERIES, 'recurrencePattern': {'recurrenceType': 'weekly', 'interval': 1,
                                              'daysOfWeek': None}}
    transport(client, write_api(requests, notes=[broken]))
    with pytest.raises(ValueError, match='utan vekedagar'):
        update(client, time_of_day='14:30')
    assert all(r.method == 'GET' for r in requests)
    update(client, repeat_days=['monday', 'thursday'])
    assert writes(requests)[1]['recurrencePattern']['daysOfWeek'] == ['monday', 'thursday']


@pytest.mark.parametrize('flags,match', [
    ({'canUpdate': False, 'canUpdateToDate': True}, 'set_pickup_note_end_date'),
    ({'canUpdate': False, 'canUpdateToDate': False}, 'canUpdate=false'),
])
def test_update_requires_can_update(client, flags, match):
    requests = []
    transport(client, write_api(requests, notes=[{**SERIES, **flags}]))
    with pytest.raises(ValueError, match=match):
        update(client)
    assert all(r.method == 'GET' for r in requests)


@pytest.mark.parametrize('changes,match', [
    ({'note_id': 'test-unknown'}, 'Fann ikkje'),
    ({'month': '2026-13'}, 'gyldig månad'),
    ({'to_date': '2026-12-27'}, 'før'),
    ({'time_of_day': '14:20'}, 'kvarter'),
    ({'repeat_days': ['saturday']}, 'repeat_days'),
    ({'repeat_days': [], 'every_other_week': True}, 'krev repeat_days'),
    ({'type_id': 'test-general'}, 'aktiv type'),
    ({'note_text': ' '}, 'krev tekst'),
    ({'child_id': 'test-child-b'}, 'Fann ikkje'),   # barn utan SFO-eining
])
def test_invalid_update_is_rejected_without_write(client, changes, match):
    requests = []
    transport(client, write_api(requests))
    with pytest.raises(ValueError, match=match):
        update(client, **changes)
    assert all(r.method == 'GET' for r in requests)


def test_note_of_other_child_is_rejected(client):
    requests = []
    transport(client, write_api(requests, notes=[{**SERIES, 'childPersonId': 'test-child-c'}]))
    with pytest.raises(ValueError, match='høyrer ikkje'):
        update(client)
    assert all(r.method == 'GET' for r in requests)


def test_deadline_only_checks_changed_start(client):
    deadline = {'organizationalUnitId': 'test-sfo', 'noteCategory': 'afterSchoolCheckOut',
                'earliestAllowedDate': '2027-01-05T23:00:00Z'}   # 06.01. i Oslo
    requests = []
    transport(client, write_api(requests, deadline=deadline))
    update(client, time_of_day='14:30')                      # startdato uendra
    writes(requests)
    requests.clear()
    with pytest.raises(ValueError, match='Registreringsfristen'):
        update(client, from_date='2027-01-04', to_date='2027-01-08')
    assert all(r.method == 'GET' for r in requests)


def test_server_deadline_error_is_explained(client):
    transport(client, write_api([], write=lambda r: httpx.Response(400, json={
        'type': 'validation', 'message': 'privat tekst', 'validationErrors': {
            'fromDate': {'errorCode': 'registration_deadline_has_passed_for_date'}}})))
    with pytest.raises(ValueError, match='registreringsfristen er passert') as error:
        update(client)
    assert 'privat' not in str(error.value)


@pytest.mark.parametrize('call', ['update', 'end', 'delete'])
def test_unknown_outcome_is_not_retried(client, call):
    requests = []
    transport(client, write_api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='list_pickup_notes'):
        if call == 'update':
            update(client)
        elif call == 'end':
            client.set_pickup_note_end_date('test-child', 'test-series', '2026-12', '2026-12-30')
        else:
            client.delete_pickup_note('test-child', 'test-series', '2026-12')
    assert len([r for r in requests if r.method != 'GET']) == 1


@pytest.fixture
def today(monkeypatch):
    class Clock(client_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return client_module.datetime(2026, 12, 29, 9, 0, tzinfo=tz)
    monkeypatch.setattr(client_module, 'datetime', Clock)


def test_end_date_sends_exclusive_end(client, today):
    requests = []
    transport(client, write_api(requests, notes=[{**SERIES, 'canUpdate': False}]))
    result = client.set_pickup_note_end_date('test-child', 'test-series', '2026-12', '2026-12-30')
    request, body = writes(requests)
    assert request.method == 'PUT'
    assert request.url.path == '/api/check-in-notes/test-series/to-date'
    assert request.headers['x-organizational-unit-id'] == 'test-sfo'
    # Vigilo lagrar toDate eksklusivt (live): siste dag 30.12. -> 31.12.
    assert body == {'toDate': '2026-12-31'}
    assert result['status'] == 'endDateUpdated'


@pytest.mark.parametrize('flags,last,match', [
    ({'canUpdateToDate': False}, '2026-12-30', 'canUpdateToDate=false'),
    ({}, '30.12.2026', 'YYYY-MM-DD'),
    ({}, '2026-12-27', 'startdatoen'),
    ({}, '2026-12-28', '2026-12-29'),        # før i dag når fristen manglar
])
def test_invalid_end_date_is_rejected_without_write(client, today, flags, last, match):
    requests = []
    transport(client, write_api(requests, notes=[{**SERIES, **flags}]))
    with pytest.raises(ValueError, match=match):
        client.set_pickup_note_end_date('test-child', 'test-series', '2026-12', last)
    assert all(r.method == 'GET' for r in requests)


def test_end_date_follows_deadline_when_present(client, today):
    deadline = {'organizationalUnitId': 'test-sfo', 'noteCategory': 'afterSchoolCheckOut',
                'earliestAllowedDate': '2026-12-29T23:00:00Z'}   # 30.12. i Oslo
    requests = []
    transport(client, write_api(requests, deadline=deadline))
    with pytest.raises(ValueError, match='2026-12-30'):
        client.set_pickup_note_end_date('test-child', 'test-series', '2026-12', '2026-12-29')
    client.set_pickup_note_end_date('test-child', 'test-series', '2026-12', '2026-12-30')
    writes(requests)


def test_delete_sends_no_body_with_unit_header(client):
    requests = []
    transport(client, write_api(requests))
    result = client.delete_pickup_note('test-child', 'test-series', '2026-12')
    request, body = writes(requests)
    assert request.method == 'DELETE' and request.url.path == '/api/check-in-notes/test-series'
    assert request.headers['x-organizational-unit-id'] == 'test-sfo'
    assert body is None and result['status'] == 'deleted'


def test_delete_requires_can_delete(client):
    requests = []
    transport(client, write_api(requests, notes=[{**SERIES, 'canDelete': False}]))
    with pytest.raises(ValueError, match='canDelete=false'):
        client.delete_pickup_note('test-child', 'test-series', '2026-12')
    assert all(r.method == 'GET' for r in requests)


def test_update_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, write_api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'update_pickup_note', 'set_pickup_note_end_date', 'delete_pickup_note'} <= names
    result = wire('update_pickup_note', {
        'child_id': 'test-child', 'note_id': 'test-series', 'month': '2026-12',
        'repeat_days': ['monday']})
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['status'] == 'updated'
    assert writes(requests)[1]['recurrencePattern']['daysOfWeek'] == ['monday']


def test_weekly_without_days_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    broken = {**SERIES, 'recurrencePattern': {'recurrenceType': 'weekly', 'interval': 1}}
    transport(real_client, write_api(requests, notes=[broken]))
    error = visible_error(wire('update_pickup_note', {
        'child_id': 'test-child', 'note_id': 'test-series', 'month': '2026-12'}))
    assert 'vekedagar' in error and all(r.method == 'GET' for r in requests)
