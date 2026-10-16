"""Small R4 followup checks through the real offline MCP gateway wire."""
import json

import httpx

from tests.test_manual_regressions import real_client, wire
from tests.test_parent_actions import transport


def test_advertised_thread_metadata_has_optional_child_and_list_default_20(wire):
    tools = {tool['name']: tool for tool in wire.list_tools()}
    detail = tools['get_message_thread']['inputSchema']
    assert 'child_id' in detail['properties']
    assert 'child_id' not in detail.get('required', [])
    assert detail['properties']['child_id']['default'] is None
    listing = tools['list_message_threads']['inputSchema']
    assert listing['properties']['page_size']['default'] == 20
    assert listing['properties']['page_size']['minimum'] == 1
    assert listing['properties']['page_size']['maximum'] == 20


def test_omitted_list_page_size_dispatches_20_on_actual_wire(wire, real_client):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path == '/api/message-threads'
        assert request.url.params['pageSize'] == '20'
        assert request.url.params['childIds'] == 'test-child'
        return httpx.Response(200, json={'pageId': 0, 'messageThreads': []})
    transport(real_client, handler)
    result = wire('list_message_threads', {'child_id': 'test-child'})
    assert not result.get('isError'), result
    assert json.loads(result['content'][0]['text'])['hasMore'] is False
    assert len(calls) == 1


def test_bad_child_error_uses_nynorsk_valt_on_actual_wire(wire, real_client):
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/api/children'
        return httpx.Response(200, json=[])
    transport(real_client, handler)
    result = wire('get_message_thread', {
        'thread_uid': 'test-thread', 'child_id': 'test-unknown-child'})
    assert result.get('isError') is True
    message = '\n'.join(block.get('text', '') for block in result['content']).lower()
    assert 'valt' in message
    assert 'valgt' not in message


def test_explicit_oversized_list_page_is_rejected_before_vigilo_call(wire, real_client):
    calls = []
    def handler(request):
        calls.append(request)
        raise AssertionError("Vigilo must not be called for an oversized page")
    transport(real_client, handler)
    result = wire('list_message_threads', {
        'child_id': 'test-child', 'page_size': 50})
    assert result.get('isError') is True
    message = '\n'.join(block.get('text', '') for block in result['content']).lower()
    assert 'page_size' in message and '20' in message
    assert calls == []
