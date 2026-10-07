import hashlib
import sqlite3

import pytest

from vigilo_connector.gateway.oauth_store import OAuthStore


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return OAuthStore(tmp_path / "oauth.db", now=clock)


def _all_values(path) -> str:
    con = sqlite3.connect(path)
    rows = []
    for (table,) in con.execute("select name from sqlite_master where type='table'"):
        rows += [repr(r) for r in con.execute(f"select * from {table}")]
    return "\n".join(rows)


def test_klienter_lagres_og_telles(store):
    assert store.get_client("c1") is None
    store.save_client("c1", '{"client_id": "c1"}')
    assert store.get_client("c1") == '{"client_id": "c1"}'
    assert store.client_count() == 1


def test_ventende_authorize_utloper(store, clock):
    req = store.create_pending({"client_id": "c1"}, ttl=600)
    assert store.get_pending(req) == {"client_id": "c1"}
    clock.t += 601
    assert store.get_pending(req) is None


def test_kode_er_engangs_og_utloper(store, clock):
    code = store.create_code({"client_id": "c1"}, ttl=300)
    assert store.peek_code(code) == {"client_id": "c1"}
    assert store.take_code(code) == {"client_id": "c1"}
    assert store.take_code(code) is None

    code2 = store.create_code({"client_id": "c1"}, ttl=300)
    clock.t += 301
    assert store.peek_code(code2) is None
    assert store.take_code(code2) is None


def test_kun_hasher_lagres(store, tmp_path):
    code = store.create_code({"client_id": "c1"}, ttl=300)
    access, refresh, _ = store.issue_tokens("c1", ["vigilo"], None)
    dump = _all_values(tmp_path / "oauth.db")
    for secret in (code, access, refresh):
        assert secret not in dump
        assert hashlib.sha256(secret.encode()).hexdigest() in dump


def test_access_token_utloper(store, clock):
    access, _, expires_in = store.issue_tokens("c1", ["vigilo"], "https://x/mcp")
    assert expires_in == 3600
    info = store.get_access(access)
    assert info["client_id"] == "c1"
    assert info["scopes"] == ["vigilo"]
    assert info["resource"] == "https://x/mcp"
    clock.t += 3601
    assert store.get_access(access) is None


def test_refresh_roteres(store):
    access, refresh, _ = store.issue_tokens("c1", ["vigilo"], None)
    new_access, new_refresh, _ = store.rotate_refresh(refresh)
    assert new_refresh != refresh
    assert store.get_refresh(refresh) is None
    assert store.get_refresh(new_refresh)["client_id"] == "c1"
    assert store.get_access(new_access) is not None


def test_gjenbruk_av_rotert_refresh_trekker_tilbake_alt(store):
    _, refresh, _ = store.issue_tokens("c1", ["vigilo"], None)
    new_access, new_refresh, _ = store.rotate_refresh(refresh)
    other_access, _, _ = store.issue_tokens("c2", ["vigilo"], None)

    assert store.rotate_refresh(refresh) is None
    assert store.get_access(new_access) is None
    assert store.get_refresh(new_refresh) is None
    assert store.get_access(other_access) is not None


def test_refresh_utloper(store, clock):
    _, refresh, _ = store.issue_tokens("c1", ["vigilo"], None)
    clock.t += 90 * 86400 + 1
    assert store.get_refresh(refresh) is None
    assert store.rotate_refresh(refresh) is None


def test_revoke_fjerner_hele_paret(store):
    access, refresh, _ = store.issue_tokens("c1", ["vigilo"], None)
    store.revoke(access)
    assert store.get_access(access) is None
    assert store.get_refresh(refresh) is None


def test_overlever_ny_instans(tmp_path, clock):
    a = OAuthStore(tmp_path / "oauth.db", now=clock)
    access, _, _ = a.issue_tokens("c1", ["vigilo"], None)
    b = OAuthStore(tmp_path / "oauth.db", now=clock)
    assert b.get_access(access) is not None
