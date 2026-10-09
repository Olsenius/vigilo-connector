# vigilo-connector

Personlig MCP-connector mot [Vigilo](https://vigilo.no) sitt (uoffisielle)
foreldre-API, slik at en agent kan lese beskjeder, timeplan, fravær, samtykker,
nyheter m.m. og aksjonere på dem. Auth-flyten er inspirert av kartleggingen i
[brujoand/vigilo2smtp](https://github.com/brujoand/vigilo2smtp).

**Kun til personlig bruk** med egen foreldretilgang. API-et er reverse-engineert
og kan endres av Vigilo uten varsel — respektér Vigilos vilkår, og ikke hent mer
data enn du selv har tilgang til. Tokens og data holdes lokalt
(`~/.config/vigilo-connector/`, chmod 600) og legges aldri inn i repoet.

## To API-er, ett token

Connectoren snakker med to endepunkter, og **samme OAuth-token dekker begge**:

- **App-gateway** (`api-gw-parent-app.prod.vigilo-oas.no`) — beskjeder/tråder.
- **Web-foreldreportal** (`web-parent.prod.vigilo-oas.no`) — timeplan, fravær,
  samtykker, nyheter, vurdering. Det Vigilo-appen ikke eksponerer.

Auth er en konfidensiell OAuth2-client (Basic-auth med client_id/secret, **uten
PKCE**). Innloggingen federerer via ID-porten. Refresh-tokenet varer 30–90 dager;
da kjører du `vigilo-login` på nytt.

## Oppsett

### 1. Client-credentials fra Android-appen

OAuth-clienten er Vigilos egen Android-app. Hent `client_id` og `client_secret`
fra APK-en (`parent.vigilo.no.parentapplication`):

1. Last ned APK-en, f.eks. med [`apkeep`](https://github.com/EFForg/apkeep):
   `apkeep -a parent.vigilo.no.parentapplication .`
2. Dekompiler med [`jadx`](https://github.com/skylot/jadx) og les
   `parent/vigilo/no/parentapplication/core/api/ApiConfig.java` — enum-verdiene
   for `PROD` inneholder `baseUrl`, `client_id`, `client_secret` og redirect-URI.
3. Legg nøklene i `~/.config/vigilo-connector/config.json`:

```json
{ "client_id": "...", "client_secret": "..." }
```

(Alternativt miljøvariablene `VIGILO_CLIENT_ID` / `VIGILO_CLIENT_SECRET`.)

### 2. Installer og logg inn

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/vigilo-login
```

`vigilo-login` skriver ut en innloggings-URL. Åpne den i en nettleser og logg
inn med ID-porten. Nettleseren ender til slutt på en
`app://ch-parent-android.vigilo.no?code=...`-adresse — **det er suksess.** Lim
hele adressen (eller bare `code`-verdien) inn i terminalen.

Viktige detaljer:

- Er du allerede innlogget hos Vigilo, hopper URL-en rett gjennom (SSO) og
  redirecten skjer momentant. Chrome/Safari viser da bare en tom feilside for
  `app://`-adressen og kan tømme adresselinjen. Åpne **DevTools → Network** og
  huk av **Preserve log** *før* du starter — den feilede `app://`-requesten blir
  liggende der med hele `code=...`.
- Logg inn fra en maskin **uten** Vigilo-appen installert, ellers kan OS-et gi
  redirecten til appen, som bruker opp engangskoden.
- Koder er engangs og kortlevde. Feiler utvekslingen: start en ny innlogging.

#### Headless innlogging (SSH / server uten skjerm)

På en maskin uten nettleser kan du ikke fange `app://`-redirecten lokalt. Bruk
`--from-curl` i stedet:

```bash
.venv/bin/vigilo-login --from-curl
```

Den skriver ut en authorize-URL. Gjør så dette i en desktop-nettleser (på en
maskin uten Vigilo-appen), der du er innlogget hos Vigilo:

1. Åpne **DevTools → Network**, naviger til den utskrevne URL-en.
2. Høyreklikk `authorize`-requesten → **Copy → Copy as cURL**.
3. Lim hele cURL-en inn i terminalen og avslutt med en linje `END` (eller Ctrl-D).

Alternativt kan cURL-en leses fra fil eller pipe (mer robust for store blokker):

```bash
.venv/bin/vigilo-login --from-curl curl.txt      # fra fil
pbpaste | ssh mini '~/Code/vigilo-connector/.venv/bin/vigilo-login --from-curl'   # pipe over SSH
```

Verktøyet henter sesjonscookiene ut av cURL-en, gjør authorize-kallet selv og
fanger koden fra redirecten — ingen manuell jakt på `app://`-adressen. Nettleser
og server trenger ikke være samme maskin.

(Alternativt: kjør innloggingen på en maskin med nettleser og kopier
`~/.config/vigilo-connector/tokens.json` over til serveren.)

### 3. Registrer MCP-serveren i Claude Code

```bash
claude mcp add vigilo -- /path/to/vigilo-connector/.venv/bin/vigilo-mcp
```

(Bytt `/path/to/vigilo-connector` med din egen sti til repoet.)

#### Alternativ: JSON-konfig (import / manuell registrering)

Klienter som importerer MCP-servere fra JSON kan bruke denne blokken:

```json
{
  "mcpServers": {
    "vigilo": {
      "command": "/path/to/vigilo-connector/.venv/bin/vigilo-mcp"
    }
  }
}
```

Skal du legge den til manuelt i en klient (MCP → sett opp manuelt), bruk
stdio-transport med samme kommando:

```json
{
  "transport": "stdio",
  "command": "/path/to/vigilo-connector/.venv/bin/vigilo-mcp"
}
```

## Remote MCP (Docker + Tailscale Funnel)

Vil du bruke connectoren fra claude.ai (web og mobil), eller dele én
installasjon mellom flere klienter (Claude Code, Codex, Hermes, OpenClaw …),
kan den kjøres som en *remote* MCP-server: `vigilo-gateway` i Docker, med en
Tailscale-container som gir en offentlig HTTPS-adresse via
[Funnel](https://tailscale.com/kb/1223/funnel). Ingen eget domene, ingen
åpne porter inn. Design og begrunnelser: [`docs/remote-gateway.md`](docs/remote-gateway.md).

Gatewayen har innebygd OAuth 2.1 (Dynamic Client Registration + PKCE): når en
klient kobler til, åpnes en innloggingsside der du skriver gatewayens
passord. Vigilo-innloggingen gjøres på `/setup` i nettleseren.

### Krav i tailnettet (én gang)

1. Slå på **HTTPS Certificates** (admin-konsollen → DNS).
2. Policyen må gi `funnel`-attributtet til noden. Standardpolicyen gir det bare
   til `autogroup:member` — **en tagget node (auth key med tag) får det ikke**,
   og da publiseres adressen aldri i offentlig DNS selv om
   `tailscale funnel status` sier «Funnel on». Bruker du en tag, legg til:

   ```json
   "tagOwners": { "tag:vigilo": ["autogroup:admin"] },
   "nodeAttrs": [{ "target": ["tag:vigilo"], "attr": ["funnel"] }]
   ```

   Noden trenger ingen tilgang til resten av tailnettet — ikke gi `tag:vigilo`
   noen `grants`/`acls`.
3. Lag en auth key (Settings → Keys): ikke-ephemeral, gjerne tagget
   `tag:vigilo` og forhåndsgodkjent.

### Start

```bash
git clone https://github.com/olsenius/vigilo-connector && cd vigilo-connector
cp .env.example .env          # sett TS_AUTHKEY
docker compose -f docker/compose.yml --env-file .env up -d
docker logs vigilo-gateway    # viser adressen og det genererte passordet
```

Passordet vises bare første gang. Senere:
`docker exec vigilo-gateway vigilo-gateway show-password`.

Gatewayen deler nettverk med `vigilo-ts`. Starter du `vigilo-ts` på nytt, må
gatewayen lages på nytt etterpå:
`docker compose -f docker/compose.yml --env-file .env up -d --force-recreate vigilo-gateway`.
Etter at Funnel er slått på (eller noden har fått `funnel`-attributtet) kan det
ta noen minutter før adressen finnes i offentlig DNS.

Uten compose, med to `docker run`:

```bash
docker volume create vigilo-data; docker volume create vigilo-ts-state; docker volume create vigilo-ts-socket
docker run -d --name vigilo-ts --hostname vigilo --restart unless-stopped \
  -e TS_AUTHKEY=tskey-auth-... -e TS_HOSTNAME=vigilo -e TS_USERSPACE=true \
  -e TS_STATE_DIR=/var/lib/tailscale -e TS_SOCKET=/var/run/tailscale/tailscaled.sock \
  -e TS_SERVE_CONFIG=/config/serve.json \
  -v vigilo-ts-state:/var/lib/tailscale -v vigilo-ts-socket:/var/run/tailscale \
  -v "$PWD/docker/serve.json:/config/serve.json:ro" \
  tailscale/tailscale:stable
docker run -d --name vigilo-gateway --network container:vigilo-ts --restart unless-stopped \
  --read-only --tmpfs /tmp \
  -v vigilo-data:/data -v vigilo-ts-socket:/var/run/tailscale \
  ghcr.io/olsenius/vigilo-connector:latest
```

### Bygg og publisering

GitHub Actions kjører tester og bygger images på native AMD64- og ARM64-runnere.
Begge images røyktestes før publisering til GHCR og samles i ett
multiarkitektur-manifest. `latest` peker på siste vellykkede bygg fra `main`;
hver commit får også en `sha-<kort SHA>`-tagg.

Python-baseimaget hentes fra det offentlige Docker Official Images-speilet
på ECR. Byggingen bruker runnerens innebygde Docker-builder og trenger
verken QEMU, nedlasting av et separat BuildKit-image eller Docker Hub-innlogging.

Bygg lokalt med:

```bash
docker build -t vigilo-gateway:local .
```

### Logg inn hos Vigilo

Åpne `https://vigilo.<tailnet>.ts.net/setup`, logg inn med passordet og:

1. Legg inn Vigilo-nøklene (se [steg 1](#1-client-credentials-fra-android-appen)),
   eller sett `VIGILO_CLIENT_ID`/`VIGILO_CLIENT_SECRET` i `.env`.
2. Trykk **Start innlogging**, følg instruksjonene og lim inn det nettleseren
   gir deg: `app://…?code=…`-adressen, «Copy as cURL» for
   `authorize`-requesten, eller svaret med `location:`-headeren.
3. Har du allerede en `tokens.json` fra `vigilo-login`, kan du importere den i
   stedet.

Samme sted fornyer du innloggingen når Vigilos refresh-token utløper (30–90 dager).

### Koble til klienter

MCP-adressen er `https://vigilo.<tailnet>.ts.net/mcp`.

- **claude.ai** (følger med til desktop og mobil): Customize → Connectors →
  Add custom connector → lim inn adressen. Velg «Register automatically»
  dersom du blir spurt om OAuth-klient.
- **Claude Code**: `claude mcp add -s user --transport http vigilo https://vigilo.<tailnet>.ts.net/mcp`,
  deretter `/mcp` for å logge inn.
- **Codex**: `codex mcp add vigilo --url https://vigilo.<tailnet>.ts.net/mcp`,
  deretter `codex mcp login vigilo`.
- **Klienter uten OAuth**: sett `API_TOKEN` (minst 32 tegn, f.eks.
  `openssl rand -hex 32`) og send `Authorization: Bearer <API_TOKEN>`.

**Bruk bare én instans per Vigilo-innlogging.** Refresh-tokenet roterer ved
bruk; kjører du både lokal `vigilo-mcp` og gatewayen med hver sin kopi av
`tokens.json`, logger de hverandre ut.

### Konfigurasjon

| Variabel | Standard | |
|---|---|---|
| `TS_AUTHKEY` | — | påkrevd første gang |
| `TS_HOSTNAME` | `vigilo` | første del av adressen |
| `ADMIN_PASSWORD` | generert | minst 12 tegn |
| `API_TOKEN` | av | minst 32 tegn |
| `VIGILO_CLIENT_ID` / `VIGILO_CLIENT_SECRET` | via `/setup` | |
| `PUBLIC_URL` | fra Tailscale | sett ved annen reverse proxy |
| `LOG_LEVEL` | `INFO` | |

Alt gatewayen lagrer ligger i volumet `/data` (passord, OAuth-klienter og
-tokens som hasher, Vigilo-nøkler og -tokens, chmod 600). For å logge ut alle
klienter: stopp gatewayen og slett `/data/oauth.db`.

## Verktøy

| Verktøy | Gjør |
|---|---|
| `web_list_children` | Barna dine med `childId` **og** `organizationalUnitId` (skole/klasse) |
| `list_children` | Barna via app-gatewayen (kun `childId`) |
| `list_message_threads` / `get_message_thread` | Beskjedtråder og meldinger (app-gateway) |
| `read_message_attachments` | Last ned og les vedlegg i en meldingstråd (PDF/docx→tekst) |
| `read_post_attachments` | Last ned og les vedlegg i et oppslag/«Siste nytt» (PDF/docx→tekst) — her ligger ofte ukeplanen |
| `news_feed` | «Siste nytt» / oppslag for et barn |
| `absences` | Fravær for et barn |
| `message_contacts` | Kontaktliste for barn/skole med mottakerfeltene `type` og `externalId` |
| `register_student_absence` | Registrer skolefravær med periode, tittel, begrunnelse og valgte mottakere |
| `absence_codes` | Aktive fraværskoder for valgt barnehage |
| `register_childcare_absence` | Registrer barnehagefravær med periode, aktiv kode og valgfri merknad |
| `consent_forms` | Samtykkeskjemaer (krever `organizationalUnitId`) |
| `timetable` | Timeplan/ukesplan for en ISO-uke |
| `scheduling_events` | Prøver/aktiviteter i timeplanen for en uke |
| `api_get` / `web_api_get` | Rått GET mot app- hhv. web-API-et — for kartlegging |

`childId` og `organizationalUnitId` får du fra `web_list_children`; begge er
UUID-er. `week` er ISO-format `YYYY-WW` (default inneværende uke).

Skolefravær tar datoer som `YYYY-MM-DD` (begge dager inkludert).
Ved sending konverteres siste fraværsdag til eksklusiv sluttdato dagen etter,
slik appen gjør. Dette ble live-verifisert ved registrering 25.12.2026 (HTTP 201)
og tilbake-lesing fra fraværsoversikten.
Hent først `message_contacts` og velg individuelle `recipient`-objekter derfra. Bruk
kontaktens `id`, ikke `employeeId`; lærere/administrasjon sendes som `employee`,
foresatte som `legalGuardian`. Skoleverktøyet støtter foreløpig ikke vedlegg eller SFO.
Tittel, begrunnelse og minst én mottaker kreves lokalt; alle
backend-valideringsregler er ennå ikke verifisert.

Barnehagefravær bruker `register_childcare_absence`, med samme inkluderte
ISO-datoperiode. Hent først `absence_codes(organizational_unit_id)` og velg
kode-ID som svarer til brukerens bestilling, f.eks. Fri. Koden sjekkes mot
barnehagens aktive koder før sending; slettede koder og koder fra andre enheter
avvises. Merknad er valgfri, og ingen tittel eller mottakere sendes. Kravene
til enhet, periode og kode samt sluttdato dagen etter er kontrollert i appens
`RegisterChildcareAbsenceUseCase`. Flyten er også live-verifisert med fraværskoden Fri over to dager
(HTTP 201 og tom respons).

Registrering krever at brukeren har bestilt den konkrete handlingen. Ved
transportfeil er resultatet ukjent: sjekk fraværsoversikten før et nytt forsøk.
Ingen automatisk retry ved timeout, andre transportfeil eller HTTP 5xx; bare et eksplisitt
401-svar gir ett nytt forsøk med fornyet token. Suksess kan ha tom respons.
Kontraktene bygger på Android APK 3.3.0-4, mock-tester og en autorisert
produksjonsregistrering. Begge verktøy registreres lokalt og i gatewayen.

## Kartlagte web-endepunkter

Base `https://web-parent.prod.vigilo-oas.no`, alle GET, Bearer-auth:

- `/api/children/my` — barn (childId + `organizationalUnits`)
- `/api/news-feed?childIds=&fromDate=&toDate=`
- `/api/message-threads?childIds=&fromDate=&toDate=`
- `/api/absences?childIds=`
- `/api/consent-forms?childId=&organizationalUnitId=`
- `/api/students/{childId}/lessons?organizationalUnitId=&week=YYYY-WW`
- `/api/scheduling-events/{childId}/student?organizationalUnitId=&week=YYYY-WW`
- `/api/school-years?organizationalUnitId=`

## Videre arbeid

Vurderings-/karakter-endepunktet laster først etter valg av skoleår/termin i
Vurdering-fanen og er ikke kartlagt ennå — sniff det med `web_api_get`. Nye
endepunkter legges inn i `client.py` og eksponeres i `server.py`.
