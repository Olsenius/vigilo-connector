import json
import httpx
PUBLIC = "http://localhost"
API_TOKEN = "t" * 40
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

def _sse_json(resp: httpx.Response) -> dict:
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    datas = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    return json.loads(datas[-1])


def _rpc(client, token, method, params=None, id_=1, session=None):
    headers = dict(MCP_HEADERS, authorization=f"Bearer {token}")
    if session:
        headers["mcp-session-id"] = session
    body = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        body["id"] = id_
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, json=body)


def _mcp_session(client, token):
    r = _rpc(client, token, "initialize", {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert r.status_code == 200, r.text
    session = r.headers.get("mcp-session-id")
    _rpc(client, token, "notifications/initialized", id_=None, session=session)
    return session
