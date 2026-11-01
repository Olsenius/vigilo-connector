"""Internal HTTP service. Public authentication belongs to mcp-gateway."""
from contextlib import asynccontextmanager
import hmac
import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from starlette.responses import JSONResponse, PlainTextResponse

from . import auth, config, server
from .client import VigiloClient
from .telemetry import TraceHTTP, configure


class Service:
    def __init__(self):
        self.tokens = auth.TokenStore(config.TOKEN_FILE)
        self.client = VigiloClient(self.tokens)
        self.lock = threading.RLock()
        self.attempts = {}
        self.verified = False
        self.generation = 0

    def document(self):
        with self.lock:
            return self._document()

    def _document(self):
        credentials = config.load_client_credentials()
        fields = []
        for key, label, value in zip(('VIGILO_CLIENT_ID', 'VIGILO_CLIENT_SECRET'), ('Client ID', 'Client secret'), credentials):
            fields.append(dict(key=key, label=label, type='secret', required=True,
                               value='', set=bool(value), locked=key in os.environ))
        status = "connected" if self.verified else ("tokens_saved" if self.tokens.path.exists() else "login_required")
        labels = {"connected": "Tilkopla", "tokens_saved": "Innlogging lagra – trykk Test for å kontrollere", "login_required": "Innlogging krevst"}
        return dict(name='vigilo-mcp', version='0.2.0', configured=all(credentials), fields=fields,
                    setup=dict(status=status, status_label=labels[status]))

    def configure(self, body):
        keys = {'VIGILO_CLIENT_ID': 'client_id', 'VIGILO_CLIENT_SECRET': 'client_secret'}
        values, clear = body.get('values', {}), body.get('clear', [])
        if not isinstance(values, dict) or not isinstance(clear, list):
            raise ValueError('Invalid settings')
        if any(k not in keys or k in os.environ for k in list(values) + clear):
            raise ValueError('Unknown or locked setting')
        if any(not isinstance(v, str) for v in values.values()):
            raise ValueError('Settings must be strings')
        with self.lock, self.tokens._lock:
            saved = json.loads(config.CONFIG_FILE.read_text()) if config.CONFIG_FILE.exists() else {}
            updated = dict(saved)
            for k, v in values.items():
                if v.strip():
                    updated[keys[k]] = v.strip()
            for k in clear:
                updated.pop(keys[k], None)
            if updated != saved:
                config.write_json_atomic(config.CONFIG_FILE, updated)
                self.tokens.path.unlink(missing_ok=True)
                self.attempts.clear()
                self.verified = False
                self.generation += 1
                self.client._read_cache.clear()
        return self.document()

    def start(self):
        with self.lock:
            now = time.time()
            self.attempts = {k: v for k, v in self.attempts.items() if v[1] > now}
            if len(self.attempts) >= 20:
                raise ValueError('Too many pending login attempts')
            attempt, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            url = auth.authorize_url(state)
            self.attempts[attempt] = (state, now + 600)
            return dict(attempt_id=attempt, authorization_url=url, expires_at=now + 600,
                        instructions='Logg inn i ei ny fane. Kopier heile app://-returadressa frå nettlesaren og lim henne inn her. Ved ukjend protokoll: sjå adressa eller Location i Network-fana. Bruk ei maskin utan Vigilo-appen slik at appen ikkje bruker koden.')

    def complete(self, body):
        with self.lock:
            attempt = self.attempts.pop(body.get('attempt_id', ''), None)
            if not attempt or attempt[1] <= time.time():
                raise ValueError('Login attempt expired or already used; start again')
            raw = body.get('input', '')
            if not isinstance(raw, str):
                raise ValueError('Paste the full return URL')
            query = parse_qs(urlsplit(raw.strip()).query)
            if query.get('state') != [attempt[0]] or len(query.get('code', [])) != 1:
                raise ValueError('Return URL must contain the matching state and one code; start again')
            with self.tokens._lock:
                self.tokens.save(auth.exchange_code(query['code'][0]))
                self.generation += 1
                self.verified = False
                self.client._read_cache.clear()
        return self.check()

    def cancel(self, body):
        with self.lock:
            self.attempts.pop(body.get('attempt_id', ''), None)
        return dict(ok=True)

    def check(self):
        with self.lock:
            generation = self.generation
        try:
            self.client.children()
            ok, detail = True, 'Tilkopla'
        except Exception:
            ok, detail = False, 'Kunne ikkje verifisere tilkoplinga. Kontroller innstillingane eller logg inn på nytt.'
        with self.lock:
            if generation != self.generation:
                ok, detail = False, "Innstillingane eller innlogginga vart endra. Prøv Test på nytt."
            else:
                self.verified = ok
        return dict(ok=ok, status='ok' if ok else 'error', tokens_saved=self.tokens.path.exists(),
                    checks=[dict(name='Vigilo', ok=ok, detail=detail)])


