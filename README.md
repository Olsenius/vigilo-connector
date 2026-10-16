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
| `web_list_children` | Én rad per barn/enhet, med stabile ID-er, enhetstype og alle grupper (også SFO) |
| `list_children` | Barna via app-gatewayen (kun `childId`) |
| `list_message_threads` / `get_message_thread` | Én side tråder/meldinger med `pageId` og tilstandsflagg (app-gateway) |
| `search_message_thread` | Søk i alle meldingssider for én ordinær tråd |
| `message_participants` | Deltakere og trådstarter for valgt barn og ordinær tråd |
| `read_message_attachments` | Last ned og les vedlegg i en meldingstråd (PDF/docx→tekst) |
| `read_post_attachments` | Last ned og les vedlegg i et oppslag/«Siste nytt» (PDF/docx→tekst) — her ligger ofte ukeplanen |
| `news_feed` | «Siste nytt» / oppslag for et barn |
| `absences` | Fravær for et barn |
| `message_contacts` | Kontaktliste for barn/skole med mottakerfeltene `type` og `externalId` |
| `send_message` | Send ny melding med tittel, tekst og eksplisitt valgte mottakere |
| `reply_message` | Send svar i en eksisterende ordinær meldingstråd |
| `register_student_absence` | Registrer skolefravær med periode, tittel, begrunnelse og valgte mottakere |
| `absence_codes` | Aktive fraværskoder for valgt barnehage |
| `register_childcare_absence` | Registrer barnehagefravær med periode, aktiv kode og valgfri merknad |
| `list_pickup_notes` | Hentemeldinger (SFO) for et barn i én kalendermåned, med barnets SFO-enheter |
| `pickup_note_options` | Aktiverte kategorier, meldingstyper og registreringsfrister for en SFO-enhet |
| `create_pickup_note` | Opprett hentemelding (SFO) for én periode, valgfritt ukentlig gjentatt |
| `update_pickup_note` | Endre hele hentemeldingen/serien (SFO); utelatte felt beholdes |
| `set_pickup_note_end_date` | Ny siste dag for en hentemelding/serie (SFO) |
| `delete_pickup_note` | Slett hele hentemeldingen/serien (SFO) |
| `consent_forms` | Samtykkeskjemaer (krever `organizationalUnitId`) |
| `timetable` | Timeplan/ukesplan for en ISO-uke |
| `scheduling_events` | Prøver/aktiviteter i timeplanen for en uke |
| `api_get` / `web_api_get` | Rått GET mot app- hhv. web-API-et — for kartlegging |

`childId` og `organizationalUnitId` får du fra `web_list_children`; begge er
UUID-er. `week` er ISO-format `YYYY-WW` (default inneværende uke).

Meldingssending krever at brukeren har bestilt konkret innhold og mottakere.
`send_message` tar `child_id`, `organizational_unit_id`, `title`, `body` og
`recipients` fra `message_contacts`. Mottakere sjekkes mot barnets kontaktliste,
og minst én lærer/administrasjon (`employee`) kreves. `reply_message` tar
`thread_uid` fra en ordinær meldingstråd og `body`; fraværstråder støttes ikke.
Tittel og meldingstekst kan ikke være blanke. Gruppene i kontaktlisten viser
individuelle ansattmottakere som kan velges; gruppe-ID sendes ikke som mottaker.
På wire legger connectoren automatisk til barnet først som
`{"type":"client","externalId":child_id}`, slik appens use-case og type-mapper
gjør. Dette er barnets trådkontekst, ikke en bruker valgt mottakertype.
Alle mottakere dedupliseres på kontakt-ID, som i appens mapper.
Ingen sikker maksimumslengde for teksten er funnet i den lokale APK-analysen;
vi finner derfor ikke på en lokal grense. Vedlegg er fortsatt ikke støttet.
Før svar hentes ordinære tråddetaljer. `threadUid` må stemme og
`isReplyDisabled` må være eksplisitt `false`; manglende status avviser sending.
Slettede tråder avvises også som en forsiktig connectorregel: appen viser
slettestatus, men separat svarblokk på dette flagget er ikke påvist.
Kontrollen kan ikke utelukke at servertilstand endres etter oppslaget.
Ved timeout eller HTTP 5xx kan meldingen være sendt:
les meldingstrådene før et nytt forsøk. Bare eksplisitt HTTP 401 gir ett nytt
forsøk med fornyet token. Begge verktøy er kontrakttestet med mock-transport.
Svar i en eksisterende tråd er også live-verifisert gjennom gatewayen, med
tilbake-lesing som bekreftet den sendte teksten. Oppretting av en ny meldingstråd
er foreløpig kun kontrakttestet. Barn/eininger, listepaging, søk i en eldre melding i én tråd og deltakeroppslag
er manuelt verifisert via remote MCP. Nye svarforkontroller er ikke live-testet.

