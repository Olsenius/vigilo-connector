import json

import httpx
import pytest

from vigilo_connector.auth import AuthError
from vigilo_connector.client import VigiloClient, WriteOutcomeUnknown
from vigilo_connector.config import API_BASE, APP_VERSION
from vigilo_connector import server


class FakeStore:
    def __init__(self):
        self.refreshes = []

    def access_token(self, force_refresh=False):
        self.refreshes.append(force_refresh)
        return "new" if force_refresh else "old"


@pytest.fixture
def client():
    c = VigiloClient(FakeStore())
    yield c
    c.close()


def transport(client, handler):
    client._http.close()
    client._http = httpx.Client(base_url=API_BASE, transport=httpx.MockTransport(handler))


def register(client, **changes):
    args = dict(child_id="child", organizational_unit_id="school", from_date="2026-10-09",
                to_date="2026-10-10", note="Syk", title="Fravær",
                recipients=[{"type": "employee", "externalId": "contact"}])
    args.update(changes)
    return client.register_student_absence(**args)


@pytest.mark.parametrize("status,body", [(204, b""), (201, b""), (200, b"ok")])
def test_request_contract_and_empty_success(client, status, body):
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == API_BASE.rstrip("/") + "/api/student-absences"
        assert request.headers["authorization"] == "Bearer old"
        assert request.headers["appVersion"] == APP_VERSION
        assert json.loads(request.content) == {
            "childId": "child", "organizationalUnitId": "school",
            "fromDate": "09.10.2026", "toDate": "11.10.2026", "note": "Syk",
            "title": "Fravær", "attachments": [],
            "recipients": [{"type": "employee", "externalId": "contact"}],
        }
        return httpx.Response(status, content=body)
    transport(client, handler)
    result = register(client)
    assert result["status"] == "registered"
    assert result["fromDate"] == "2026-10-09"
    assert result["recipients"][0]["externalId"] == "contact"


@pytest.mark.parametrize("day,end", [("2026-12-25", "26.12.2026"), ("2026-12-31", "01.01.2027")])
def test_single_day_uses_exclusive_next_midnight(client, day, end):
    def handler(request):
        assert json.loads(request.content)["toDate"] == end
        return httpx.Response(201)
    transport(client, handler)
    result = register(client, from_date=day, to_date=day)
    assert result["fromDate"] == result["toDate"] == day


def test_contact_groups_and_outbound_ids(client):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/messages/contact-list"
        assert dict(request.url.params) == {"childId": "child", "organizationalUnitId": "school"}
        return httpx.Response(200, json={
            "relatedEmployees": [{"id": "contact", "employeeId": "different", "firstName": "Lærer"}],
            "otherEmployees": None, "legalGuardians": [{"id": "guardian"}],
            "communicationGroups": [{"groupId": "group", "employees": [{"id": "admin"}]}],
        })
    transport(client, handler)
    result = client.message_contacts("child", "school")
    assert result["relatedEmployees"][0]["recipient"] == {"type": "employee", "externalId": "contact"}
    assert result["legalGuardians"][0]["recipient"]["type"] == "legalGuardian"
    assert result["communicationGroups"][0]["employees"][0]["recipient"]["externalId"] == "admin"


@pytest.mark.parametrize("changes", [
    {"from_date": "20261009"}, {"from_date": "2026-02-30"}, {"to_date": "2026-10-08"},
    {"title": " "}, {"note": ""}, {"child_id": ""}, {"recipients": []},
    {"recipients": [{"type": "teacher", "externalId": "contact"}]},
    {"recipients": [{"type": "employee", "externalId": ""}]},
    {"recipients": [{"type": "employee", "externalId": "contact", "extra": "x"}]},
])
def test_invalid_inputs_never_send(client, changes):
    def handler(request):
        pytest.fail("Invalid input reached the API")
    transport(client, handler)
    with pytest.raises(ValueError):
        register(client, **changes)
    assert client._store.refreshes == []


@pytest.mark.parametrize("status", [400, 403, 409])
def test_http_errors_are_not_retried(client, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status)
    transport(client, handler)
    with pytest.raises(httpx.HTTPStatusError):
        register(client)
    assert len(calls) == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError])
def test_transport_failure_is_unknown_and_not_retried(client, error):
    calls = []
    def handler(request):
        calls.append(request)
        raise error("failure", request=request)
    transport(client, handler)
    with pytest.raises(WriteOutcomeUnknown, match="kan være registrert"):
        register(client)
    assert len(calls) == 1


