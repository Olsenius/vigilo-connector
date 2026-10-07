from mcp.server.mcpserver import MCPServer

from vigilo_connector import server

EXPECTED = {
    "list_children",
    "list_message_threads",
    "get_message_thread",
    "read_message_attachments",
    "read_post_attachments",
    "api_get",
    "web_list_children",
    "news_feed",
    "absences",
    "consent_forms",
    "timetable",
    "scheduling_events",
    "web_api_get",
}


async def _names(mcp: MCPServer) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


async def test_register_tools_gir_alle_verktoy():
    mcp = MCPServer("test")
    server.register_tools(mcp)
    assert await _names(mcp) == EXPECTED


async def test_stdio_serveren_har_samme_verktoy():
    assert await _names(server.mcp) == EXPECTED