`list_message_threads` returnerer nå et objekt med `messageThreads` og rå
`pageId` i stedet for en løs liste. `page_size` og `page_id` kan brukes på både
trådlisten og `get_message_thread`. Standard sidestørrelse er 20 for trådlisten
og 50 for meldingsdetaljer. Trådlisten tillater 1–20 per side; større verdier
avvises før noe kall mot Vigilo. Detaljsider hentes i appen med returnert
`pageId + 1`, inntil tom side. Listepaging er påvist i appens `fetchPaged`:
første side uten `pageId`, deretter `pageId + 1` mens antall tråder er lik
sidestørrelsen. Denne progresjonen er nå statisk verifisert i REA/smali og
mocktestet og manuelt verifisert for trådlisten.
Listen viser `lastMessage.snippet` med høyst 200 tegn, kompakt `threadStarter`
fra trådens sender og `lastMessage.sender` bare når siste melding har egen sender
(`id`, `name`, `communicatorType`), og eksisterende trådfelt. Full tekst hentes
med `get_message_thread`. `hasMore` betyr at siden er full, som i appens
videreføringsregel; neste side kan fortsatt være tom. `nextPageId` oppgis
når siden er full og returnert `pageId` er gyldig. SFO er inkludert i listen som standard.
`message_participants(thread_uid, child_id)` beholder rå deltakerfelt.
`get_message_thread` kan få `child_id`. Barnets identitet hentes fra den
autoriserte barnelisten, og trådens tilknytning bekreftes i barnets filtrerte
trådliste før detaljene hentes. Dette koster ett barnelistekall og ett eller
flere trådlistekall (50 per side, maksimalt 100 sider), i tillegg til detaljkallet.
Ugyldig paging, manglende tilknytning og sidegrense gir feil. Uten `child_id`
utelates detaljresponsens tvetydige `child`; gruppemedlemmer utledes ikke.
Ingen av disse lesekallene markerer tråden som lest.

Barnelisten, trådlisten og deltakeroppslag gjenbrukes i prosessminnet i opptil
60 sekunder (høyst 512 svar og 8 MiB), adskilt per tilgangstoken, sti og
query. Like samtidige kall deler én forespørsel; feil lagres ikke, og et kall
som venter på et annet kall som feiler, henter selv. `list_children`,
`list_message_threads`, `message_participants` og barneverifiseringen i
`get_message_thread` tar `force_refresh=true`, som henter bare de berørte svarene
på nytt. Meldingsdetaljer og `search_message_thread` hentes alltid ferske:
appen henter detaljer på nytt ved hver åpning, og sidene forskyves når nye
meldinger kommer. Finner ikke barneverifiseringen tråden i bufrede lister,
hentes listene ferske før feil. Alle skriveforsøk tømmer bufferen før og
etter kallet, også ved ukjent utfall. Utløpte svar fjernes av en tidtaker,
ikke bare ved neste lesing. Endringer gjort andre steder, f.eks. i appen,
kan være usynlige i listene i opptil 60 sekunder.

