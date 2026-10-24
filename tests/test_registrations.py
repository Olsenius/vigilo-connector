"""Påmeldingsskjema (forms/registrations): lesing, svar og tilbaketrekking.

Berre syntetiske data.
"""
import base64
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import CHILDREN
from vigilo_connector import client as client_module
from vigilo_connector.client import WriteOutcomeUnknown

CHOICES = [
    {'registrationChoiceId': 'test-yes', 'choiceNumber': 1, 'choiceText': 'Barnet kommer'},
    {'registrationChoiceId': 'test-no', 'choiceNumber': 2, 'choiceText': 'Barnet kommer ikke'},
    {'registrationChoiceId': 'test-maybe', 'choiceNumber': 3, 'choiceText': 'Kanskje'},
]
FORM = {'id': 'test-form', 'registrationType': 'singleChoice', 'title': 'Testlangdag',
        'description': 'Testtekst', 'registrationChoices': CHOICES, 'reply': None,
        'registrationDeadline': '2099-10-26T22:00:00Z', 'attachments': [],
        'createdByPerson': {'personId': 'test-employee', 'firstName': 'Test',
                            'lastName': 'Tilsett', 'profilePictureUrl': None},
        'publicationTime': '2026-10-19T08:00:00Z', 'organizationalUnitId': 'test-sfo',
        'organizationalUnitName': 'Test-SFO'}
REPLY = {'replyTime': '2026-10-20T08:00:00Z', 'confirmationSentTime': None,
         'selectedRegistrationChoices': [
             {'registrationChoiceId': 'test-yes', 'rank': 1, 'choiceText': 'Barnet kommer'}]}


def jwt(claims):
    encode = lambda part: base64.urlsafe_b64encode(json.dumps(part).encode()).decode().rstrip('=')
    return f"{encode({'alg': 'none'})}.{encode(claims)}.signature"


@pytest.fixture(autouse=True)
def user(monkeypatch):
    # Appen brukar personId som userId, ikkje sub (live 2026-10-24: sub gav HTTP 403).
    token = jwt({'sub': 'test-subject', 'personId': 'test-guardian'})
    monkeypatch.setattr(client_module.VigiloClient, 'user_id',
                        property(lambda self: client_module.user_id_from_jwt(token)))


def api(requests, forms=None, write=None):
    def handler(request):
        requests.append(request)
        assert 'userId' not in request.url.params
        path = request.url.path
        if request.method != 'GET':
            return write(request) if write else httpx.Response(200)
        if path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if path == '/api/forms/registrations/invitations/test-child':
            return httpx.Response(200, json={'items': [FORM] if forms is None else forms})
        raise AssertionError(f'Uventa kall: {path}')
    return handler


def writes(requests):
    found = [r for r in requests if r.method != 'GET']
    assert len(found) == 1
    return found[0], json.loads(found[0].content) if found[0].content else None


def test_list_marks_open_and_closed_forms(client):
    requests = []
    closed = {**FORM, 'id': 'test-closed', 'registrationDeadline': '2000-01-01T00:00:00Z'}
    transport(client, api(requests, forms=[FORM, closed]))
    result = client.registration_forms('test-child')
    assert [f['canReply'] for f in result['registrations']] == [True, False]
    assert all(r.method == 'GET' for r in requests)


def test_list_rejects_unknown_child(client):
    requests = []
    transport(client, api(requests))
    with pytest.raises(ValueError, match='Fann ikkje barnet'):
        client.registration_forms('test-unknown')
    assert [r.url.path for r in requests] == ['/api/children']


def test_single_choice_reply_matches_app_body(client):
    requests = []
    transport(client, api(requests))
    result = client.reply_registration('test-child', 'test-form', ['test-no'])
    request, body = writes(requests)
    assert request.method == 'POST'
    assert request.url.path == '/api/forms/registrations/test-form/replies'
    assert 'x-organizational-unit-id' not in request.headers
    # Som appens RegistrationReplyRequest; rank er choiceNumber for enkeltval.
    assert body == {'childPersonId': 'test-child', 'inviteePersonId': 'test-guardian',
                    'selectedRegistrationChoices': [{'rank': 2, 'registrationChoiceId': 'test-no'}]}
    assert result['status'] == 'replied' and result['replacedPreviousReply'] is False
    assert result['selectedChoices'][0]['choiceText'] == 'Barnet kommer ikke'


def test_multiple_choices_keep_choice_numbers(client):
    requests = []
    transport(client, api(requests, forms=[{**FORM, 'registrationType': 'multipleChoices'}]))
    client.reply_registration('test-child', 'test-form', ['test-maybe', 'test-yes'])
    assert writes(requests)[1]['selectedRegistrationChoices'] == [
        {'rank': 3, 'registrationChoiceId': 'test-maybe'},
        {'rank': 1, 'registrationChoiceId': 'test-yes'}]


