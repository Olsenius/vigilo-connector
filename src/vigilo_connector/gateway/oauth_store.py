"""SQLite-lagring for OAuth-tilstanden: klienter, ventende authorize, koder og tokens.

Koder og tokens lagres kun som SHA-256-hasher, så en kopi av databasen ikke
kan brukes til å logge inn. Refresh-tokens roteres; brukes et allerede rotert
refresh-token igjen, tolkes det som lekkasje og alle klientens tokens trekkes
tilbake (OAuth 2.1 §4.3.1).
"""

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

ACCESS_TTL = 3600
REFRESH_TTL = 90 * 86400

_SCHEMA = """
create table if not exists clients (client_id text primary key, info text not null);
create table if not exists pending (id text primary key, data text not null, expires_at real not null);
create table if not exists codes (hash text primary key, data text not null, expires_at real not null);
create table if not exists tokens (
    hash text primary key,
    kind text not null,            -- 'access' | 'refresh'
    pair_id text not null,         -- access og refresh fra samme utstedelse
    client_id text not null,
    scopes text not null,
    resource text,
    expires_at real not null,
    used integer not null default 0
);
create index if not exists tokens_client on tokens (client_id);
create index if not exists tokens_pair on tokens (pair_id);
"""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


class OAuthStore:
    def __init__(self, path: Path, now: Callable[[], float] = time.time):
        self._now = now
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.executescript(_SCHEMA)
        path.chmod(0o600)

    def _q(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # --- Klienter (DCR) ----------------------------------------------------

    def save_client(self, client_id: str, info_json: str) -> None:
        self._q("insert or replace into clients values (?, ?)", (client_id, info_json))

    def get_client(self, client_id: str) -> str | None:
        rows = self._q("select info from clients where client_id = ?", (client_id,))
        return rows[0][0] if rows else None

    def client_count(self) -> int:
        return self._q("select count(*) from clients")[0][0]

    # --- Ventende authorize-forespørsler -------------------------------------

    def create_pending(self, data: dict, ttl: float) -> str:
        req_id = _new_token()
        self._q(
            "insert into pending values (?, ?, ?)",
            (req_id, json.dumps(data), self._now() + ttl),
        )
        return req_id

    def get_pending(self, req_id: str) -> dict | None:
        rows = self._q(
            "select data from pending where id = ? and expires_at > ?", (req_id, self._now())
        )
        return json.loads(rows[0][0]) if rows else None

    def delete_pending(self, req_id: str) -> None:
        self._q("delete from pending where id = ?", (req_id,))

    # --- Autorisasjonskoder --------------------------------------------------

    def create_code(self, data: dict, ttl: float) -> str:
        code = _new_token()
        self._q(
            "insert into codes values (?, ?, ?)",
            (_hash(code), json.dumps(data), self._now() + ttl),
        )
        return code

    def peek_code(self, code: str) -> dict | None:
        """Les koden uten å bruke den opp. Resultatet har også `expires_at`."""
        rows = self._q(
            "select data, expires_at from codes where hash = ? and expires_at > ?",
            (_hash(code), self._now()),
        )
        return {**json.loads(rows[0][0]), "expires_at": rows[0][1]} if rows else None

    def take_code(self, code: str) -> dict | None:
        """Hent og slett koden atomisk — en kode kan bare brukes én gang."""
        with self._lock:
            rows = self._db.execute(
                "delete from codes where hash = ? returning data, expires_at", (_hash(code),)
            ).fetchall()
        if not rows or rows[0][1] <= self._now():
            return None
        return json.loads(rows[0][0])

    # --- Tokens --------------------------------------------------------------

    def issue_tokens(
        self, client_id: str, scopes: list[str], resource: str | None
    ) -> tuple[str, str, int]:
        access, refresh, pair_id = _new_token(), _new_token(), _new_token()
        now = self._now()
        scopes_json = json.dumps(scopes)
        with self._lock:
            self._db.executemany(
                "insert into tokens (hash, kind, pair_id, client_id, scopes, resource, expires_at)"
                " values (?, ?, ?, ?, ?, ?, ?)",
                [
                    (_hash(access), "access", pair_id, client_id, scopes_json, resource, now + ACCESS_TTL),
                    (_hash(refresh), "refresh", pair_id, client_id, scopes_json, resource, now + REFRESH_TTL),
                ],
            )
        return access, refresh, ACCESS_TTL

    def _get(self, token: str, kind: str) -> dict | None:
        rows = self._q(
            "select client_id, scopes, resource, expires_at from tokens"
            " where hash = ? and kind = ? and used = 0 and expires_at > ?",
            (_hash(token), kind, self._now()),
        )
        if not rows:
            return None
        client_id, scopes, resource, expires_at = rows[0]
        return {
            "client_id": client_id,
            "scopes": json.loads(scopes),
            "resource": resource,
            "expires_at": int(expires_at),
        }

    def get_access(self, token: str) -> dict | None:
        return self._get(token, "access")

    def get_refresh(self, token: str) -> dict | None:
        return self._get(token, "refresh")

    def rotate_refresh(self, token: str) -> tuple[str, str, int] | None:
        h = _hash(token)
        rows = self._q(
            "select client_id, scopes, resource, expires_at, used, pair_id from tokens"
            " where hash = ? and kind = 'refresh'",
            (h,),
        )
        if not rows:
            return None
        client_id, scopes, resource, expires_at, used, pair_id = rows[0]
        if expires_at <= self._now():
            return None
        # Merk som brukt (ikke slett) så gjenbruk kan oppdages. Atomisk: bare ett
        # av to samtidige kall vinner; taperen behandles som gjenbruk.
        with self._lock:
            claimed = self._db.execute(
                "update tokens set used = 1 where hash = ? and used = 0", (h,)
            ).rowcount
        if used or not claimed:
            self.revoke_client_tokens(client_id)
            return None
        self._q("delete from tokens where pair_id = ? and kind = 'access'", (pair_id,))
        return self.issue_tokens(client_id, json.loads(scopes), resource)

    def revoke_client_tokens(self, client_id: str) -> None:
        self._q("delete from tokens where client_id = ?", (client_id,))

    def revoke(self, token: str) -> None:
        rows = self._q("select pair_id from tokens where hash = ?", (_hash(token),))
        if rows:
            self._q("delete from tokens where pair_id = ?", (rows[0][0],))