def create_app(token, service=None):
    if not token:
        raise ValueError('MCP_AUTH_TOKEN is required')
    service = service or Service()
    server.use_client(service.client)
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings
    @asynccontextmanager
    async def lifespan(_):
        try:
            yield {}
        finally:
            service.client.close()

    mcp = MCPServer('vigilo', lifespan=lifespan)
    server.register_tools(mcp)

    async def health(request):
        if request.query_params.get('check') != '1':
            return PlainTextResponse('ok')
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(service.check)
        return JSONResponse(result, status_code=200 if result['ok'] else 503, headers={'Cache-Control': 'no-store'})

    async def api(request):
        from starlette.concurrency import run_in_threadpool
        try:
            body = {} if request.method == 'GET' else await request.json()
            if not isinstance(body, dict):
                raise ValueError('Expected an object')
            path = request.url.path
            if path == '/config':
                if request.method == 'GET':
                    result = await run_in_threadpool(service.document)
                else:
                    result = await run_in_threadpool(service.configure, body)
            elif path == '/setup/start':
                result = await run_in_threadpool(service.start)
            elif path == '/setup/complete':
                result = await run_in_threadpool(service.complete, body)
            else:
                result = await run_in_threadpool(service.cancel, body)
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, auth.AuthError, httpx.HTTPError):
            return JSONResponse({'error': 'Handlinga feila. Kontroller innstillingane og start innlogginga på nytt.'}, status_code=400, headers={'Cache-Control': 'no-store'})

    mcp.custom_route('/healthz', methods=['GET'])(health)
    mcp.custom_route('/config', methods=['GET', 'POST'])(api)
    for path in ('start', 'complete', 'cancel'):
        mcp.custom_route('/setup/' + path, methods=['POST'])(api)
    hosts = os.environ.get('MCP_ALLOWED_HOSTS', 'vigilo,vigilo:*,vigilo-mcp,vigilo-mcp:*,localhost,localhost:*,127.0.0.1,127.0.0.1:*').split(',')
    app = mcp.streamable_http_app(stateless_http=True, transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=[v for v in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if v]))

    traced_app = TraceHTTP(app)

    class Protected:
        inner = app
        async def __call__(self, scope, receive, send):
            if scope['type'] != 'http':
                return await app(scope, receive, send)
            headers = dict(scope.get('headers', []))
            public = scope['path'] == '/healthz' and not scope.get('query_string')
            supplied = headers.get(b'authorization', b'')
            if not public and not hmac.compare_digest(supplied, ('Bearer ' + token).encode('utf-8')):
                return await JSONResponse({'error': 'Unauthorized'}, status_code=401)(scope, receive, send)
            # Bound all bodies before JSON parsing, including chunked requests.
            chunks, size = [], 0
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                size += len(message.get('body', b''))
                if size > 4 * 1024 * 1024:
                    return await JSONResponse({'error': 'Body too large'}, status_code=413)(scope, receive, send)
                chunks.append(message.get('body', b''))
                if not message.get('more_body'):
                    break
            delivered = False
            async def replay():
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            await traced_app(scope, replay, send)
    return Protected()


def main():
    os.environ.setdefault('VIGILO_CONFIG_DIR', os.path.join(os.environ.get('DATA_DIR', '/data'), 'vigilo'))
    # config constants are loaded at import; require an explicit consistent directory.
    if str(config.CONFIG_DIR) != os.environ['VIGILO_CONFIG_DIR']:
        raise ValueError('Set VIGILO_CONFIG_DIR before starting the service')
    import logging
    logging.getLogger('httpx').setLevel(logging.WARNING)
    import uvicorn
    shutdown = configure()
    try:
        uvicorn.run(create_app(os.environ.get('MCP_AUTH_TOKEN', '')), host='0.0.0.0', port=int(os.environ.get('PORT', '8080')), workers=1)
    finally:
        shutdown()
