import base64
import hashlib
import json
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

from vigilo_connector import config, server
from vigilo_connector.auth import AuthError
from vigilo_connector.client import WriteOutcomeUnknown
from vigilo_connector.gateway import app as gw
from vigilo_connector.gateway.settings import ConfigError, load_settings

PUBLIC = "https://vigilo.example.ts.net"
PASSWORD = "riktig-passord-123"
API_TOKEN = "t" * 40
CALLBACK = "http://localhost:33418/callback"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


class FakeVigilo:
    def __init__(self):
        self.fail = False

    def children(self):
        if self.fail:
            raise AuthError("Ingen tokens i /data/vigilo/tokens.json — kjør `vigilo-login` først.")
        return [{"childId": "1", "firstName": "Testbarn A", "lastName": "Testetternavn", "organizationalUnitId": "u", "school": "BHG", "group": "A"}]

    def register_student_absence(self, *args):
        if self.fail:
            raise WriteOutcomeUnknown("Ukjent resultat: kontroller fravær før et nytt forsøk.")
        return {"status": "registered", "childId": args[0], "recipients": args[-1]}

    def register_childcare_absence(self, *args):
        if self.fail:
            raise WriteOutcomeUnknown("Ukjent resultat: kontroller fravær før et nytt forsøk.")
        return {"status": "registered", "childId": args[0], "absenceCode": {"id": args[4], "name": "Fri"}}


@pytest.fixture
def fake():
    return FakeVigilo()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "vigilo")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "vigilo" / "config.json")
    return load_settings(
        {"DATA_DIR": str(tmp_path), "PUBLIC_URL": PUBLIC, "ADMIN_PASSWORD": PASSWORD, "API_TOKEN": API_TOKEN}
    )


@pytest.fixture
def client(settings, fake):
    old = server._client
    application = gw.create_app(settings, PUBLIC, vigilo_client=fake)
    with TestClient(application, base_url=PUBLIC, follow_redirects=False) as c:
        yield c
    server._client = old


