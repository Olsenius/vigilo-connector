"""Nettsidene i gatewayen: /login (del av OAuth-flyten), /setup og /healthz.

Rene HTML-skjemaer uten JavaScript. Alle skjemaer har CSRF-token (double-submit
mot en egen cookie), all dynamisk tekst escapes, og passordforsøk begrenses
per IP. Økten for /setup er en HMAC-signert cookie.
"""

import hashlib
import hmac
import html
import secrets
import time
from collections.abc import Callable
from datetime import datetime

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .oauth_provider import VigiloOAuthProvider
from .oauth_store import OAuthStore
from .vigilo_setup import SetupError

SESSION_COOKIE = "vg_session"
CSRF_COOKIE = "vg_csrf"
STATE_COOKIE = "vg_vstate"
SESSION_TTL = 12 * 3600
STATE_TTL = 15 * 60


class LoginLimiter:
    """Maks `max_failures` feil per nøkkel innen `window` sekunder, deretter sperre."""

    def __init__(self, max_failures: int = 5, window: float = 900, block: float = 900,
                 now: Callable[[], float] = time.time):
        self._max, self._window, self._block, self._now = max_failures, window, block, now
        self._failures: dict[str, list[float]] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, key: str) -> bool:
        until = self._blocked_until.get(key)
        if until is None:
            return False
        if self._now() >= until:
            del self._blocked_until[key]
            self._failures.pop(key, None)
            return False
        return True

    def fail(self, key: str) -> None:
        now = self._now()
        recent = [t for t in self._failures.get(key, []) if now - t < self._window] + [now]
        self._failures[key] = recent
        if len(recent) >= self._max:
            self._blocked_until[key] = now + self._block

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def _client_key(request: Request) -> str:
    # Tailscale Funnel/Serve setter X-Forwarded-For. Uten den deler alle én teller.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or "global"


class _Signer:
    def __init__(self, secret: bytes):
        self._secret = secret

    def _mac(self, value: str) -> str:
        return hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()

    def sign(self, value: str, ttl: float) -> str:
        payload = f"{int(time.time() + ttl)}.{value}"
        return f"{payload}.{self._mac(payload)}"

    def unsign(self, cookie: str | None) -> str | None:
        if not cookie or cookie.count(".") < 2:
            return None
        payload, mac = cookie.rsplit(".", 1)
        if not hmac.compare_digest(mac, self._mac(payload)):
            return None
        expires, value = payload.split(".", 1)
        if not expires.isdigit() or int(expires) < time.time():
            return None
        return value


def _set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, secure=True, samesite="lax", path="/")


