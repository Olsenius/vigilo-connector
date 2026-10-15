import json

import httpx
import pytest

from vigilo_connector.auth import AuthError
from vigilo_connector.client import VigiloClient, WriteOutcomeUnknown
from vigilo_connector.config import API_BASE
from tests.test_parent_actions import FakeStore, transport

CONTACT = {"type": "employee", "externalId": "test-contact"}


@pytest.fixture
def client():
    c = VigiloClient(FakeStore())
    yield c
    c.close()


def send(client, **changes):
    args = dict(child_id="test-child", organizational_unit_id="test-school",
                title="Testtittel", body="Testmelding", recipients=[CONTACT])
    args.update(changes)
    return client.send_message(**args)


def contacts(request):
    assert dict(request.url.params) == {
        "childId": "test-child", "organizationalUnitId": "test-school",
    }
    return httpx.Response(200, json={
        "relatedEmployees": [{"id": "test-contact", "employeeId": "wrong-id"}],
    })


@pytest.mark.parametrize("status", [200, 201, 204])
def test_new_message_contract(client, status):
    def handler(request):
        if request.method == "GET":
            return contacts(request)
        assert request.url.path == "/api/messages"
        assert not request.url.query
        assert request.headers["authorization"] == "Bearer old"
        assert json.loads(request.content) == {
            "organizationalUnitId": "test-school", "title": "Testtittel",
            "body": "Testmelding", "attachments": [], "recipients": [CONTACT],
        }
        return httpx.Response(status)
    transport(client, handler)
    assert send(client)["status"] == "sent"


@pytest.mark.parametrize("changes", [
    {"title": ""}, {"body": " "}, {"child_id": ""}, {"organizational_unit_id": ""},
    {"recipients": []}, {"recipients": None}, {"recipients": [None]},
    {"recipients": [{"type": "employee", "externalId": ""}]},
    {"recipients": [{"type": "legalGuardian", "externalId": "test-guardian"}]},
])
def test_invalid_new_message_never_reaches_api(client, changes):
    transport(client, lambda r: pytest.fail("Invalid input reached API"))
    with pytest.raises(ValueError):
        send(client, **changes)


def test_unknown_recipient_never_posts(client):
    def handler(request):
        assert request.method == "GET"
        return contacts(request)
    transport(client, handler)
    with pytest.raises(ValueError, match="kontaktlisten"):
        send(client, recipients=[{"type": "employee", "externalId": "unknown"}])


def test_reply_contract(client):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/messages/test-thread/reply"
        assert not request.url.query
        assert json.loads(request.content) == {"body": "Testsvar", "attachments": []}
        return httpx.Response(204)
    transport(client, handler)
    assert client.reply_message("test-thread", "Testsvar")["status"] == "sent"


@pytest.mark.parametrize("thread,body", [("", "Svar"), ("..", "Svar"), ("thread", " ")])
def test_invalid_reply(client, thread, body):
    transport(client, lambda r: pytest.fail("Invalid input reached API"))
    with pytest.raises(ValueError):
        client.reply_message(thread, body)


@pytest.mark.parametrize("operation", ["new", "reply"])
@pytest.mark.parametrize("failure", [500, 502, 403, "timeout"])
def test_failures_never_retry(client, operation, failure):
    posts = []
    def handler(request):
        if request.method == "GET":
            return contacts(request)
        posts.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(failure)
    transport(client, handler)
    error = httpx.HTTPStatusError if failure == 403 else WriteOutcomeUnknown
    with pytest.raises(error):
        send(client) if operation == "new" else client.reply_message("test-thread", "Testsvar")
    assert len(posts) == 1


@pytest.mark.parametrize("operation", ["new", "reply"])
@pytest.mark.parametrize("second", [204, 401])
def test_auth_retry_once(client, operation, second):
    posts = []
    def handler(request):
        if request.method == "GET":
            return contacts(request)
        posts.append(request)
        assert request.headers["authorization"] == ("Bearer old" if len(posts) == 1 else "Bearer new")
        return httpx.Response(401 if len(posts) == 1 else second)
    transport(client, handler)
    def call():
        return send(client) if operation == "new" else client.reply_message("test-thread", "Testsvar")
    if second == 401:
        with pytest.raises(AuthError):
            call()
    else:
        assert call()["status"] == "sent"
    assert len(posts) == 2