`search_message_thread(thread_uid, query)` søker bokstavelig uten skille på
store/små bokstaver i meldingstekst og vedleggsnavn i én valgt tråd.
Finn først tråd-ID og forhåndsvisning med `list_message_threads`, og hent
valgte tråder med `get_message_thread`. Søket leser eldre sider til tom side
uten å markere lest; innholdet i vedleggsfilene blir ikke søkt.
`complete=true` kommer bare etter full traversering. Feil, ugyldig eller
gjentatt side og lokale grenser gir synlig MCP-feil med framdrift og «ikkje
komplett». `max_pages` er som standard 100 og `timeout_s` 45 sekunder
(større enn 0 og høyst 120). Søke-GET bruker connect/pool/write høyst
5 sekunder og read høyst 15, begrenset av gjenstående søkebudsjett.
Auth og venting på den delte tokenlåsen får høyst 15 sekunder innenfor
søkebudsjettet. Mottatte roterte tokens lagres før en utløpt frist rapporteres.
Synkron transport og tokenfornyelse kan overskride tidsbudsjettet mens et
aktivt kall avsluttes. Feil viser `phase=auth`, `auth_lock` eller `scan`,
faktisk `elapsed_s` og konfigurert `timeout_s`. Nye meldinger under søket
kan endre sidene; resultatet er ingen transaksjonell øyeblikkskopi.

HTTP-metadata kan slåast på med `VIGILO_HTTP_LOG=1` i gatewaymiljøet.
Standard er av. Loggen går som éi JSON-linje per request, response eller feil
til stderr (tilgjengeleg gjennom containerloggen), òg for stdio utan ekstra
loggoppsett. Linjene inneheld tilfeldig lokal `callId`, metode, trygg
rutemal, kategori (`app`, `web`, `oauth`, `attachment`), HTTP-status og
`elapsedMs` ved respons/feil; feil får berre ein trygg klasse, aldri feilteksten.
Response-tida blir målt når headers er mottekne; sein transportfeil under
bodylesing får ei eiga feillinje med same ID.
Alle app-/OAuth-kall via `android_client`, web-API-kall, filestorage,
eksterne meldingsvedlegg og browser-authorize frå cookies er omfatta.
Web-oppslagsvedlegg bruker webklienten og blir logga som `web`.
Interne Tailscale-kall og innkomande MCP-/OAuth-klienttrafikk er ikkje
Vigilo-kall og blir ikkje logga av denne funksjonen.
Headerverdiar, cookies, tokens, queryverdiar, person-/objekt-ID-ar,
signerte URL-ar, requestbody og responsebody blir aldri tekne med.
Ukjende ruter får berre `/{redacted}`; vedleggsruter får ein generisk mal.
Når metadatalogging er på, blir rå `httpx`-/`httpcore`-loggar undertrykte,
fordi dei kan skrive heile URL-ar ved INFO/DEBUG i andre vertsapplikasjonar.
Logging les ikkje responsebody og endrar ikkje headers, retries eller
redirectpolicy. Feil i loggutgangen stoppar ikkje API-kallet.

Alle svar fra MCP-verktøyene filtrerer profilbildefeltene `profilePicture`
og `profilePictureDto` rekursivt og erstatter dem med `hasProfilePicture`.
Dette omfatter råverktøyene `api_get` og `web_api_get`. Signerte profilbilde-URL-er
returneres ikke. Vedleggsmetadata utelater URL-felt og beholder ID, navn og MIME.
Signerte Azure Blob-URL-er fjernes også fra tekst, råsvar og verktøyfeil.
Vedleggslesing bruker URL-ene internt og returnerer bare filtrerte resultater.

