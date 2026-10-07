# Remote MCP-gateway (Docker + Tailscale Funnel)

Designdokument for å gjøre vigilo-connector tilgjengelig som en *remote*
MCP-server, slik at den kan brukes fra claude.ai (web og mobil), Claude Code,
Codex, Hermes, OpenClaw og andre MCP-klienter — ikke bare som lokal
stdio-server.

Status: implementert (`vigilo_connector.gateway`).

## Mål

- Én installasjon per foresatt, kjørt med `docker compose up -d` (eller to
  `docker run`), som gir en offentlig HTTPS-adresse med MCP over Streamable HTTP.
- Fungerer med claude.ai custom connectors uten ekstra oppsett i klienten:
  OAuth 2.1 med Dynamic Client Registration (DCR) og PKCE.
- Enkel nok for andre foreldre: ingen eget domene, ingen OAuth-app hos
  Google/GitHub, ingen terminal for Vigilo-innloggingen.
- Eksisterende `vigilo-login` og `vigilo-mcp` virker som før.

## Ikke-mål (første versjon)

- Andre eksponeringsmoduser enn Tailscale Funnel (Cloudflare Tunnel og «egen
  reverse proxy» kan legges til senere uten å endre gatewayen).
- Skrivende verktøy (f.eks. fraværsregistrering).
- Flere foresatte/brukere per instans, 2FA, innlogging via Google/GitHub.
- Endringer i oppførselen til eksisterende verktøy.

## Hvorfor dette oppsettet

- **claude.ai kobler til fra Anthropics servere**, ikke fra brukerens enhet.
  En adresse som bare finnes i et privat nettverk (inkl. et tailnet uten Funnel)
  er derfor ikke nåbar. Serveren må ha en offentlig HTTPS-adresse.
- **claude.ai krever OAuth** for custom connectors (header-basert auth er i
  begrenset beta). Klienter som Claude Code og Codex støtter også OAuth, så
  OAuth dekker alle.
- **Tailscale Funnel** gir offentlig HTTPS på `<host>.<tailnet>.ts.net` med
  gyldig sertifikat, gratis, uten eget domene. Det gjør oppsettet enklest for
  andre foreldre.
