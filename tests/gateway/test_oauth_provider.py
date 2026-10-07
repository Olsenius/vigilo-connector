import time
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from vigilo_connector.gateway.oauth_provider import VigiloOAuthProvider
from vigilo_connector.gateway.oauth_store import OAuthStore

PUBLIC = "https://vigilo.example.ts.net"
API_TOKEN = "s" * 40


@pytest.fixture
def store(tmp_path):
    return OAuthStore(tmp_path / "oauth.db")


@pytest.fixture
def provider(store):
    return VigiloOAuthProvider(store, PUBLIC, api_token=API_TOKEN)


def _client(cid="c1", redirect="https://claude.ai/api/mcp/auth_callback", name="Claude"):
    return OAuthClientInformationFull(
        client_id=cid,
        client_name=name,
        redirect_uris=[AnyUrl(redirect)],
        scope="vigilo",
        token_endpoint_auth_method="none",
    )


def _params(redirect="https://claude.ai/api/mcp/auth_callback", state="st8"):
    return AuthorizationParams(
        state=state,
        scopes=["vigilo"],
        code_challenge="ch" * 22,
        redirect_uri=AnyUrl(redirect),
        redirect_uri_provided_explicitly=True,
        resource=f"{PUBLIC}/mcp",
    )


async def _authorize_and_complete(provider, client, params):
    await provider.register_client(client)
    login_url = await provider.authorize(client, params)
    req_id = parse_qs(urlsplit(login_url).query)["req"][0]
    return provider.complete_authorization(req_id)


async def test_klient_roundtrip(provider):
    client = _client()
    await provider.register_client(client)
    loaded = await provider.get_client("c1")
    assert loaded.client_name == "Claude"
    assert await provider.get_client("ukjent") is None


async def test_authorize_sender_til_login(provider):
    client = _client()
    await provider.register_client(client)
    url = await provider.authorize(client, _params())
    assert url.startswith(f"{PUBLIC}/login?req=")
    req_id = parse_qs(urlsplit(url).query)["req"][0]
    info = provider.pending_info(req_id)
    assert info == {"client_name": "Claude", "redirect_uri": "https://claude.ai/api/mcp/auth_callback"}


async def test_fullfort_authorize_gir_kode_og_state(provider):
    redirect = await _authorize_and_complete(provider, _client(), _params())
    q = parse_qs(urlsplit(redirect).query)
    assert redirect.startswith("https://claude.ai/api/mcp/auth_callback?")
    assert q["state"] == ["st8"]
    assert q["code"][0]


async def test_redirect_bevarer_eksisterende_query_for_native_klienter(provider):
    cb = "http://localhost:33418/callback?x=1"
    redirect = await _authorize_and_complete(provider, _client(redirect=cb), _params(redirect=cb))
    parts = urlsplit(redirect)
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "localhost:33418", "/callback")
    q = parse_qs(parts.query)
    assert q["x"] == ["1"] and q["state"] == ["st8"] and q["code"]


async def test_ventende_forespørsel_kan_bare_fullfores_en_gang(provider):
    client = _client()
    await provider.register_client(client)
    url = await provider.authorize(client, _params())
    req_id = parse_qs(urlsplit(url).query)["req"][0]
    provider.complete_authorization(req_id)
    with pytest.raises(KeyError):
        provider.complete_authorization(req_id)


async def test_kode_byttes_mot_tokens(provider):
    client = _client()
    redirect = await _authorize_and_complete(provider, client, _params())
    code = parse_qs(urlsplit(redirect).query)["code"][0]

    assert await provider.load_authorization_code(_client(cid="annen"), code) is None
    ac = await provider.load_authorization_code(client, code)
    assert ac.code_challenge == "ch" * 22
    assert ac.expires_at > time.time()  # SDK-en avviser koder med expires_at i fortiden
    assert ac.resource == f"{PUBLIC}/mcp"

    tok = await provider.exchange_authorization_code(client, ac)
    assert tok.token_type == "Bearer"
    assert tok.expires_in == 3600
    assert tok.scope == "vigilo"
    assert tok.refresh_token

    at = await provider.load_access_token(tok.access_token)
    assert at.client_id == "c1"
    assert at.resource == f"{PUBLIC}/mcp"

    # koden er brukt opp
    assert await provider.load_authorization_code(client, code) is None


async def test_refresh_roteres(provider):
    client = _client()
    redirect = await _authorize_and_complete(provider, client, _params())
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    tok = await provider.exchange_authorization_code(client, await provider.load_authorization_code(client, code))

    rt = await provider.load_refresh_token(client, tok.refresh_token)
    assert await provider.load_refresh_token(_client(cid="annen"), tok.refresh_token) is None
    new = await provider.exchange_refresh_token(client, rt, ["vigilo"])
    assert new.refresh_token != tok.refresh_token
    assert await provider.load_access_token(tok.access_token) is None
    assert await provider.load_access_token(new.access_token) is not None


async def test_api_token(store):
    on = VigiloOAuthProvider(store, PUBLIC, api_token=API_TOKEN)
    off = VigiloOAuthProvider(store, PUBLIC, api_token=None)
    at = await on.load_access_token(API_TOKEN)
    assert at.client_id == "api-token"
    assert at.scopes == ["vigilo"]
    assert await off.load_access_token(API_TOKEN) is None
    assert await on.load_access_token("feil") is None


async def test_revoke(provider):
    client = _client()
    redirect = await _authorize_and_complete(provider, client, _params())
    code = parse_qs(urlsplit(redirect).query)["code"][0]
    tok = await provider.exchange_authorization_code(client, await provider.load_authorization_code(client, code))
    at = await provider.load_access_token(tok.access_token)
    await provider.revoke_token(at)
    assert await provider.load_access_token(tok.access_token) is None
    assert await provider.load_refresh_token(client, tok.refresh_token) is None