def test_ranked_choices_use_position(client):
    requests = []
    transport(client, api(requests, forms=[{**FORM, 'registrationType': 'rankedChoices'}]))
    client.reply_registration('test-child', 'test-form', ['test-maybe', 'test-yes', 'test-no'])
    assert [(c['rank'], c['registrationChoiceId'])
            for c in writes(requests)[1]['selectedRegistrationChoices']] == [
        (1, 'test-maybe'), (2, 'test-yes'), (3, 'test-no')]


def test_new_reply_replaces_existing(client):
    requests = []
    transport(client, api(requests, forms=[{**FORM, 'reply': REPLY}]))
    result = client.reply_registration('test-child', 'test-form', ['test-no'])
    writes(requests)
    assert result['replacedPreviousReply'] is True


@pytest.mark.parametrize('form,choices,match', [
    ({}, [], 'liste'),
    ({}, 'test-yes', 'liste'),
    ({}, ['test-yes', 'test-no'], 'nøyaktig eitt'),
    ({}, ['test-unknown'], 'Ukjend val'),
    ({'registrationType': 'multipleChoices'}, ['test-yes', 'test-yes'], 'fleire gonger'),
    ({'registrationType': 'rankedChoices'}, ['test-yes', 'test-no'], 'alle val'),
    ({'registrationType': 'freeText'}, ['test-yes'], 'Ukjend type'),
    ({'registrationDeadline': '2000-01-01T00:00:00Z'}, ['test-yes'], 'Fristen'),
    ({'registrationDeadline': None}, ['test-yes'], 'Fristen'),
    ({'id': 'test-other'}, ['test-yes'], 'Fann ikkje påmeldinga'),
])
def test_invalid_reply_is_rejected_without_write(client, form, choices, match):
    requests = []
    transport(client, api(requests, forms=[{**FORM, **form}]))
    with pytest.raises(ValueError, match=match):
        client.reply_registration('test-child', 'test-form', choices)
    assert all(r.method == 'GET' for r in requests)


def test_withdraw_uses_user_and_child_in_path(client):
    requests = []
    transport(client, api(requests, forms=[{**FORM, 'reply': REPLY}]))
    result = client.withdraw_registration_reply('test-child', 'test-form')
    request, body = writes(requests)
    assert request.method == 'DELETE'
    assert request.url.path == '/api/forms/registrations/test-form/replies/test-guardian/test-child'
    assert body is None and result['status'] == 'withdrawn'


@pytest.mark.parametrize('form,match', [
    ({}, 'ikkje noko svar'),
    ({'reply': REPLY, 'registrationDeadline': '2000-01-01T00:00:00Z'}, 'Fristen'),
])
def test_invalid_withdraw_is_rejected_without_write(client, form, match):
    requests = []
    transport(client, api(requests, forms=[{**FORM, **form}]))
    with pytest.raises(ValueError, match=match):
        client.withdraw_registration_reply('test-child', 'test-form')
    assert all(r.method == 'GET' for r in requests)


@pytest.mark.parametrize('call', ['reply', 'withdraw'])
def test_unknown_outcome_is_not_retried(client, call):
    requests = []
    transport(client, api(requests, forms=[{**FORM, 'reply': REPLY}],
                          write=lambda r: httpx.Response(502)))
    with pytest.raises(WriteOutcomeUnknown, match='list_registrations'):
        if call == 'reply':
            client.reply_registration('test-child', 'test-form', ['test-yes'])
        else:
            client.withdraw_registration_reply('test-child', 'test-form')
    assert len([r for r in requests if r.method != 'GET']) == 1


def test_reply_reads_fresh_invitations(client):
    requests = []
    transport(client, api(requests))
    client.registration_forms('test-child')
    client.reply_registration('test-child', 'test-form', ['test-yes'])
    invitations = [r for r in requests if r.url.path.startswith('/api/forms/')
                   and r.method == 'GET']
    assert len(invitations) == 2


def test_reply_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    names = {tool['name'] for tool in wire.list_tools()}
    assert {'list_registrations', 'reply_registration', 'withdraw_registration_reply'} <= names
    listed = wire('list_registrations', {'child_id': 'test-child'})
    assert json.loads(listed['content'][0]['text'])['registrations'][0]['canReply'] is True
    result = wire('reply_registration', {'child_id': 'test-child', 'registration_id': 'test-form',
                                         'choice_ids': ['test-yes']})
    assert result.get('isError') is not True, result
    assert json.loads(result['content'][0]['text'])['status'] == 'replied'
    assert writes(requests)[1]['selectedRegistrationChoices'] == [
        {'rank': 1, 'registrationChoiceId': 'test-yes'}]


def test_rejected_reply_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    error = visible_error(wire('reply_registration', {
        'child_id': 'test-child', 'registration_id': 'test-form',
        'choice_ids': ['test-yes', 'test-no']}))
    assert 'eitt val' in error and all(r.method == 'GET' for r in requests)


def test_user_id_requires_person_id_claim():
    assert client_module.user_id_from_jwt(jwt({'sub': 'test-subject', 'personId': 'test-guardian'})) == 'test-guardian'
    for token in (jwt({'sub': 'test-subject'}), 'not-a-jwt'):
        with pytest.raises(client_module.AuthError, match='personId'):
            client_module.user_id_from_jwt(token)
