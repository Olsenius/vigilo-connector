# HTTP deployment and migration

Vigilo MCP v2 runs behind mcp-gateway. The gateway owns public OAuth, admin sessions and tunnels. This backend owns Vigilo credentials and rotating tokens.

## Local configuration

Install with `pip install -e '.[dev]'`. Set `MCP_AUTH_TOKEN` to a long random internal secret and `DATA_DIR` to a private writable directory, then run `vigilo-mcp`. Default port: 8080. `VIGILO_CONFIG_DIR` defaults to `DATA_DIR/vigilo`. Run one worker only. `MCP_ALLOWED_HOSTS` is an optional comma-separated list for the SDK Host validation, defaulting to the internal Vigilo service names and localhost.

The internal HTTP API requires bearer authentication except `GET /healthz`:

- `/mcp`: stateless Streamable HTTP, existing tool catalogue.
- `GET/POST /config`: gateway settings contract. Fields: `VIGILO_CLIENT_ID`, `VIGILO_CLIENT_SECRET`. Empty secret preserves the current value; `clear` removes an unlocked field. Environment settings are locked. A stored credential change clears tokens and pending login attempts; log in again afterwards.
- `GET /healthz?check=1`: read-only Vigilo connectivity check, 200 or 503. Plain liveness never contacts Vigilo.
- `POST /setup/start`, `/setup/complete`, `/setup/cancel`: see the gateway setup contract.

Use the [gateway's Compose example](https://github.com/Olsenius/mcp-gateway#quick-start-docker-compose). It runs the published `ghcr.io/olsenius/vigilo-connector` image on the internal network without publishing backend ports. To run this checkout instead, use [mcp-workspace](https://github.com/Olsenius/mcp-workspace)'s `task dev:up`. Open the gateway admin page, select Vigilo, save credentials, then press **Logg inn**. Follow the link, log in at Vigilo and paste the full return URL. If the browser reports an unknown app protocol, inspect the address or the authorize response's Location header in its Network tab. Use a browser/device without the Vigilo app consuming the code. The URL must include the matching state. Start again after an expired, invalid or already consumed attempt.

V2 initially accepts full state-bearing return URLs only. Bare codes, cookie/cURL imports and token-file uploads are not exposed in the web UI: there is no implicit fallback that bypasses the state check. This is a deliberate compatibility reduction from the old setup page. Automatic callback is not implemented because support for a controlled HTTPS redirect URI has not been established.

## Production migration (not performed by local development)

1. Build and test both checkouts; record the previous images and private config/volume layout. Verify gateway admin login independently before changing Vigilo.
2. Stop the old Vigilo token holder. Do not start a development client from a copied token. Make a private backup of the stopped service's current data.
3. Reuse its `vigilo/config.json` and current `vigilo/tokens.json` in the new DATA_DIR volume, preserving private permissions and container UID 10001. If directory layouts differ, explicitly map/copy the existing Vigilo directory; do not blindly create a new empty volume. The token file must be the latest authoritative file.
4. Start the backend with the internal token matching `UPSTREAM_TOKEN_VIGILO`; start/update mcp-gateway with `vigilo=http://vigilo:8080` in UPSTREAMS. Remove the old Vigilo tunnel from exposure.
5. Verify liveness, unauthorized MCP rejection, settings, and a read-only Test through gateway admin. Verify one read-only MCP call through `https://mcp.example.test/vigilo/mcp`.
6. Reconnect MCP clients to that gateway URL and approve them there. Old Vigilo OAuth client grants are not migrated. Existing Vigilo API login can be preserved through the token files.

For rollback stop the new backend first, then give the old backend the **current** token file before restarting its previous image/config. A backup token from before refresh may already be invalid. Never run both token holders at once. Preserve gateway and Vigilo data while switching images.

## Verification limitations

Use synthetic data and mock transports for local tests. No live writes are required. Full gateway tests use localhost listeners; some restricted sandboxes deny these. AnyIO thread-bridge and TestClient integration must be verified in a normal development/CI environment if the sandbox blocks them. Do not treat isolated route tests with a mocked thread bridge as end-to-end proof.

## Container and browser regression check

Build both review images from their respective checkouts:

```sh
sudo -n docker build -t vigilo-mcp:v2-local-review .
# In the mcp-gateway checkout:
sudo -n docker build -t mcp-gateway:v2-local-review .
```

From the Vigilo checkout, run `.venv/bin/python tests/container_smoke.py`.
It creates isolated containers with synthetic credentials, mocked Vigilo responses
and a loopback gateway port, then removes them. It never imports live account data.

Optionally set `SMOKE_BROWSER_SCRIPT` to the absolute path of
`tests/browser_smoke.cjs` to include Chromium form submission and desktop/mobile
layout checks. The Node script requires Playwright with a working Chromium
installation. Set `PLAYWRIGHT_MODULE` to your installed Playwright module path
if it is not in Node's module lookup path. Screenshots are written to
`/tmp/vigilo-v2-browser-{login,connected,mobile}.png` using synthetic data only.

## OpenTelemetry

Tracing is opt-in. Set `OTEL_EXPORTER_OTLP_ENDPOINT` (base URL, e.g.
`http://jaeger:4318`) or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` (full URL).
The exporter uses OTLP HTTP/protobuf and standard SDK headers, TLS, timeout,
resource and sampler environment settings. `OTEL_SERVICE_NAME` defaults to
`vigilo-mcp`. `OTEL_SDK_DISABLED=true`, `OTEL_TRACES_EXPORTER=none`, or no
endpoint disables export. Pending batches flush on graceful process shutdown.

Incoming W3C traceparent/tracestate continue the gateway trace. Server request,
MCP tool, and outbound Vigilo HTTP spans share that trace across worker threads.
Plain liveness is excluded. Recorded metadata includes fixed route templates,
HTTP methods/statuses, tool names and API categories. Query values, raw paths
containing identifiers, bodies, credentials, baggage and exception messages are
excluded. No trace headers or baggage are added to third-party HTTP requests.
For streamed outbound responses the client span covers receipt of response
headers; the surrounding tool span covers subsequent body processing.
