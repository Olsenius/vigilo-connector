import re
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.testclient import TestClient

from vigilo_connector.gateway.oauth_provider import VigiloOAuthProvider
from vigilo_connector.gateway.oauth_store import OAuthStore
from vigilo_connector.gateway.vigilo_setup import SetupError
from vigilo_connector.gateway.web import LoginLimiter, build_routes

PUBLIC = "https://vigilo.example.ts.net"
PASSWORD = "riktig-passord-123"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"


class FakeSetup:
    def __init__(self):
        self.calls = []
        self.fail_finish = False

    def status(self):
        return {"credentials": True, "client_id": "id.ANDROID", "tokens": True, "obtained_at": 1_700_000_000}

    def save_credentials(self, cid, secret):
        self.calls.append(("credentials", cid, secret))

    def import_tokens(self, data):
        self.calls.append(("import", data))
        if data == b"feil":
            raise SetupError("Fila mangler access_token eller refresh_token.")

    def start_login(self):
        return "https://auth.prod.vigilo-oas.no/connect/authorize?client_id=x&state=STATE1", "STATE1"

    def finish_login(self, raw, state):
        self.calls.append(("finish", raw, state))
        if self.fail_finish:
            raise SetupError("Koden er allerede brukt eller utløpt.")
        return [{"firstName": "Testbarn A", "lastName": "Testetternavn", "school": "BHG", "group": "Testavdeling"}]

    def test_connection(self):
        return [{"firstName": "Testbarn B", "lastName": "Testetternavn", "school": "Skole", "group": "Testklasse"}]


class Env:
    def __init__(self, tmp_path):
        self.store = OAuthStore(tmp_path / "oauth.db")
        self.provider = VigiloOAuthProvider(self.store, PUBLIC, api_token=None)
        self.setup = FakeSetup()
        self.limiter = LoginLimiter()
        app = Starlette(
            routes=build_routes(
                provider=self.provider,
                store=self.store,
                setup=self.setup,
                password=PASSWORD,
                session_secret=b"s" * 32,
                limiter=self.limiter,
            )
        )
        self.client = TestClient(app, base_url="https://testserver", follow_redirects=False)

    async def pending(self, client_name="Claude"):
        client = OAuthClientInformationFull(
            client_id="c1", client_name=client_name, redirect_uris=[AnyUrl(CALLBACK)], scope="vigilo"
        )
        await self.provider.register_client(client)
        url = await self.provider.authorize(
            client,
            AuthorizationParams(
                state="st8",
                scopes=["vigilo"],
                code_challenge="c" * 43,
                redirect_uri=AnyUrl(CALLBACK),
                redirect_uri_provided_explicitly=True,
            ),
        )
        return parse_qs(urlsplit(url).query)["req"][0]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _login_post(env, req, password, csrf=None):
    page = env.client.get(f"/login?req={req}")
    return env.client.post(
        "/login", data={"req": req, "password": password, "csrf": csrf if csrf is not None else _csrf(page.text)}
    )


def test_healthz(env):
    r = env.client.get("/healthz")
    assert r.status_code == 200 and r.text == "ok"


def test_login_ukjent_forespørsel(env):
    assert env.client.get("/login?req=finnes-ikke").status_code == 400


async def test_login_side_escaper_klientnavn(env):
    req = await env.pending(client_name="<script>alert(1)</script>")
    r = env.client.get(f"/login?req={req}")
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert CALLBACK in r.text
    assert "no-store" in r.headers["cache-control"]


async def test_login_riktig_passord_redirecter_med_kode(env):
    req = await env.pending()
    r = _login_post(env, req, PASSWORD)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith(CALLBACK + "?")
    q = parse_qs(urlsplit(loc).query)
    assert q["state"] == ["st8"] and q["code"]


async def test_login_feil_passord(env):
    req = await env.pending()
    r = _login_post(env, req, "feil-passord-xyz")
    assert r.status_code == 401
    assert "Feil passord" in r.text


async def test_login_uten_csrf_avvises(env):
    req = await env.pending()
    r = _login_post(env, req, PASSWORD, csrf="tull")
    assert r.status_code == 403


async def test_login_sperres_etter_fem_feil(env):
    req = await env.pending()
    for _ in range(5):
        assert _login_post(env, req, "feil-passord-xyz").status_code == 401
    r = _login_post(env, req, PASSWORD)
    assert r.status_code == 429


