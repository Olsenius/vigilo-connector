"""Oppretting av hentemeldingar (POST /api/check-in-notes); berre syntetiske data."""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import api
from vigilo_connector.client import WriteOutcomeUnknown


def write_api(requests, post=None, deadline=None, types=None):
    read = api([], types=types)
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        if request.method == 'POST':
            assert request.url.path == '/api/check-in-notes'
            return post(request) if post else httpx.Response(201)
        if deadline is not None and request.url.path.endswith('/registration-deadlines'):
            return httpx.Response(200, json=deadline)
        return read(request)
    return handler


def create(client, **changes):
    args = dict(child_id='test-child', organizational_unit_id='test-sfo', type_id='test-type',
                from_date='2026-10-19', to_date='2026-10-19', time_of_day='14:30',
                note_text='Testhenting')
    args.update(changes)
    return client.create_pickup_note(**args)


def posted(requests):
    posts = [r for r in requests if r.method == 'POST']
    assert len(posts) == 1
    return posts[0], json.loads(posts[0].content)


def test_single_day_matches_app_wire_contract(client):
    requests = []
    transport(client, write_api(requests))
    result = create(client)
    request, body = posted(requests)
    assert request.headers['x-organizational-unit-id'] == 'test-sfo'
    assert request.headers['authorization'] == 'Bearer old'
    assert request.headers['content-type'].startswith('application/json')
    assert body == {
        'childPersonIds': ['test-child'], 'category': 'afterSchoolCheckOut',
        'typeId': 'test-type', 'fromDate': '2026-10-19', 'toDate': '2026-10-20',
        # Gson utelèt daysOfWeek når det er null.
        'recurrencePattern': {'recurrenceType': 'daily', 'interval': 1},
        'timeOfDay': '14:30:00', 'noteText': 'Testhenting'}
    assert result['status'] == 'created' and result['type']['category'] == 'afterSchoolCheckOut'
    # Lesingane går føre skrivinga.
    assert requests[-1].method == 'POST'


@pytest.mark.parametrize('days,other,interval', [
    (['friday', 'monday'], False, 1), (['wednesday'], True, 2)])
def test_weekly_repeat_orders_days_and_sets_interval(client, days, other, interval):
    requests = []
    transport(client, write_api(requests))
    create(client, from_date='2026-10-19', to_date='2026-12-18',
           repeat_days=days, every_other_week=other)
    _, body = posted(requests)
    expected = [d for d in ('monday', 'wednesday', 'friday') if d in days]
    assert body['recurrencePattern'] == {
        'recurrenceType': 'weekly', 'interval': interval, 'daysOfWeek': expected}
    assert body['toDate'] == '2026-12-19'


def test_type_without_comment_sends_empty_text(client):
    requests = []
    transport(client, write_api(requests, types=[
        {'id': 'test-type', 'name': 'Går hjem selv', 'category': 'afterSchoolCheckOut',
         'useComment': False, 'isActive': True, 'isDeleted': False}]))
    create(client, note_text=None)
    assert posted(requests)[1]['noteText'] == ''


@pytest.mark.parametrize('changes,match', [
    ({'from_date': '2026-10-20', 'to_date': '2026-10-19'}, 'før'),
    ({'from_date': '19.10.2026'}, 'YYYY-MM-DD'),
    ({'from_date': '2026-02-30'}, 'gyldige'),
    ({'time_of_day': '14:20'}, 'kvarter'),
    ({'time_of_day': '05:45'}, 'kvarter'),
    ({'time_of_day': '18:15'}, 'kvarter'),
    ({'time_of_day': '1430'}, 'HH:MM'),
    ({'repeat_days': []}, 'repeat_days'),
    ({'repeat_days': ['saturday']}, 'repeat_days'),
    ({'repeat_days': 'monday'}, 'repeat_days'),
    ({'every_other_week': True}, 'krev repeat_days'),
    ({'every_other_week': 'yes'}, 'true eller false'),
    ({'type_id': ''}, 'type_id'),
])
def test_invalid_input_is_rejected_before_any_request(client, changes, match):
    requests = []
    transport(client, write_api(requests))
    with pytest.raises(ValueError, match=match):
        create(client, **changes)
    assert requests == []


