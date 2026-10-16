import asyncio

import pytest

from vigilo_connector import server


def test_schema_advertises_list_page_size_bounds_and_default():
    tools = asyncio.run(server.mcp.list_tools())
    tool = next(item for item in tools if item.name == "list_message_threads")
    page_size = tool.input_schema["properties"]["page_size"]
    assert page_size["default"] == 20
    assert page_size["minimum"] == 1
    assert page_size["maximum"] == 20


def test_oversized_list_page_fails_before_client_access():
    class NoClientAccess:
        def __getattr__(self, name):
            raise AssertionError("client accessed before page-size validation")

    server.use_client(NoClientAccess())
    with pytest.raises(ValueError, match="1 til 20"):
        server.list_message_threads("synthetic-child", page_size=50)