async def test_sperre_er_per_ip(env):
    req = await env.pending()
    for _ in range(5):
        page = env.client.get(f"/login?req={req}")
        env.client.post(
            "/login",
            data={"req": req, "password": "feil-passord-xyz", "csrf": _csrf(page.text)},
            headers={"X-Forwarded-For": "203.0.113.9"},
        )
    page = env.client.get(f"/login?req={req}")
    r = env.client.post(
        "/login",
        data={"req": req, "password": PASSWORD, "csrf": _csrf(page.text)},
        headers={"X-Forwarded-For": "198.51.100.7"},
    )
    assert r.status_code == 302


def _setup_login(env, password=PASSWORD):
    page = env.client.get("/setup")
    return env.client.post("/setup/login", data={"password": password, "csrf": _csrf(page.text)})


def test_setup_krever_innlogging(env):
    r = env.client.get("/setup")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert "id.ANDROID" not in r.text


def test_setup_innlogging_setter_sikker_cookie(env):
    r = _setup_login(env)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    page = env.client.get("/setup")
    assert "id.ANDROID" in page.text


def test_setup_feil_passord(env):
    r = _setup_login(env, "feil-passord-xyz")
    assert r.status_code == 401


def test_setup_handlinger_krever_okt(env):
    page = env.client.get("/setup")
    r = env.client.post("/setup/finish", data={"raw": "x", "csrf": _csrf(page.text)})
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    assert env.setup.calls == []


def test_setup_start_og_fullfor_vigilo_login(env):
    _setup_login(env)
    page = env.client.get("/setup")
    r = env.client.post("/setup/start", data={"csrf": _csrf(page.text)})
    assert r.status_code == 200
    assert "https://auth.prod.vigilo-oas.no/connect/authorize?client_id=x&amp;state=STATE1" in r.text
    r = env.client.post("/setup/finish", data={"raw": "app://x?code=abc&state=STATE1", "csrf": _csrf(r.text)})
    assert r.status_code == 200
    assert "Testbarn A" in r.text and "Testavdeling" in r.text
    assert env.setup.calls[-1] == ("finish", "app://x?code=abc&state=STATE1", "STATE1")


def test_setup_feil_vises_paa_siden(env):
    _setup_login(env)
    env.setup.fail_finish = True
    page = env.client.get("/setup")
    r = env.client.post("/setup/finish", data={"raw": "søppel", "csrf": _csrf(page.text)})
    assert r.status_code == 200
    assert "brukt eller utløpt" in r.text


def test_setup_import_og_nokler(env):
    _setup_login(env)
    page = env.client.get("/setup")
    csrf = _csrf(page.text)
    r = env.client.post("/setup/import", data={"csrf": csrf}, files={"tokens": ("tokens.json", b"feil")})
    assert "mangler access_token" in r.text
    env.client.post("/setup/credentials", data={"csrf": csrf, "client_id": "a", "client_secret": "b"})
    assert ("credentials", "a", "b") in env.setup.calls


def test_setup_test_tilkobling(env):
    _setup_login(env)
    page = env.client.get("/setup")
    r = env.client.post("/setup/test", data={"csrf": _csrf(page.text)})
    assert "Testbarn B" in r.text and "Testklasse" in r.text


def test_setup_csrf(env):
    _setup_login(env)
    r = env.client.post("/setup/test", data={"csrf": "tull"})
    assert r.status_code == 403


def test_limiter_slipper_til_etter_sperretid():
    t = [0.0]
    lim = LoginLimiter(now=lambda: t[0])
    for _ in range(5):
        lim.fail("ip")
    assert lim.blocked("ip")
    t[0] += 15 * 60 + 1
    assert not lim.blocked("ip")


async def test_forfalsket_x_forwarded_for_omgaar_ikke_sperren(env):
    # Klienten kan selv sende X-Forwarded-For; proxyen (Tailscale) legger til
    # den ekte IP-en sist. Bare den siste verdien kan stoles på.
    req = await env.pending()
    for i in range(5):
        page = env.client.get(f"/login?req={req}")
        env.client.post(
            "/login",
            data={"req": req, "password": "feil-passord-xyz", "csrf": _csrf(page.text)},
            headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.9"},
        )
    page = env.client.get(f"/login?req={req}")
    r = env.client.post(
        "/login",
        data={"req": req, "password": PASSWORD, "csrf": _csrf(page.text)},
        headers={"X-Forwarded-For": "10.0.0.99, 203.0.113.9"},
    )
    assert r.status_code == 429


def test_limiter_vokser_ikke_uten_grense():
    lim = LoginLimiter(max_keys=100)
    for i in range(1000):
        lim.fail(f"ip{i}")
    assert len(lim._failures) <= 100