@pytest.mark.parametrize("second_status", [204, 401])
def test_401_refresh_once(client, second_status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(401 if len(calls) == 1 else second_status)
    transport(client, handler)
    if second_status == 401:
        with pytest.raises(AuthError):
            register(client)
    else:
        assert register(client)["status"] == "registered"
    assert len(calls) == 2
    assert calls[1].headers["authorization"] == "Bearer new"


def test_tools_serialize_client_results(client, monkeypatch):
    monkeypatch.setattr(server, "_client", client)
    transport(client, lambda request: httpx.Response(204))
    result = json.loads(server.register_student_absence(
        "child", "school", "2026-10-09", "2026-10-10", "Syk", "Fravær",
        [{"type": "employee", "externalId": "contact"}],
    ))
    assert result["status"] == "registered"


def childcare_codes():
    return {"items": [
        {"organizationalUnitId": "nursery", "codes": [
            {"id": "free", "name": "Fri", "isDeleted": False},
            {"id": "deleted", "name": "Gammel", "isDeleted": True},
        ]},
        {"organizationalUnitId": "other", "codes": [{"id": "foreign", "name": "Fri", "isDeleted": False}]},
    ]}


def register_childcare(client, **changes):
    args = dict(child_id="child", organizational_unit_id="nursery", from_date="2026-12-24",
                to_date="2026-12-25", absence_code_id="free")
    args.update(changes)
    return client.register_childcare_absence(**args)


def test_absence_codes_are_active_and_scoped(client):
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/absencecodes"
        assert dict(request.url.params) == {"organizationalUnitIds": "nursery"}
        return httpx.Response(200, json=childcare_codes())
    transport(client, handler)
    assert client.absence_codes("nursery") == [{"id": "free", "name": "Fri", "isDeleted": False}]


@pytest.mark.parametrize("note", [None, "", "Fri"])
def test_childcare_request_and_empty_success(client, note):
    methods = []
    def handler(request):
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=childcare_codes())
        assert str(request.url) == API_BASE.rstrip("/") + "/api/absences"
        assert request.headers["authorization"] == "Bearer old"
        expected = {"childId": "child", "organizationalUnitId": "nursery", "fromDate": "24.12.2026",
                    "toDate": "26.12.2026", "absenceCodeId": "free"}
        if note is not None:
            expected["note"] = note
        assert json.loads(request.content) == expected
        return httpx.Response(201)
    transport(client, handler)
    result = register_childcare(client, note=note)
    assert result["absenceCode"] == {"id": "free", "name": "Fri"}
    assert result["toDate"] == "2026-12-25"
    assert methods == ["GET", "POST"]


@pytest.mark.parametrize("code", ["deleted", "foreign", "unknown"])
def test_childcare_rejects_inactive_and_wrong_unit_code(client, code):
    def handler(request):
        assert request.method == "GET", "Invalid code must not reach POST"
        return httpx.Response(200, json=childcare_codes())
    transport(client, handler)
    with pytest.raises(ValueError, match="aktiv fraværskode"):
        register_childcare(client, absence_code_id=code)


@pytest.mark.parametrize("changes", [{"absence_code_id": ""}, {"child_id": ""},
    {"organizational_unit_id": ""}, {"from_date": "2026-02-30"}, {"to_date": "2026-12-23"},
    {"to_date": "9999-12-31"}, {"note": 123}])
def test_invalid_childcare_fields_never_call_api(client, changes):
    transport(client, lambda request: pytest.fail("Invalid input reached API"))
    with pytest.raises(ValueError):
        register_childcare(client, **changes)


@pytest.mark.parametrize("childcare", [True, False])
@pytest.mark.parametrize("status", [500, 502, 504])
def test_server_failure_is_unknown_without_retry(client, childcare, status):
    posts = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=childcare_codes())
        posts.append(request)
        return httpx.Response(status)
    transport(client, handler)
    with pytest.raises(WriteOutcomeUnknown, match=f"HTTP {status}"):
        (register_childcare if childcare else register)(client)
    assert len(posts) == 1


def test_childcare_tool_serializes_result(client, monkeypatch):
    monkeypatch.setattr(server, "_client", client)
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=childcare_codes())
        return httpx.Response(204)
    transport(client, handler)
    result = json.loads(server.register_childcare_absence(
        "child", "nursery", "2026-12-24", "2026-12-25", "free",
    ))
    assert result["status"] == "registered"
    assert result["absenceCode"]["name"] == "Fri"
