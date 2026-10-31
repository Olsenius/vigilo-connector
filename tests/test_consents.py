"""Samtykke: lesing, gi (POST /api/consents) og trekkje tilbake (PUT .../revoke).

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


def consent(person, status, consent_id=None):
    return {'id': consent_id, 'status': status, 'updatedAt': None,
            'legalGuardian': {'role': 'parent', 'person': {
                'id': person, 'firstName': 'Test', 'lastName': person}}}


OPEN = {'id': 'test-form', 'description': '<p>Bilete p&aring; nett</p><p>Ja?</p>',
        'isRead': True, 'status': 'partlyConsentsAccepted', 'updatedAt': None,
        'consents': [consent('test-guardian', 'noResponse'),
                     consent('test-other', 'accepted', 'test-other-consent')]}
GIVEN = {**OPEN, 'consents': [consent('test-guardian', 'accepted', 'test-consent'),
                              consent('test-other', 'accepted', 'test-other-consent')]}
REVOKED = {**OPEN, 'consents': [consent('test-guardian', 'revoked', 'test-consent')]}
NOT_MINE = {**OPEN, 'consents': [consent('test-other', 'noResponse')]}


def api(requests, form=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(200)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/consent-forms':
            return httpx.Response(200, json={
                'consentForms': [OPEN if form is None else form],
                'consentInformation': {'text': '<b>Info</b>', 'updatedAt': None}})
        if path == '/api/consents':
            return httpx.Response(200, json=OPEN if form is None else form)
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0]


def test_list_matches_app_query_and_marks_my_answer(client):
    requests = []
    transport(client, api(requests))
    result = client.list_consents('test-child')
    query = dict(next(r for r in requests if r.url.path == '/api/consent-forms').url.params)
    # Berre skule-/barnehageeiningar; SFO-eininga til barnet blir ikkje spurd.
    assert query == {'childPersonId': 'test-child', 'organizationalUnitId': 'test-school'}
    unit = result['units'][0]
    assert unit['information'] == 'Info'
    form = unit['forms'][0]
    assert form['description'] == 'Bilete på nett\nJa?'
    assert (form['myStatus'], form['canGive'], form['canRevoke']) == ('noResponse', True, False)
    assert form['otherGuardians'] == [{'name': 'Test test-other', 'status': 'accepted'}]


@pytest.mark.parametrize('form,expected', [
    (GIVEN, ('accepted', False, True)),
    (REVOKED, ('revoked', True, False)),
    (NOT_MINE, ('noResponse', False, False)),
])
def test_list_flags(client, form, expected):
    transport(client, api([], form=form))
    found = client.list_consents('test-child')['units'][0]['forms'][0]
    assert (found['myStatus'], found['canGive'], found['canRevoke']) == expected


def test_give_matches_app_body(client):
    requests = []
    transport(client, api(requests))
    result = client.give_consent('test-child', 'test-form')
    detail = next(r for r in requests if r.url.path == '/api/consents' and r.method == 'GET')
    assert dict(detail.url.params) == {'childPersonId': 'test-child', 'consentFormId': 'test-form'}
    request = writes(requests)
    assert request.method == 'POST' and request.url.path == '/api/consents'
    assert json.loads(request.content) == {'childPersonId': 'test-child',
                                           'consentFormId': 'test-form'}
    assert result['status'] == 'accepted'


def test_give_after_revoke_is_allowed(client):
    requests = []
    transport(client, api(requests, form=REVOKED))
    client.give_consent('test-child', 'test-form')
    assert writes(requests).method == 'POST'


def test_revoke_uses_my_consent_id_without_body(client):
    requests = []
    transport(client, api(requests, form=GIVEN))
    result = client.revoke_consent('test-child', 'test-form')
    request = writes(requests)
    assert request.method == 'PUT' and request.url.path == '/api/consents/test-consent/revoke'
    assert not request.content and result['status'] == 'revoked'


@pytest.mark.parametrize('action,form,args,match', [
    ('give_consent', GIVEN, {}, 'alt gitt'),
    ('give_consent', NOT_MINE, {}, 'ikkje som føresett'),
    ('give_consent', None, {'consent_form_id': 'test-other-form'}, 'Fann ikkje'),
    ('give_consent', None, {'consent_form_id': ' '}, 'consent_form_id'),
    ('give_consent', None, {'child_id': 'test-unknown'}, 'Fann ikkje barnet'),
    ('revoke_consent', None, {}, 'Utan tidlegare svar'),
    ('revoke_consent', REVOKED, {}, 'Berre eit gitt samtykke'),
    ('revoke_consent', {**OPEN, 'consents': [consent('test-guardian', 'accepted')]}, {},
     'Berre eit gitt samtykke'),
])
def test_invalid_answer_is_rejected_without_write(client, action, form, args, match):
    requests = []
    transport(client, api(requests, form=form))
    with pytest.raises(ValueError, match=match):
        getattr(client, action)(**{'child_id': 'test-child', 'consent_form_id': 'test-form',
                                   **args})
    assert all(r.method == 'GET' for r in requests)


def test_explicit_unit_must_belong_to_child(client):
    transport(client, api([]))
    with pytest.raises(ValueError, match='skule- eller barnehageeining'):
        client.list_consents('test-child', 'test-sfo')


def test_unknown_outcome_is_not_retried(client):
    requests = []
    transport(client, api(requests, write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='list_consents'):
        client.give_consent('test-child', 'test-form')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_give_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_consents', 'give_consent', 'revoke_consent'} <= names
    result = wire('give_consent', {'child_id': 'test-child', 'consent_form_id': 'test-form'})
    assert result.get('isError') is not True, result
    assert writes(requests).url.path == '/api/consents'


def test_revoke_without_answer_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    error = visible_error(wire('revoke_consent', {'child_id': 'test-child',
                                                  'consent_form_id': 'test-form'}))
    assert 'samtykke' in error and all(r.method == 'GET' for r in requests)