- **Cloudflare Access «Managed OAuth» er forkastet:** den svarer 401 uten
  `WWW-Authenticate`, og claude.ai web/mobil feiler da før innlogging
  (anthropics/claude-ai-mcp#410, lukket som «not planned»).
- **Anthropics «MCP tunnels»** krever Enterprise-plan og er derfor uaktuelt.
- **Eget passord i stedet for ekstern IdP:** MCP-SDK-en har en komplett
  `OAuthAuthorizationServerProvider` med DCR/PKCE. Vi implementerer
  lagring og en passordside; ingen trenger å registrere en OAuth-app noe sted.

## Arkitektur

```
Internett (claude.ai, mobil, Claude Code, Codex, Hermes, OpenClaw)
        │ HTTPS
        ▼
https://vigilo.<tailnet>.ts.net  (Tailscale Funnel, :443)
        │
┌───────┴──────────── container: vigilo-ts (tailscale/tailscale) ───────────┐
│  userspace-modus, TS_SERVE_CONFIG=serve.json (Funnel → 127.0.0.1:8000)     │
│  ┌──────────────── container: vigilo-gateway (deler nettverk) ──────────┐  │
│  │ uvicorn :8000 (1 worker)                                             │  │
│  │  /mcp                    MCP Streamable HTTP (krever Bearer)         │  │
│  │  /.well-known/...        OAuth-metadata (AS + protected resource)    │  │
│  │  /register /authorize /token /revoke   (fra MCP-SDK-en)              │  │
│  │  /login                  passordside i authorize-flyten              │  │
│  │  /setup                  Vigilo-oppsett og status                    │  │
│  │  /healthz                                                            │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────────┘
volumer: data (/data i gateway), ts-state, ts-socket (delt)
```

Verktøyene kjører **i samme prosess** som gatewayen: `server.py` får en
`register_tools(mcp)` som både `vigilo-mcp` (stdio) og gatewayen bruker. Én
prosess betyr én `TokenStore` og én lås rundt token-refresh — viktig fordi
Vigilos refresh-tokens roterer ved bruk.

## Komponenter

```
src/vigilo_connector/
  server.py            register_tools(mcp); vigilo-mcp uendret utad
  gateway/
    settings.py        miljøvariabler, validering, autogenererte hemmeligheter
    oauth_store.py     SQLite: klienter, ventende authorize, koder, tokens
    oauth_provider.py  OAuthAuthorizationServerProvider + verifisering av API_TOKEN
    web.py             /login, /setup (HTML-skjema, CSRF, økt-cookie, sperre)
    vigilo_setup.py    authorize-URL, tolking av innlimt input, tokens-import, status
    app.py             setter sammen MCPServer + ruter; entrypoint vigilo-gateway
```

| Modul | Avhenger av | Brukes av |
|---|---|---|
| `settings` | stdlib | alle |
| `oauth_store` | `sqlite3` | `oauth_provider`, `web` |
| `oauth_provider` | `oauth_store`, `mcp.server.auth` | `app` |
| `vigilo_setup` | `vigilo_connector.auth`/`login`/`client` | `web` |
| `web` | `oauth_store`, `vigilo_setup`, `settings` | `app` |
| `app` | alt over + `server.register_tools` | entrypoint |

`pyproject.toml`: ny entrypoint `vigilo-gateway = "vigilo_connector.gateway.app:main"`,
`mcp>=2.0` (koden bruker allerede `mcp.server.mcpserver`, som ikke finnes i 1.x),
og `pytest` i en `dev`-extra. Starlette og uvicorn følger med `mcp`.

## OAuth-flyt

1. Klienten kaller `/mcp` uten token → `401` med `WWW-Authenticate: Bearer
   resource_metadata=...` (SDK-en).
2. Klienten leser metadata og registrerer seg på `/register` (DCR). Klienten
   lagres i `oauth.db`.
3. Klienten sender nettleseren til `/authorize`. Provideren lagrer forespørselen
   (klient, redirect-URI, `state`, PKCE-challenge, scopes, resource) under en
   tilfeldig id med 10 min levetid, og redirecter til `/login?req=<id>`.
4. `/login` viser klientnavn og redirect-URI og ber om passordet.
5. Riktig passord → autorisasjonskode (engangs, 5 min) → redirect til klientens
   redirect-URI med `code` og `state`.
6. Klienten bytter koden på `/token` (SDK-en sjekker PKCE). Access-token: 1 time.
   Refresh-token: 90 dager, rotert ved hver bruk. Gjenbruk av et allerede brukt
   refresh-token tilbakekaller alle tokens for den klienten.

Tokens er 32 tilfeldige byte (`secrets.token_urlsafe`). Kun SHA-256-hasher
lagres. Ett scope: `vigilo`.

### Passord

- `ADMIN_PASSWORD` fra miljøet vinner; må være minst 12 tegn.
- Uten den genereres `secrets.token_urlsafe(48)` (64 tegn) ved første oppstart,
  lagres i `/data/admin_password` (chmod 600) og skrives til loggen **én gang**,
  bare når det genereres. Senere: `docker exec vigilo-gateway vigilo-gateway show-password`.
- Sammenligning med `hmac.compare_digest`. Etter 5 feil fra samme IP innen
  15 min sperres innlogging for den IP-en i 15 min. Klient-IP hentes fra
  headeren Tailscale Funnel setter (`X-Forwarded-For`; verifiseres under
  implementasjonen). Mangler den, faller sperren tilbake til en global teller.
- Samme passord beskytter `/setup`.
- Nytt passord påvirker bare nye innlogginger. Slett `oauth.db` for å logge ut
  alle klienter.

### Statisk token

Valgfri `API_TOKEN` (minst 32 tegn) for klienter uten OAuth-støtte:
`Authorization: Bearer <API_TOKEN>` godtas på `/mcp`. Ikke satt → avslått.
Genereres ikke automatisk.

## /setup

- Innlogging med passord gir en økt-cookie (`HttpOnly`, `Secure`,
  `SameSite=Lax`, 12 t). Signert med `/data/session_secret` (autogenerert).
  Alle skjemaer har CSRF-token. Ingen JavaScript.
- **Status:** om Vigilo-nøkler er satt (viser kun `client_id`), om tokens finnes
  og når de sist ble fornyet, antall OAuth-klienter, og en «Test tilkobling»-knapp
  som lister barna (navn, skole, gruppe).
- **Vigilo-nøkler:** skjema for `client_id`/`client_secret` hvis de ikke er satt
  i miljøet; lagres i `/data/vigilo/config.json` (600). Miljøet vinner.
- **Logg inn hos Vigilo:** knapp som lager authorize-URL (ny `state` i økten) med
  instruksjoner for Chrome og Firefox. Ett tekstfelt tar imot enten
  `app://…?code=`-URL, bar code, «Copy as cURL» (Chrome `-b`, Firefox
  `-H 'Cookie:'`) eller et rått 302-svar med `location:`-header. Gjenbruker
  `login.parse_code_input`, `login.cookies_from_curl` og `auth.code_from_cookies`.
  Ved suksess lagres tokens og tilkoblingen testes.
- **Importer `tokens.json`:** filopplasting; krever `access_token` og
  `refresh_token` før den lagres.
- Feil vises med forklaring på norsk (f.eks. `invalid_grant` → «koden er brukt
  eller utløpt — start en ny innlogging»).

## Konfigurasjon

| Variabel | Default | Merknad |
|---|---|---|
| `PUBLIC_URL` | hentes fra Tailscale | f.eks. `https://vigilo.example.ts.net` |
| `ADMIN_PASSWORD` | autogenerert | min. 12 tegn |
| `API_TOKEN` | av | min. 32 tegn |
| `VIGILO_CLIENT_ID` / `VIGILO_CLIENT_SECRET` | via `/setup` | miljøet vinner |
| `DATA_DIR` | `/data` | |
| `PORT` | `8000` | lytter på `127.0.0.1` |
| `TAILSCALE_SOCKET` | `/var/run/tailscale/tailscaled.sock` | for `PUBLIC_URL`-oppslag |
| `LOG_LEVEL` | `INFO` | |

Er `PUBLIC_URL` ikke satt, spør gatewayen Tailscales lokale API
(`/localapi/v0/status` over den delte socketen) om `Self.DNSName`, og venter
inntil 60 s på at Tailscale er klar. `PUBLIC_URL` brukes som issuer,
resource-URL og tillatt `Host` for SDK-ens DNS-rebinding-beskyttelse.

Datavolum:

```
/data/admin_password        600 (hvis autogenerert)
/data/session_secret        600
/data/oauth.db
/data/vigilo/config.json    600 (hvis satt via /setup; VIGILO_CONFIG_DIR=/data/vigilo)
/data/vigilo/tokens.json    600
```

## Docker

- `Dockerfile`: `python:3.13-slim`, `pip install .`, ikke-root (`uid 10001`),
  `HEALTHCHECK` mot `/healthz`, `CMD ["vigilo-gateway"]`. Kan kjøres med
  skrivebeskyttet rotfilsystem (skrivbart: `/data`, `/tmp`).
- `docker/compose.yml`: `vigilo-ts` (`tailscale/tailscale:stable`, `TS_AUTHKEY`,
  `TS_HOSTNAME=vigilo`, `TS_USERSPACE=true`, `TS_STATE_DIR`, `TS_SERVE_CONFIG`,
  `TS_SOCKET` på delt volum) og `vigilo-gateway` med
  `network_mode: service:vigilo-ts`.
- `docker/serve.json`:

  ```json
  {"TCP":{"443":{"HTTPS":true}},
   "Web":{"${TS_CERT_DOMAIN}:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8000"}}}},
   "AllowFunnel":{"${TS_CERT_DOMAIN}:443":true}}
  ```

- `.env.example`: bare `TS_AUTHKEY` er påkrevd.
- README viser også varianten med to `docker run` (`--network container:vigilo-ts`).
- `.github/workflows/docker.yml`: test, bygg multi-arch (amd64, arm64), røyktest,
  publiser til `ghcr.io/${{ github.repository_owner }}/vigilo-connector` med
  taggene `latest` (main), `sha-<kort>` og `vX.Y.Z` (git-tagger).

### Krav i tailnettet (én gang)

1. HTTPS-sertifikater slått på (DNS → HTTPS Certificates).
2. `funnel`-nodeattributt i policyen (standard for `autogroup:member` i nye
   tailnets; må ev. legges til for taggen).
3. Auth key — anbefalt: tagget `tag:vigilo`, ikke-ephemeral, forhåndsgodkjent.
   Anbefalt policy: ingen utgående tilgang fra `tag:vigilo`.

Bare port 443 på denne noden eksponeres via Funnel. Userspace-modus gir
gatewayen ingen rute inn i resten av tailnettet.

## Feilhåndtering

- Ugyldig konfigurasjon (for kort passord/token, ikke-skrivbart `/data`,
  `PUBLIC_URL` ikke funnet innen 60 s) → avslutt ved oppstart med én tydelig
  melding.
- Vigilo ikke satt opp / innlogging utløpt (`AuthError`) → verktøyet returnerer
  en feilmelding med lenke til `<PUBLIC_URL>/setup`, ikke HTTP 500. `/setup`
  viser «Innlogging utløpt».
- Vigilo 5xx/timeout → verktøyfeil med HTTP-status. Ingen nye retries utover
  dagens ene retry ved 401.
- Ingen hemmeligheter i logg (Authorization, cookies, passord, koder, tokens).
  `httpx`-loggeren settes til `WARNING` (URL-er inneholder barne-id-er).
- `/healthz` sjekker bare at prosessen svarer.
- uvicorn kjøres med én worker (låsen i `TokenStore` gjelder per prosess).

## Testing

Ingen kall mot ekte Vigilo i CI.

- `oauth_store`: hashing, utløp, engangskoder, refresh-rotasjon og
  tilbakekalling ved gjenbruk.
- `settings`: autogenerert passord og session-secret persisteres og gjenbrukes,
  miljøet vinner, minstelengder.
- `vigilo_setup`: tolking av `app://`-URL, bar code, Chrome-cURL, Firefox-cURL og
  rått 302-svar (syntetiske verdier); validering av `tokens.json`-import.
- Integrasjon (ASGI i prosess): `401` + `WWW-Authenticate` på `/mcp`; full flyt
  DCR → authorize → login → token → MCP `initialize`/`tools/list`/ett verktøykall
  mot mocket `VigiloClient`; `API_TOKEN` på/av; CSRF og sperre på `/login` og `/setup`.
- Regresjon: `vigilo-mcp` eksponerer samme verktøyliste som før refaktoren.
- CI: bygg imaget, start med `PUBLIC_URL` satt, sjekk `/healthz` og
  `/.well-known/oauth-authorization-server`.
- Manuell ende-til-ende: Funnel + claude.ai (web og mobil), Claude Code, Codex.

## Klientoppsett

- claude.ai: Customize → Connectors → Add custom connector → `https://<host>/mcp`,
  OAuth-feltene tomme. Følger med til desktop og mobil.
- Claude Code: `claude mcp add -s user --transport http vigilo https://<host>/mcp`, deretter `/mcp`.
- Codex: `codex mcp add vigilo --url https://<host>/mcp`, deretter `codex mcp login vigilo`.
- Klienter uten OAuth: send `Authorization: Bearer <API_TOKEN>`.

## Viktig: én instans per Vigilo-innlogging

Vigilos refresh-tokens roterer ved bruk. Kjører både en lokal `vigilo-mcp` og
gatewayen med hver sin kopi av `tokens.json`, vil den ene logge ut den andre.
Når gatewayen er i bruk, bør alle klienter gå via den.