@pytest.mark.parametrize('changes,match', [
    ({'type_id': 'test-general'}, 'aktiv type'),   # kategori ikkje aktivert
    ({'type_id': 'test-unknown'}, 'aktiv type'),
    ({'note_text': '  '}, 'krev tekst'),
    ({'organizational_unit_id': 'test-school'}, 'SFO-eining'),
    ({'child_id': 'test-child-b'}, 'SFO-eining'),
])
def test_invalid_choice_is_rejected_without_write(client, changes, match):
    requests = []
    transport(client, write_api(requests))
    with pytest.raises(ValueError, match=match):
        create(client, **changes)
    assert all(r.method == 'GET' for r in requests)


@pytest.mark.parametrize('flag', [{'isActive': False}, {'isDeleted': True}])
def test_inactive_or_deleted_type_is_rejected(client, flag):
    requests = []
    transport(client, write_api(requests, types=[
        {'id': 'test-type', 'name': 'Hentes av', 'category': 'afterSchoolCheckOut',
         'useComment': True, 'isActive': True, 'isDeleted': False, **flag}]))
    with pytest.raises(ValueError, match='aktiv type'):
        create(client)
    assert all(r.method == 'GET' for r in requests)


@pytest.mark.parametrize('earliest,start,allowed', [
    ('2026-10-18T22:00:00Z', '2026-10-19', True),    # midnatt i Oslo 19.10.
    ('2026-10-19T22:00:00Z', '2026-10-19', False),   # 20.10. i Oslo
    ('2026-10-16T22:00:00Z', '2026-10-17', False),   # laurdag -> måndag 19.10.
    ('2026-10-16T22:00:00Z', '2026-10-19', True),
    ('ugyldig', '2026-10-01', True),                 # ingen lokal grense; serveren avgjer
])
def test_earliest_allowed_date_follows_app(client, earliest, start, allowed):
    requests = []
    transport(client, write_api(requests, deadline={
        'organizationalUnitId': 'test-sfo', 'noteCategory': 'afterSchoolCheckOut',
        'earliestAllowedDate': earliest}))
    if allowed:
        create(client, from_date=start, to_date='2026-10-23')
        posted(requests)
    else:
        with pytest.raises(ValueError, match='Registreringsfristen'):
            create(client, from_date=start, to_date='2026-10-23')
        assert all(r.method == 'GET' for r in requests)


def test_server_deadline_error_is_explained(client):
    requests = []
    transport(client, write_api(requests, post=lambda r: httpx.Response(400, json={
        'type': 'validation', 'message': 'privat tekst',
        'validationErrors': {'fromDate': {'errorCode': 'registration_deadline_has_passed_for_date',
                                          'message': 'privat tekst'}}})))
    with pytest.raises(ValueError, match='registreringsfristen er passert') as error:
        create(client)
    assert 'privat' not in str(error.value)


def test_server_validation_codes_are_listed(client):
    transport(client, write_api([], post=lambda r: httpx.Response(400, json={
        'type': 'category', 'message': 'Mangler årsak'})))
    with pytest.raises(ValueError, match='category'):
        create(client)


def test_unparsed_rejection_stays_http_error(client):
    transport(client, write_api([], post=lambda r: httpx.Response(403, text='nei')))
    with pytest.raises(httpx.HTTPStatusError):
        create(client)


@pytest.mark.parametrize('post', [
    lambda r: httpx.Response(502),
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('timeout', request=r)),
])
def test_unknown_outcome_is_not_retried(client, post):
    requests = []
    transport(client, write_api(requests, post=post))
    with pytest.raises(WriteOutcomeUnknown, match='list_pickup_notes'):
        create(client)
    assert len([r for r in requests if r.method == 'POST']) == 1


def test_create_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, write_api(requests))
    assert 'create_pickup_note' in {tool['name'] for tool in wire.list_tools()}
    result = wire('create_pickup_note', {
        'child_id': 'test-child', 'organizational_unit_id': 'test-sfo', 'type_id': 'test-type',
        'from_date': '2026-10-19', 'to_date': '2026-10-19', 'time_of_day': '14:30',
        'note_text': 'Testhenting', 'repeat_days': ['monday']})
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['status'] == 'created'
    assert posted(requests)[1]['recurrencePattern']['daysOfWeek'] == ['monday']


def test_invalid_time_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, write_api(requests))
    error = visible_error(wire('create_pickup_note', {
        'child_id': 'test-child', 'organizational_unit_id': 'test-sfo', 'type_id': 'test-type',
        'from_date': '2026-10-19', 'to_date': '2026-10-19', 'time_of_day': '14:20'}))
    assert 'kvarter' in error and requests == []
