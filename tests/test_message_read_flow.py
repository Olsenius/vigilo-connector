"""Offline MCP regressions for selecting and reading one ordinary thread."""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, wire
from tests.test_parent_actions import transport


def payload(result):
    assert not result.get('isError'), result
    return json.loads(result['content'][0]['text'])


def test_removed_global_search_is_not_advertised_or_dispatched(wire, real_client):
    transport(real_client, lambda request: pytest.fail('Removed tool reached API'))
    names = {tool['name'] for tool in wire.list_tools()}
    assert 'search_messages' not in names
    assert {'list_message_threads', 'get_message_thread', 'search_message_thread'} <= names
    result = wire('search_messages', {'child_id': 'test-child', 'query': 'test'})
    assert result.get('isError') is True
    assert real_client._store.refreshes == []


def test_list_then_selected_thread_paging_never_reads_other_threads(wire, real_client):
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.method == 'GET'  # Reading must not mark a thread read.
        if request.url.path == '/api/message-threads':
            assert request.url.params['childIds'] == 'test-child'
            return httpx.Response(200, json={'pageId': 0, 'messageThreads': [
                {'threadUid': uid, 'title': 'Testtittel ' + uid,
                 'lastMessage': {'body': 'Testutdrag ' + uid}}
                for uid in ('test-chosen', 'test-other')]})
        assert request.url.path == '/api/messages/threads/test-chosen'
        page_id = int(request.url.params.get('pageId', 0))
        return httpx.Response(200, json={'threadUid': 'test-chosen', 'pageId': page_id,
            'messages': [{'id': f'test-message-{page_id}', 'body': f'Testinnhold {page_id}'}]})
    transport(real_client, handler)
    listing = payload(wire('list_message_threads', {'child_id': 'test-child', 'page_size': 2}))
    assert len(calls) == 1
    assert listing['hasMore'] and listing['nextPageId'] == 1
    assert [(item['threadUid'], item['title'], item['lastMessage']['snippet'])
            for item in listing['messageThreads']] == [
        (uid, 'Testtittel ' + uid, 'Testutdrag ' + uid) for uid in ('test-chosen', 'test-other')]
    for page_id in (0, 1):
        arguments = {'thread_uid': 'test-chosen', 'page_size': 3}
        if page_id:
            arguments['page_id'] = page_id
        detail = payload(wire('get_message_thread', arguments))
        assert detail['pageId'] == page_id
        assert detail['messages'][0]['body'] == f'Testinnhold {page_id}'
    assert len(calls) == 3
    assert calls[1][2].get('pageId') is None
    assert calls[2][2]['pageId'] == '1'
    assert all(item[2]['pageSize'] == '3' for item in calls[1:])


def test_single_thread_search_pages_only_selected_id(wire, real_client):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path == '/api/messages/threads/test-chosen'
        page_id = int(request.url.params.get('pageId', 0))
        return httpx.Response(200, json={'threadUid': 'test-chosen', 'pageId': page_id,
            'messages': [{'id': 'test-message', 'body': 'Test søkeord'}] if not page_id else []})
    transport(real_client, handler)
    result = payload(wire('search_message_thread', {'thread_uid': 'test-chosen', 'query': 'søkeord'}))
    assert result['complete'] is True
    assert result['pagesFetched'] == 2
    assert result['matches'][0]['message']['id'] == 'test-message'
    assert len(calls) == 2


@pytest.mark.parametrize('timeout', [0, -1, True, float('nan'), float('inf'), 121])
def test_single_thread_invalid_timeout_never_reaches_auth_or_api(real_client, timeout):
    transport(real_client, lambda request: pytest.fail('Invalid timeout reached API'))
    with pytest.raises(ValueError):
        real_client.search_message_thread('test-chosen', 'test', timeout_s=timeout)
    assert real_client._store.refreshes == []
