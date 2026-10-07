import json
import stat

import pytest

from vigilo_connector import auth, config
from vigilo_connector.gateway.vigilo_setup import SetupError, VigiloSetup, classify_input

CHROME_CURL = """curl 'https://auth.prod.vigilo-oas.no/connect/authorize?client_id=x' \\
  -H 'accept: text/html' \\
  -b 'idsrv.session=abc; .AspNetCore.Identity=def' \\
  -H 'user-agent: Mozilla/5.0'"""

FIREFOX_CURL = """curl 'https://auth.prod.vigilo-oas.no/connect/authorize?client_id=x' \\
  -H 'User-Agent: Mozilla/5.0' \\
  -H 'Cookie: idsrv.session=abc; .AspNetCore.Identity=def' \\
  -H 'Sec-Fetch-Dest: document'"""

RAW_302 = """HTTP/2 302
cache-control: no-store, max-age=0
location: app://ch-parent-android.vigilo.no/?code=c0ffee&state=st8&session_state=zzz
server: Microsoft-HTTPAPI/2.0"""


def test_app_url():
    assert classify_input("app://ch-parent-android.vigilo.no?code=abc&state=st8", "st8") == ("code", "abc")


def test_app_url_med_feil_state_avvises():
    with pytest.raises(SetupError, match="state"):
        classify_input("app://ch-parent-android.vigilo.no?code=abc&state=annen", "st8")


def test_bar_kode():
    assert classify_input("  b6ab44d7c4d3daac7961be8eb3ed6f85 \n", None) == (
        "code",
        "b6ab44d7c4d3daac7961be8eb3ed6f85",
    )


def test_chrome_curl():
    assert classify_input(CHROME_CURL, None) == ("cookies", "idsrv.session=abc; .AspNetCore.Identity=def")


def test_firefox_curl():
    assert classify_input(FIREFOX_CURL, None) == ("cookies", "idsrv.session=abc; .AspNetCore.Identity=def")


def test_raatt_302_svar_godtar_annen_state():
    # Svaret kommer fra en innlogging startet i nettleseren, ikke fra /setup,
    # så state er en annen. Koden er like gyldig.
    assert classify_input(RAW_302, "st8-fra-setup") == ("code", "c0ffee")


@pytest.mark.parametrize("raw", ["", "   ", "curl 'https://x' -H 'accept: */*'", "hei og hopp"])
def test_soppel_gir_setup_error(raw):
    with pytest.raises(SetupError):
        classify_input(raw, None)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    vdir = tmp_path / "vigilo"
    monkeypatch.setattr(config, "CONFIG_FILE", vdir / "config.json")
    monkeypatch.delenv("VIGILO_CLIENT_ID", raising=False)
    monkeypatch.delenv("VIGILO_CLIENT_SECRET", raising=False)
    children = [{"childId": "1", "firstName": "Testbarn A", "school": "BHG", "group": "A"}]
    return VigiloSetup(vdir, children_fn=lambda store: children)


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def test_status_uten_noe(setup):
    s = setup.status()
    assert s == {"credentials": False, "client_id": None, "tokens": False, "obtained_at": None}


def test_lagre_nokler(setup, tmp_path):
    setup.save_credentials(" id.ANDROID ", " hemmelig ")
    p = tmp_path / "vigilo" / "config.json"
    assert json.loads(p.read_text()) == {"client_id": "id.ANDROID", "client_secret": "hemmelig"}
    assert _mode(p) == 0o600
    s = setup.status()
    assert s["credentials"] is True and s["client_id"] == "id.ANDROID"
    assert "hemmelig" not in json.dumps(s)


def test_lagre_tomme_nokler_avvises(setup):
    with pytest.raises(SetupError):
        setup.save_credentials("", "x")


def test_import_tokens(setup, tmp_path):
    with pytest.raises(SetupError, match="refresh_token"):
        setup.import_tokens(json.dumps({"access_token": "a"}).encode())
    with pytest.raises(SetupError):
        setup.import_tokens(b"ikke json")
    setup.import_tokens(json.dumps({"access_token": "a", "refresh_token": "r", "expires_in": 3600}).encode())
    p = tmp_path / "vigilo" / "tokens.json"
    assert json.loads(p.read_text())["refresh_token"] == "r"
    assert _mode(p) == 0o600
    assert setup.status()["tokens"] is True


def test_start_login_krever_nokler(setup):
    with pytest.raises(SetupError, match="nøkl"):
        setup.start_login()
    setup.save_credentials("id.ANDROID", "s")
    url, state = setup.start_login()
    assert "client_id=id.ANDROID" in url and f"state={state}" in url


def test_finish_login_med_kode(setup, monkeypatch, tmp_path):
    setup.save_credentials("id.ANDROID", "s")
    monkeypatch.setattr(auth, "exchange_code", lambda code: {"access_token": "a", "refresh_token": "r", "expires_in": 3600})
    children = setup.finish_login("0123456789abcdef0123456789abcdef", "st8")
    assert children[0]["firstName"] == "Testbarn A"
    assert json.loads((tmp_path / "vigilo" / "tokens.json").read_text())["access_token"] == "a"


def test_finish_login_med_curl(setup, monkeypatch):
    setup.save_credentials("id.ANDROID", "s")
    seen = {}
    monkeypatch.setattr(auth, "code_from_cookies", lambda c: seen.setdefault("cookies", c) and "kode")
    monkeypatch.setattr(auth, "exchange_code", lambda code: seen.setdefault("code", code) and {"access_token": "a", "refresh_token": "r", "expires_in": 1})
    setup.finish_login(CHROME_CURL, None)
    assert seen == {"cookies": "idsrv.session=abc; .AspNetCore.Identity=def", "code": "kode"}


def test_brukt_kode_gir_norsk_forklaring(setup, monkeypatch):
    setup.save_credentials("id.ANDROID", "s")

    def boom(code):
        raise auth.AuthError('Token-kall avvist (400): {"error":"invalid_grant"}')

    monkeypatch.setattr(auth, "exchange_code", boom)
    with pytest.raises(SetupError, match="brukt eller utløpt"):
        setup.finish_login("0123456789abcdef0123456789abcdef", None)


def test_test_connection_uten_tokens(setup):
    with pytest.raises(SetupError, match="logget inn"):
        setup.test_connection()