Felles skrivetransport støtter POST, PUT og DELETE med eller uten JSON-body
og med headers per operasjon. Nye operasjoner får ingen automatisk 401-retry;
de fire eksisterende skriveoperasjonene beholder ett forsøk etter eksplisitt
401 og tokenfornyelse. Ingen skrivekall følger redirects eller spilles av på
nytt etter transportfeil, 3xx eller 5xx; utfallet regnes da som ukjent.
Feilmeldinger viser HTTP-status og et
formatbegrenset `X-Correlation-Id` når tilgjengelig, aldri responsinnhold.
`create_pickup_note`, `update_pickup_note`, `set_pickup_note_end_date` og
`delete_pickup_note` har samme 401-forsøk.

Hentemeldinger kan leses, opprettes, endres og slettes.
Som i appen vises de bare for barn med en SFO-enhet (`type: afterSchool` i
app-gatewayens barneliste); uten SFO-enhet gjøres ikke noe kall mot
`/api/check-in-notes`. `list_pickup_notes(child_id, month)` henter én
kalendermåned (`YYYY-MM`, standard inneværende måned i Europe/Oslo) med
`childPersonId`, `fromDate` lik første dag og eksklusiv `toDate` lik første
dag i neste måned, slik appens månedsvisning gjør. `pickup_note_options` krever
en av barnets SFO-enheter. Den henter konfigurasjonen med headeren
`X-Organizational-Unit-Id`, alle meldingstyper uten kategori (som appen) og
registreringsfrister per aktivert kategori. Typer markeres med
`categoryEnabled`; appens eventuelle videre filtrering er ikke kartlagt.
Fristoppslag per kategori er utledet fra appens konfigurasjonsmodell, siden
repository-metoden ikke er dekompilert.

Vigilo sender `fromDate`/`toDate` som lokal tid uten sone, `timeOfDay` som
`HH:MM:SS` og `dateOccurrences` som UTC-tidspunkt for midnatt i Europe/Oslo.
Med `day` (`YYYY-MM-DD`) beholdes bare meldinger med en forekomst den dagen
regnet i Oslo-tid. Flere meldinger kan gjelde samme dag; som i appens kalender
markeres den sist endrede (`lastUpdatedTime`) med `active: true` og
`activeNoteId`, og ved likt tidspunkt vinner den første. Hver melding får
`typeAbbreviation`, appens kode: forbokstavene i de tre første ordene i
typenavnet, eller de tre første tegnene når navnet er ett ord. Appens kalender
viser bare hverdager. Lesingen er live-verifisert mot appens visning.

`create_pickup_note` sender `POST /api/check-in-notes` med
`X-Organizational-Unit-Id` og samme body som appen: `childPersonIds`, typens
`category`, `typeId`, `fromDate` og eksklusiv `toDate` som `YYYY-MM-DD`,
`timeOfDay` som `HH:MM:SS` og `noteText` (tom når den mangler). Uten
`repeat_days` er mønsteret `daily` med intervall 1 og uten `daysOfWeek` (Gson
utelater null). Med `repeat_days` (mandag–fredag, som appen tilbyr) er det
`weekly` med intervall 1, eller 2 med `every_other_week`. Før sending avvises
lokalt: enhet som ikke er barnets SFO, type som er inaktiv, slettet eller har
deaktivert kategori, manglende tekst når typen har `useComment`, tid utenfor
appens timehjul (kvarter fra 06:00 til 18:00) og startdato før
`earliestAllowedDate` (UTC til Oslo-tid, helg flyttet til mandag som i appen).
Vigilos valideringskoder (`type`, `validationErrors[*].errorCode`) vises uten
servertekst. Hva klokkeslettet betyr avhenger av typen; for «Bestemmer selv
sluttid» er det tidligste sluttid. Enkeltdager er live-verifisert 16.10.2026
(HTTP 204, tilbake-lesing med `list_pickup_notes`), og ukentlig gjentakelse
likeså (`weekly`, intervall 1, `daysOfWeek` lest tilbake som sendt).

