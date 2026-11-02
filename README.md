# vigilo-mcp

HTTP-only MCP server for the Vigilo parent API, running behind [mcp-gateway](https://github.com/Olsenius/mcp-gateway).

Read messages, news, attachments, timetables, registrations, meetings, consents,
pickup notes and absences. Existing tools also support the corresponding parent
write actions. The Python module remains `vigilo_connector`; the executable and
package are named `vigilo-mcp`.

## Run and configure

See [HTTP deployment, web login and migration](docs/remote-gateway.md).
Install with `pip install -e '.[dev]'`, set `MCP_AUTH_TOKEN` and `DATA_DIR`, then
run `vigilo-mcp`. Default port: 8080. Run one worker. No stdio or login CLI is
needed. Configure credentials and log in through the gateway admin page.

For Docker, use the [gateway's Compose example](https://github.com/Olsenius/mcp-gateway#quick-start-docker-compose), which runs the gateway, this server
and the other MCP servers, or the internal backend example in `docker/compose.yml`.
Backend ports should remain on the internal network. The gateway provides public
OAuth and the tunnel. To develop the server together with the gateway, use
[mcp-workspace](https://github.com/Olsenius/mcp-workspace).

Environment variables `VIGILO_CLIENT_ID` and `VIGILO_CLIENT_SECRET` override
stored credentials. Secrets and rotating tokens stay in the backend's data
volume; never commit these files. See the migration guide before reusing an
existing account: only one active process may hold its refresh token.

## Tools and development

The tool catalogue is defined in `src/vigilo_connector/server.py` and discoverable
through MCP `tools/list`. Existing argument/output semantics, privacy filtering,
read-cache behavior and write safeguards are preserved by the v2 migration.
Read responses can be cached briefly where documented by the individual tools.

Run `pytest -q` after installing development dependencies. Tests use synthetic
data and mocked API transports. Some HTTP integration tests need a normal local
network/thread environment; restricted sandbox limitations must be reported
separately from passing offline checks. Build the container with
`docker build -t vigilo-mcp:local .`.
