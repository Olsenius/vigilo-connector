"""Remote MCP-gateway: `vigilo-gateway`.

Setter sammen MCP-serveren (Streamable HTTP på /mcp) med innebygd OAuth,
passordside og /setup, og kjører den med uvicorn. Se docs/remote-gateway.md.

Modulen importerer bevisst ikke resten av vigilo_connector på toppnivå:
`config` leser VIGILO_CONFIG_DIR ved import, og `main()` må få satt den først.
"""

import functools
import logging
import os
import sys
import time
from pathlib import Path

import httpx

from .settings import ConfigError, Settings, load_settings

log = logging.getLogger("vigilo_connector.gateway")


def resolve_public_url(settings: Settings, *, transport: httpx.BaseTransport | None = None,
                       timeout: float = 60, interval: float = 1) -> str:
    """PUBLIC_URL fra miljøet, ellers maskinens navn fra Tailscales lokale API."""
    if settings.public_url:
        return settings.public_url
    transport = transport or httpx.HTTPTransport(uds=str(settings.tailscale_socket))
    deadline = time.monotonic() + timeout
    last = "ingen svar"
    with httpx.Client(transport=transport, base_url="http://local-tailscaled.sock", timeout=5) as c:
        while True:
            try:
                status = c.get("/localapi/v0/status").json()
                name = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
                if status.get("BackendState") == "Running" and name:
                    return f"https://{name}"
                last = f"Tailscale-status {status.get('BackendState')!r}"
            except (httpx.HTTPError, ValueError) as e:
                last = str(e)
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"Fant ikke PUBLIC_URL: Tailscale ble ikke klar på {settings.tailscale_socket} "
                    f"innen {timeout:.0f} s ({last}). Sett PUBLIC_URL, eller sjekk Tailscale-containeren."
                )
            time.sleep(interval)


def _friendly_errors(fn, setup_url: str):
    """Gjør Vigilo-feil om til verktøyfeil agenten kan forklare brukeren."""
    from mcp.server.mcpserver.exceptions import ToolError

    from ..auth import AuthError

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except AuthError as e:
            raise ToolError(
                f"Vigilo er ikke satt opp, eller innloggingen er utløpt ({e}). "
                f"Be brukeren logge inn på nytt på {setup_url}"
            ) from None
        except httpx.HTTPStatusError as e:
            raise ToolError(f"Vigilo svarte HTTP {e.response.status_code} på {e.request.url.path}.") from None
        except httpx.TransportError as e:
            raise ToolError(f"Fikk ikke kontakt med Vigilo: {e}") from None

    return wrapper


def create_app(settings: Settings, public_url: str, vigilo_client=None):
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    from .. import config, server
    from ..client import VigiloClient
    from .oauth_provider import SCOPE, VigiloOAuthProvider
    from .oauth_store import OAuthStore
    from .vigilo_setup import VigiloSetup
    from .web import LoginLimiter, build_routes

    if Path(config.CONFIG_DIR) != settings.vigilo_dir:
        raise ConfigError(
            f"VIGILO_CONFIG_DIR ({config.CONFIG_DIR}) må være {settings.vigilo_dir} "
            "(DATA_DIR/vigilo). Ikke sett den til noe annet."
        )
    settings.vigilo_dir.mkdir(parents=True, exist_ok=True)

    store = OAuthStore(settings.oauth_db)
    provider = VigiloOAuthProvider(store, public_url, settings.api_token)
    setup = VigiloSetup(settings.vigilo_dir)
    # Verktøyene og /setup deler TokenStore — og dermed låsen rundt refresh.
    server.use_client(vigilo_client or VigiloClient(setup.tokens))

    mcp = MCPServer(
        "vigilo",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=f"{public_url}/mcp",
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[SCOPE],
            # API_TOKEN er ikke bundet til en resource; OAuth-tokens utstedes kun for /mcp.
            validate_token_resource=False,
        ),
    )
    for fn in server.TOOLS:
        mcp.add_tool(_friendly_errors(fn, f"{public_url}/setup"))

    for route in build_routes(
        provider=provider,
        store=store,
        setup=setup,
        password=settings.admin_password,
        session_secret=settings.session_secret,
        limiter=LoginLimiter(),
    ):
        mcp.custom_route(route.path, methods=sorted(route.methods - {"HEAD"}))(route.endpoint)

    host = public_url.removeprefix("https://")
    return mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            # Tailscale serve kan sende enten offentlig vertsnavn eller 127.0.0.1 som Host.
            allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
            allowed_origins=[public_url],
        ),
    )


def _announce_password(settings: Settings) -> None:
    line = "=" * 72
    print(
        f"\n{line}\nNytt admin-passord generert (vises bare denne ene gangen):\n\n"
        f"    {settings.admin_password}\n\n"
        f"Lagret i {settings.password_file}. Vis igjen med:\n"
        f"    docker exec vigilo-gateway vigilo-gateway show-password\n{line}\n",
        file=sys.stderr,
        flush=True,
    )


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    try:
        settings = load_settings()
    except ConfigError as e:
        sys.exit(f"Konfigurasjonsfeil: {e}")

    if argv[:1] == ["show-password"]:
        print(settings.admin_password)
        return
    if argv:
        sys.exit("Bruk: vigilo-gateway [show-password]")

    os.environ.setdefault("VIGILO_CONFIG_DIR", str(settings.vigilo_dir))
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # URL-ene mot Vigilo inneholder barne-id-er; ikke logg dem.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if settings.password_generated:
        _announce_password(settings)

    try:
        public_url = resolve_public_url(settings)
        log.info("Offentlig adresse: %s (MCP: %s/mcp, oppsett: %s/setup)", public_url, public_url, public_url)
        app = create_app(settings, public_url)
    except ConfigError as e:
        sys.exit(f"Konfigurasjonsfeil: {e}")

    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port, workers=1,
                log_level=settings.log_level.lower(), proxy_headers=False)


if __name__ == "__main__":
    main()