def _sse_json(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    datas = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    return json.loads(datas[-1])


def _rpc(client, token, method, params=None, id_=1, session=None):
    headers = dict(MCP_HEADERS, authorization=f"Bearer {token}")
    if session:
        headers["mcp-session-id"] = session
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def _mcp_session(client, token):
    r = _rpc(client, token, "initialize", {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert r.status_code == 200, r.text
    session = r.headers.get("mcp-session-id")
    _rpc(client, token, "notifications/initialized", id_=None, session=session)
    return session


def _oauth_token(client) -> dict:
    reg = client.post("/register", json={
        "redirect_uris": [CALLBACK],
        "client_name": "Testklient",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz",
        "scope": "vigilo", "resource": f"{PUBLIC}/mcp",
    })
    assert r.status_code == 302, r.text
    login_url = r.headers["location"]
    assert login_url.startswith(f"{PUBLIC}/login?req=")

    page = client.get(login_url)
    assert "Testklient" in page.text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    req = parse_qs(urlsplit(login_url).query)["req"][0]
    r = client.post("/login", data={"req": req, "password": PASSWORD, "csrf": csrf})
    assert r.status_code == 302
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["state"] == ["xyz"]

    tok = client.post("/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": verifier, "resource": f"{PUBLIC}/mcp",
    })
    assert tok.status_code == 200, tok.text
    return {**tok.json(), "client_id": client_id}


def test_mcp_uten_token_gir_401_med_metadata(client):
    r = client.post("/mcp", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_metadata(client):
    as_meta = client.get("/.well-known/oauth-authorization-server").json()
    assert as_meta["issuer"].rstrip("/") == PUBLIC
    assert as_meta["registration_endpoint"] == f"{PUBLIC}/register"
    pr = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert pr["resource"] == f"{PUBLIC}/mcp"


def test_full_oauth_flyt_og_verktoykall(client):
    tok = _oauth_token(client)
    assert tok["token_type"] == "Bearer" and tok["refresh_token"]

    session = _mcp_session(client, tok["access_token"])
    tools = _sse_json(_rpc(client, tok["access_token"], "tools/list", {}, id_=2, session=session))
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"web_list_children", "message_contacts", "register_student_absence"} <= names
    assert {"absence_codes", "register_childcare_absence"} <= names
    assert len(names) == 17

    res = _sse_json(_rpc(client, tok["access_token"], "tools/call",
                         {"name": "web_list_children", "arguments": {}}, id_=3, session=session))
    assert res["result"].get("isError") is not True
    assert "Testbarn A" in res["result"]["content"][0]["text"]

    # refresh roterer
    new = client.post("/token", data={
        "grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": tok["client_id"],
    })
    assert new.status_code == 200, new.text
    assert new.json()["refresh_token"] != tok["refresh_token"]


def test_api_token_virker(client):
    session = _mcp_session(client, API_TOKEN)
    tools = _sse_json(_rpc(client, API_TOKEN, "tools/list", {}, id_=2, session=session))
    assert len(tools["result"]["tools"]) == 17


def test_verktoy_uten_vigilo_innlogging_peker_til_setup(client, fake):
    fake.fail = True
    session = _mcp_session(client, API_TOKEN)
    res = _sse_json(_rpc(client, API_TOKEN, "tools/call",
                         {"name": "web_list_children", "arguments": {}}, id_=3, session=session))
    assert res["result"]["isError"] is True
    text = res["result"]["content"][0]["text"]
    assert f"{PUBLIC}/setup" in text


@pytest.mark.parametrize("unknown", [False, True])
def test_skolefravaer_gjennom_gateway(client, fake, unknown):
    fake.fail = unknown
    session = _mcp_session(client, API_TOKEN)
    result = _sse_json(_rpc(client, API_TOKEN, "tools/call", {
        "name": "register_student_absence", "arguments": {
            "child_id": "child", "organizational_unit_id": "school",
            "from_date": "2026-10-09", "to_date": "2026-10-09",
            "note": "Syk", "title": "Fravær",
            "recipients": [{"type": "employee", "externalId": "contact"}],
        },
    }, id_=3, session=session))["result"]
    if unknown:
        assert result["isError"] is True
        assert "kontroller fravær" in result["content"][0]["text"]
    else:
        assert result.get("isError") is not True
        assert "registered" in result["content"][0]["text"]


def test_feil_host_avvises(client):
    r = client.post("/mcp", headers=dict(MCP_HEADERS, authorization=f"Bearer {API_TOKEN}", host="evil.example"),
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code in (400, 421)


@pytest.mark.parametrize("unknown", [False, True])
def test_barnehagefravaer_gjennom_gateway(client, fake, unknown):
    fake.fail = unknown
    session = _mcp_session(client, API_TOKEN)
    result = _sse_json(_rpc(client, API_TOKEN, "tools/call", {
        "name": "register_childcare_absence", "arguments": {
            "child_id": "child", "organizational_unit_id": "nursery",
            "from_date": "2026-12-24", "to_date": "2026-12-25", "absence_code_id": "free",
        },
    }, id_=3, session=session))["result"]
    if unknown:
        assert result["isError"] is True
        assert "kontroller fravær" in result["content"][0]["text"]
    else:
        assert result.get("isError") is not True
        assert "Fri" in result["content"][0]["text"]


def test_feil_config_dir_stopper_oppstart(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "annet")
    s = load_settings({"DATA_DIR": str(tmp_path), "PUBLIC_URL": PUBLIC, "ADMIN_PASSWORD": PASSWORD})
    with pytest.raises(ConfigError, match="VIGILO_CONFIG_DIR"):
        gw.create_app(s, PUBLIC)


def _status_transport(payload, status=200):
    def handler(request):
        assert request.url.path == "/localapi/v0/status"
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def test_public_url_fra_tailscale(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path)})
    transport = _status_transport({"BackendState": "Running", "Self": {"DNSName": "vigilo.tail123.ts.net."}})
    assert gw.resolve_public_url(s, transport=transport) == "https://vigilo.tail123.ts.net"


def test_public_url_fra_miljo_vinner(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "PUBLIC_URL": PUBLIC})
    assert gw.resolve_public_url(s, transport=None) == PUBLIC


def test_public_url_timeout(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "TAILSCALE_SOCKET": str(tmp_path / "finnes-ikke.sock")})
    with pytest.raises(ConfigError, match="PUBLIC_URL"):
        gw.resolve_public_url(s, timeout=0.3, interval=0.1)


def test_public_url_venter_paa_running(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path)})
    transport = _status_transport({"BackendState": "NeedsLogin", "Self": {"DNSName": ""}})
    with pytest.raises(ConfigError):
        gw.resolve_public_url(s, transport=transport, timeout=0.3, interval=0.1)


def test_show_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    gw.main(["show-password"])
    out = capsys.readouterr().out.strip()
    assert out == (tmp_path / "admin_password").read_text().strip()


def test_verktoyskjema_bevares_gjennom_feilinnpakning(client):
    session = _mcp_session(client, API_TOKEN)
    tools = _sse_json(_rpc(client, API_TOKEN, "tools/list", {}, id_=2, session=session))["result"]["tools"]
    timetable = next(t for t in tools if t["name"] == "timetable")
    assert set(timetable["inputSchema"]["required"]) == {"child_id", "organizational_unit_id"}
    assert "ISO-uke" in timetable["description"]
    childcare = next(t for t in tools if t["name"] == "register_childcare_absence")
    assert set(childcare["inputSchema"]["required"]) == {
        "child_id", "organizational_unit_id", "from_date", "to_date", "absence_code_id",
    }