_STYLE = """
body{font-family:system-ui,sans-serif;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#1b1b1b;background:#fafafa}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}
input,textarea,button{font:inherit}input[type=password],input[type=text],textarea{width:100%;box-sizing:border-box;padding:.4rem}
textarea{height:8rem;font-family:ui-monospace,monospace;font-size:.85rem}
button{margin-top:.5rem;padding:.4rem 1rem}
.err{background:#fde8e8;border:1px solid #e0a0a0;padding:.6rem}.ok{background:#e8f5e8;border:1px solid #a0d0a0;padding:.6rem}
code,.url{word-break:break-all;font-family:ui-monospace,monospace;font-size:.85rem}
table{border-collapse:collapse}td{padding:.2rem .8rem .2rem 0}
@media (prefers-color-scheme:dark){body{background:#161616;color:#e6e6e6}.err{background:#3a1d1d;border-color:#7a3a3a}.ok{background:#1d3a1d;border-color:#3a7a3a}}
"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (
        f"<!doctype html><html lang=no><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<meta name=referrer content=no-referrer>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    resp = HTMLResponse(doc, status_code=status)
    resp.headers["cache-control"] = "no-store"
    resp.headers["x-frame-options"] = "DENY"
    resp.headers["content-security-policy"] = "default-src 'none'; style-src 'unsafe-inline'"
    return resp


def _msg(kind: str, text: str) -> str:
    return f"<p class={kind}>{html.escape(text)}</p>"


def _children_table(children: list[dict]) -> str:
    rows = "".join(
        "<tr>" + "".join(
            f"<td>{html.escape(str(c.get(k) or ''))}</td>" for k in ("firstName", "lastName", "school", "group")
        ) + "</tr>"
        for c in children
    )
    return f"<table>{rows}</table>"


def build_routes(*, provider: VigiloOAuthProvider, store: OAuthStore, setup, password: str,
                 session_secret: bytes, limiter: LoginLimiter) -> list[Route]:
    signer = _Signer(session_secret)

    def password_ok(candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), password.encode())

    def csrf_token(request: Request) -> str:
        return request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(24)

    def with_csrf(resp: Response, token: str) -> Response:
        _set_cookie(resp, CSRF_COOKIE, token, SESSION_TTL)
        return resp

    async def form_with_csrf(request: Request):
        form = await request.form()
        cookie = request.cookies.get(CSRF_COOKIE, "")
        sent = str(form.get("csrf", ""))
        if not cookie or not hmac.compare_digest(cookie, sent):
            return None
        return form

    def logged_in(request: Request) -> bool:
        return signer.unsign(request.cookies.get(SESSION_COOKIE)) is not None

    # --- /healthz ----------------------------------------------------------------

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    # --- /login (OAuth) --------------------------------------------------------------

    def login_page(request: Request, req: str, info: dict, error: str = "", status: int = 200) -> Response:
        token = csrf_token(request)
        body = (
            (_msg("err", error) if error else "")
            + f"<p><b>{html.escape(info['client_name'])}</b> ber om tilgang til Vigilo-dataene dine.</p>"
            + f"<p>Svaret sendes til <span class=url>{html.escape(info['redirect_uri'])}</span>.</p>"
            + "<p>Kjenner du ikke igjen klienten eller adressen, ikke logg inn.</p>"
            + "<form method=post action=/login>"
            + f'<input type=hidden name="req" value="{html.escape(req)}">'
            + f'<input type=hidden name="csrf" value="{html.escape(token)}">'
            + '<label>Passord<input type="password" name="password" autofocus autocomplete=current-password></label>'
            + "<button>Gi tilgang</button></form>"
        )
        return with_csrf(_page("Logg inn – Vigilo MCP", body, status), token)

    async def login_get(request: Request) -> Response:
        req = request.query_params.get("req", "")
        info = provider.pending_info(req)
        if info is None:
            return _page("Ugyldig forespørsel", _msg("err", "Innloggingen er utløpt eller ukjent. Start tilkoblingen på nytt fra klienten."), 400)
        return login_page(request, req, info)

    async def login_post(request: Request) -> Response:
        key = _client_key(request)
        if limiter.blocked(key):
            return _page("For mange forsøk", _msg("err", "For mange feil passord. Vent et kvarter og prøv igjen."), 429)
        form = await form_with_csrf(request)
        if form is None:
            return _page("Ugyldig skjema", _msg("err", "Skjemaet er utløpt. Last siden på nytt."), 403)
        req = str(form.get("req", ""))
        info = provider.pending_info(req)
        if info is None:
            return _page("Ugyldig forespørsel", _msg("err", "Innloggingen er utløpt eller ukjent. Start tilkoblingen på nytt fra klienten."), 400)
        if not password_ok(str(form.get("password", ""))):
            limiter.fail(key)
            return login_page(request, req, info, "Feil passord.", 401)
        limiter.reset(key)
        return RedirectResponse(provider.complete_authorization(req), status_code=302)

    # --- /setup -------------------------------------------------------------------------

    def setup_login_page(request: Request, error: str = "", status: int = 200) -> Response:
        token = csrf_token(request)
        body = (
            (_msg("err", error) if error else "")
            + "<form method=post action=/setup/login>"
            + f'<input type=hidden name="csrf" value="{html.escape(token)}">'
            + '<label>Passord<input type="password" name="password" autofocus autocomplete=current-password></label>'
            + "<button>Logg inn</button></form>"
        )
        return with_csrf(_page("Oppsett – Vigilo MCP", body, status), token)

    def setup_page(request: Request, notice: str = "", authorize_url: str = "") -> Response:
        token = csrf_token(request)
        hidden = f'<input type=hidden name="csrf" value="{html.escape(token)}">'
        st = setup.status()
        obtained = (
            datetime.fromtimestamp(st["obtained_at"]).strftime("%Y-%m-%d %H:%M")
            if st.get("obtained_at") else "ukjent"
        )
        parts = [notice, "<h2>Status</h2><table>"]
        parts.append(f"<tr><td>Vigilo-nøkler</td><td>{'satt (' + html.escape(st['client_id']) + ')' if st['credentials'] else 'mangler'}</td></tr>")
        parts.append(f"<tr><td>Vigilo-innlogging</td><td>{'ja, sist fornyet ' + obtained if st['tokens'] else 'nei'}</td></tr>")
        parts.append(f"<tr><td>Tilkoblede klienter</td><td>{store.client_count()}</td></tr></table>")
        parts.append(f"<form method=post action=/setup/test>{hidden}<button>Test tilkobling</button></form>")

        if not st["credentials"]:
            parts.append(
                "<h2>Vigilo-nøkler</h2><p>Hentes fra Vigilos Android-app, se README.</p>"
                f"<form method=post action=/setup/credentials>{hidden}"
                '<label>client_id<input type="text" name="client_id" autocomplete=off></label>'
                '<label>client_secret<input type="password" name="client_secret" autocomplete=off></label>'
                "<button>Lagre</button></form>"
            )

        parts.append("<h2>Logg inn hos Vigilo</h2>")
        if authorize_url:
            parts.append(
                "<ol><li>Åpne utviklerverktøyene i nettleseren (Network-fanen) og slå på "
                "<i>Preserve log</i> / <i>Persist logs</i>.</li>"
                f"<li>Åpne <a class=url href='{html.escape(authorize_url)}' target=_blank rel=noopener>{html.escape(authorize_url)}</a> og logg inn med ID-porten.</li>"
                "<li>Nettleseren ender på en feilside for en <code>app://</code>-adresse — det er riktig.</li>"
                "<li>Kopier enten hele <code>app://…?code=…</code>-adressen, «Copy as cURL» for "
                "<code>authorize</code>-requesten, eller svaret med <code>location:</code>-headeren (Firefox), og lim inn under.</li></ol>"
            )
        else:
            parts.append(f"<form method=post action=/setup/start>{hidden}<button>Start innlogging</button></form>")
        parts.append(
            f"<form method=post action=/setup/finish>{hidden}"
            '<textarea name="raw" placeholder="app://…?code=… / curl … / location: …"></textarea>'
            "<button>Fullfør innlogging</button></form>"
        )
        parts.append(
            "<h2>Importer tokens.json</h2><p>Fra en tidligere <code>vigilo-login</code>. "
            "Bruk den deretter ikke andre steder — tokenet roterer.</p>"
            f"<form method=post action=/setup/import enctype=multipart/form-data>{hidden}"
            '<input type="file" name="tokens" accept=".json,application/json"><button>Importer</button></form>'
        )
        parts.append(f"<form method=post action=/setup/logout>{hidden}<button>Logg ut</button></form>")
        return with_csrf(_page("Oppsett – Vigilo MCP", "".join(parts)), token)

    async def setup_get(request: Request) -> Response:
        return setup_page(request) if logged_in(request) else setup_login_page(request)

    async def setup_login(request: Request) -> Response:
        key = _client_key(request)
        if limiter.blocked(key):
            return _page("For mange forsøk", _msg("err", "For mange feil passord. Vent et kvarter og prøv igjen."), 429)
        form = await form_with_csrf(request)
        if form is None:
            return _page("Ugyldig skjema", _msg("err", "Skjemaet er utløpt. Last siden på nytt."), 403)
        if not password_ok(str(form.get("password", ""))):
            limiter.fail(key)
            return setup_login_page(request, "Feil passord.", 401)
        limiter.reset(key)
        resp = RedirectResponse("/setup", status_code=303)
        _set_cookie(resp, SESSION_COOKIE, signer.sign(secrets.token_urlsafe(16), SESSION_TTL), SESSION_TTL)
        return resp

    def setup_action(handler):
        """Felles for POST /setup/*: CSRF, krever økt, og viser SetupError på siden."""

        async def endpoint(request: Request) -> Response:
            form = await form_with_csrf(request)
            if form is None:
                return _page("Ugyldig skjema", _msg("err", "Skjemaet er utløpt. Last siden på nytt."), 403)
            if not logged_in(request):
                return RedirectResponse("/setup", status_code=303)
            try:
                return await handler(request, form)
            except SetupError as e:
                return setup_page(request, _msg("err", str(e)))

        return endpoint

    async def do_credentials(request, form):
        setup.save_credentials(str(form.get("client_id", "")), str(form.get("client_secret", "")))
        return setup_page(request, _msg("ok", "Vigilo-nøklene er lagret."))

    async def do_start(request, form):
        url, state = setup.start_login()
        resp = setup_page(request, authorize_url=url)
        _set_cookie(resp, STATE_COOKIE, signer.sign(state, STATE_TTL), STATE_TTL)
        return resp

    async def do_finish(request, form):
        state = signer.unsign(request.cookies.get(STATE_COOKIE))
        children = setup.finish_login(str(form.get("raw", "")), state)
        resp = setup_page(request, _msg("ok", f"Innlogget hos Vigilo. Fant {len(children)} barn:") + _children_table(children))
        resp.delete_cookie(STATE_COOKIE, path="/")
        return resp

    async def do_import(request, form):
        upload = form.get("tokens")
        data = await upload.read() if hasattr(upload, "read") else b""
        setup.import_tokens(data)
        return setup_page(request, _msg("ok", "tokens.json er importert. Trykk «Test tilkobling»."))

    async def do_test(request, form):
        children = setup.test_connection()
        return setup_page(request, _msg("ok", f"Tilkoblingen virker. Fant {len(children)} barn:") + _children_table(children))

    async def do_logout(request, form):
        resp = RedirectResponse("/setup", status_code=303)
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/login", login_get, methods=["GET"]),
        Route("/login", login_post, methods=["POST"]),
        Route("/setup", setup_get, methods=["GET"]),
        Route("/setup/login", setup_login, methods=["POST"]),
        Route("/setup/credentials", setup_action(do_credentials), methods=["POST"]),
        Route("/setup/start", setup_action(do_start), methods=["POST"]),
        Route("/setup/finish", setup_action(do_finish), methods=["POST"]),
        Route("/setup/import", setup_action(do_import), methods=["POST"]),
        Route("/setup/test", setup_action(do_test), methods=["POST"]),
        Route("/setup/logout", setup_action(do_logout), methods=["POST"]),
    ]
