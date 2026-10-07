"""Vigilo-innlogging og status for /setup-siden.

Tynt lag over `auth` og `login`: samme flyt som `vigilo-login`, men styrt fra
et nettskjema. Alt brukeren limer inn (redirect-URL, bar kode, «Copy as cURL»
eller et rått 302-svar) går gjennom `classify_input`. Feil kastes som
`SetupError` med en norsk forklaring som kan vises direkte på siden.
"""

import json
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import auth, config, login
from ..auth import AuthError, TokenStore
from ..client import VigiloClient
from ..config import write_json_atomic


class SetupError(RuntimeError):
    pass


_LOCATION = re.compile(r"^\s*location:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_BARE_CODE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")


def _code_from_url(url: str, expected_state: str | None) -> str:
    params = parse_qs(urlsplit(url).query or url.split("?", 1)[-1])
    state = params.get("state", [None])[0]
    if expected_state and state and state != expected_state:
        raise SetupError(
            "state i adressen matcher ikke denne innloggingen. Start en ny innlogging fra /setup."
        )
    code = params.get("code", [None])[0]
    if not code:
        raise SetupError("Fant ingen code i det du limte inn.")
    return code


def classify_input(raw: str, expected_state: str | None) -> tuple[str, str]:
    """Returner ("code", kode) eller ("cookies", cookie-header)."""
    text = (raw or "").strip()
    if not text:
        raise SetupError("Lim inn adressen, koden eller cURL-en fra nettleseren.")

    # Rått HTTP-svar (f.eks. kopiert fra Firefox' Network-fane): les Location.
    # Innloggingen er da startet i nettleseren, så state sjekkes ikke.
    m = _LOCATION.search(text)
    if m:
        return "code", _code_from_url(m.group(1), None)

    if text.startswith("curl "):
        try:
            return "cookies", login.cookies_from_curl(text)
        except SystemExit as e:  # login.py avslutter med sys.exit(melding)
            raise SetupError(str(e.code)) from None

    if "code=" in text:
        return "code", _code_from_url(text, expected_state)

    if _BARE_CODE.match(text):
        return "code", text

    raise SetupError(
        "Forsto ikke det du limte inn. Lim inn app://…?code=…-adressen, bare koden, "
        "«Copy as cURL» for authorize-requesten, eller 302-svaret med location-headeren."
    )


def _explain(e: AuthError) -> SetupError:
    msg = str(e)
    if "invalid_grant" in msg:
        return SetupError("Koden er allerede brukt eller utløpt. Start en ny innlogging og prøv igjen raskt.")
    if "client-credentials" in msg or "client_id" in msg:
        return SetupError("Vigilo-nøklene (client_id/client_secret) mangler. Legg dem inn først.")
    return SetupError(f"Vigilo avviste innloggingen: {msg}")


def _list_children(store: TokenStore) -> list[dict]:
    client = VigiloClient(store)
    try:
        return client.children()
    finally:
        client.close()


class VigiloSetup:
    def __init__(self, vigilo_dir: Path, children_fn: Callable[[TokenStore], list[dict]] = _list_children):
        self._dir = vigilo_dir
        self._children_fn = children_fn
        self.tokens = TokenStore(vigilo_dir / "tokens.json")

    def status(self) -> dict:
        client_id, client_secret = config.load_client_credentials()
        has_tokens = self.tokens.path.exists()
        obtained_at = None
        if has_tokens:
            try:
                obtained_at = json.loads(self.tokens.path.read_text()).get("obtained_at")
            except ValueError:
                has_tokens = False
        return {
            "credentials": bool(client_id and client_secret),
            "client_id": client_id or None,
            "tokens": has_tokens,
            "obtained_at": obtained_at,
        }

    def save_credentials(self, client_id: str, client_secret: str) -> None:
        client_id, client_secret = client_id.strip(), client_secret.strip()
        if not client_id or not client_secret:
            raise SetupError("Både client_id og client_secret må fylles ut.")
        write_json_atomic(self._dir / "config.json", {"client_id": client_id, "client_secret": client_secret})

    def import_tokens(self, data: bytes) -> None:
        try:
            tokens = json.loads(data)
        except ValueError:
            raise SetupError("Fila er ikke gyldig JSON. Velg tokens.json fra vigilo-login.") from None
        if not isinstance(tokens, dict) or not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise SetupError("Fila mangler access_token eller refresh_token.")
        # Behold obtained_at fra fila, så utløpstiden er riktig.
        write_json_atomic(self.tokens.path, tokens)

    def start_login(self) -> tuple[str, str]:
        state = secrets.token_urlsafe(16)
        try:
            return auth.authorize_url(state), state
        except AuthError:
            raise SetupError("Vigilo-nøklene (client_id/client_secret) mangler. Legg dem inn først.") from None

    def finish_login(self, raw: str, expected_state: str | None) -> list[dict]:
        kind, value = classify_input(raw, expected_state)
        try:
            code = auth.code_from_cookies(value) if kind == "cookies" else value
            tokens = auth.exchange_code(code)
        except AuthError as e:
            raise _explain(e) from None
        self.tokens.save(tokens)
        return self.test_connection()

    def test_connection(self) -> list[dict]:
        if not self.tokens.path.exists():
            raise SetupError("Ikke logget inn hos Vigilo ennå.")
        try:
            return self._children_fn(self.tokens)
        except AuthError:
            raise SetupError("Innloggingen hos Vigilo er utløpt. Logg inn på nytt.") from None
        except Exception as e:  # noqa: BLE001 — vis feilen på siden i stedet for 500
            raise SetupError(f"Vigilo svarte med feil: {e}") from None