Endring følger appen: det finnes ingen endring av én enkelt dag, bare av hele
meldingen/serien. Verktøyene tar `note_id` og en `month` (`YYYY-MM`) der
meldingen vises i `list_pickup_notes`, fordi appen ikke har oppslag av én
melding. Meldingens egen SFO-enhet brukes i `X-Organizational-Unit-Id`, og
Vigilos flagg avgjør hva som er lov, som i appens liste: `canUpdate` gir
`update_pickup_note`, bare `canUpdateToDate` gir `set_pickup_note_end_date`,
og `canDelete` gir `delete_pickup_note`.

`update_pickup_note` sender `PUT /api/check-in-notes/{id}` med samme body som
oppretting, men uten `childPersonIds` (appens
`UpdateCheckoutNoteRequestApiModel`). Utelatte felt hentes fra meldingen;
`repeat_days=[]` slår av gjentakelse. Fristen sjekkes bare når startdatoen
endres. En ukentlig serie uten `daysOfWeek` avvises lokalt: Vigilo lagrer den,
men den gjelder da bare startdagen (observert live 16.10.2026 etter endring i
appen). Live-verifisert 16.10.2026: PUT med `daysOfWeek` ga HTTP 204, og
tilbake-lesing viste dagene og alle forekomstene.

`set_pickup_note_end_date` sender `PUT /api/check-in-notes/{id}/to-date` med
`{"toDate": "YYYY-MM-DD"}` lik dagen etter siste dag. Live 16.10.2026 lagret
Vigilo verdien som eksklusiv slutt: `toDate` 30.12. ga siste forekomst 29.12.
Appen sender valgt dag uten +1 og forkorter dermed serien én dag mer enn valgt;
det kopieres ikke. Siste dag kan ikke være før startdatoen, og ikke før
`earliestAllowedDate` (eller i dag når fristen mangler), som i appens
sluttdatoskjerm. `delete_pickup_note` sender `DELETE /api/check-in-notes/{id}`
uten body; live-verifisert 16.10.2026 (HTTP 204, serien borte ved tilbake-lesing,
andre meldinger urørt). Kontroller alltid resultatet med `list_pickup_notes`.

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

## Android-transport

App-API-et etterligner transporten observert i Android APK 3.3.0-4:
`appVersion: Android 3.3.0-4`, `User-Agent: okhttp/4.10.0`, gzip og
`Content-Type: application/json; charset=UTF-8` på JSON-kall. HTTP/2 tilbys med
HTTP/1.1-fallback. JSON bruker kompakt UTF-8 og Gsons standard escaping.
`userId` i query er en lokal Android-sesjonsmarkør og fjernes før sending;
ID-er i URL-stier beholdes. Ingen cookies persisteres i app-/OAuth-klienten.
App-/OAuth-klientene har 100 s connect/read-timeout og 10 s write/pool-timeout.
Token fornyes når mindre enn 15 sekunder gjenstår. OAuth-tokenkall bruker Basic
auth og form-urlencoded uten appVersion. Browser-login, web-API-et og eksterne
vedlegg bruker sine separate oppsett. `filestorage`-stier hopper over Bearer,
appVersion og token-refresh, slik appens interceptor gjør.

Meldinger trenger ingen ekstra `X-*`-header i den undersøkte kontrakten.
Enkelte hente-/leveringsbeskjeder bruker `X-Organizational-Unit-Id`, og tenant-logo
bruker `X-Tenant-Id`; disse settes ikke globalt. Skrivekall følger ikke redirects
eller automatiske transport-retries. HTTP 401 tillater ett eksplisitt nytt forsøk
med fornyet token. TLS-fingeravtrykk og Androids cache/retry-implementasjon er
forskjellige; dette er kompatibilitet med observerte kontrakter, ikke garantert
identisk nettverkstrafikk.

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
