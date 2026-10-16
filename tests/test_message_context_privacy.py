"""Independent R4 acceptance: synthetic MCP wire and HTTP only."""
import inspect
import json
from contextlib import nullcontext

import httpx
import pytest

from tests.test_manual_regressions import real_client, wire
from tests.test_parent_actions import transport
from vigilo_connector import server

SAS = 'https://Synthetic.BLOB.CORE.WINDOWS.NET/container/plan.pdf?sv=1&amp;SIG=synthetic-secret'


def decode(result):
    assert not result.get('isError'), result
    return json.loads(result['content'][0]['text'])


@pytest.mark.parametrize('tool', server.TOOLS, ids=lambda tool: tool.__name__)
def test_signed_blob_never_leaves_any_registered_tool(wire, monkeypatch, tool):
    data = {'profilePicture': {'url': 'https://profile.invalid/secret'},
            'attachments': [{'name': 'plan.pdf', 'mimeType': 'application/pdf', 'url': SAS}],
            'nested': [{'arbitrary': f'<a href="{SAS}">plan</a>'}],
            'serialized': json.dumps({'deep': SAS}), 'messageThreads': [], 'pageId': 0}
    class Fake:
        def fresh_reads(self, *args):
            return nullcontext()
        def __getattr__(self, name):
            if name in {'get', 'web_get'}:
                return lambda *a, **kw: httpx.Response(200, json=data)
            return lambda *a, **kw: data
    monkeypatch.setattr(server, '_client', Fake())
    arguments = {name: ([] if name == 'recipients' else 'synthetic')
                 for name, parameter in inspect.signature(tool).parameters.items()
                 if parameter.default is inspect.Parameter.empty}
    result = wire(tool.__name__, arguments)
    assert not result.get('isError'), result
    encoded = json.dumps(result).lower()
    assert 'synthetic-secret' not in encoded
    assert 'blob.core.windows.net' not in encoded
    assert 'profile.invalid' not in encoded
    assert 'hasprofilepicture' in encoded


@pytest.mark.parametrize('tool', ['api_get', 'web_api_get'])
@pytest.mark.parametrize('prefix', ['', 'x' * 19950])
def test_raw_non_json_scrub_precedes_truncation(wire, real_client, tool, prefix):
    body = prefix + ' <a href="' + SAS + '">plan</a>'
    transport(real_client, lambda request: httpx.Response(200, text=body))
    real_client._web.close()
    real_client._web = httpx.Client(base_url='https://web.invalid',
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
    result = wire(tool, {'path': '/synthetic'})
    encoded = json.dumps(result).lower()
    assert not result.get('isError'), result
    assert 'blob.core.windows.net' not in encoded
    assert 'synthetic-secret' not in encoded


def test_starter_and_actual_last_sender_are_distinct_without_fallback(wire, real_client):
    rows = [{'threadUid': 'chosen', 'sender': {'id': 'guardian', 'name': 'Guardian'},
             'lastMessage': {'sender': {'id': 'employee', 'firstName': 'School', 'lastName': 'Employee'}, 'body': 'hello'}},
            {'threadUid': 'missing', 'sender': {'id': 'guardian'}, 'lastMessage': {'body': 'hello'}}]
    transport(real_client, lambda request: httpx.Response(200, json={'pageId': 0, 'messageThreads': rows}))
    page = decode(wire('list_message_threads', {'child_id': 'mine'}))
    first, second = page['messageThreads']
    assert 'sender' not in first
    assert first['threadStarter']['id'] == 'guardian'
    assert first['lastMessage']['sender']['id'] == 'employee'
    assert first['lastMessage']['sender']['name'] == 'School Employee'
    assert not second['lastMessage'].get('sender', {}).get('id')


@pytest.mark.parametrize('child_id,member,valid', [('mine', True, True), ('mine', False, False), ('stranger', True, False)])
def test_selected_child_requires_roster_and_thread_membership_and_reads_preserve_unread(wire, real_client, child_id, member, valid):
    calls = []
    unread = {'chosen': True}
    def handler(request):
        calls.append((request.method, request.url.path, dict(request.url.params)))
        assert request.method == 'GET'
        if request.url.path == '/api/children':
            return httpx.Response(200, json=[{'id': 'mine', 'firstName': 'My', 'lastName': 'Child'}])
        if request.url.path == '/api/message-threads':
            assert request.url.params['childIds'] == 'mine'
            return httpx.Response(200, json={'pageId': 0, 'messageThreads': [{'threadUid': 'chosen', 'isSeen': False}] if member else []})
        assert request.url.path == '/api/messages/threads/chosen'
        return httpx.Response(200, json={'threadUid': 'chosen', 'child': {'id': 'unrelated', 'firstName': 'Wrong'}, 'messages': [], 'isSeen': not unread['chosen']})
    transport(real_client, handler)
    result = wire('get_message_thread', {'thread_uid': 'chosen', 'child_id': child_id})
    if valid:
        detail = decode(result)
        assert detail['child']['id'] == 'mine'
        assert 'Wrong' not in json.dumps(detail)
        assert detail['isSeen'] is False
    else:
        assert result.get('isError') is True
    assert unread == {'chosen': True}
    assert all(method == 'GET' and not any(part.lower() in {'read', 'seen', 'readstatus', 'mark-read'} for part in path.split('/')) for method, path, _ in calls)


def test_no_selected_child_omits_arbitrary_group_child(wire, real_client):
    transport(real_client, lambda request: httpx.Response(200, json={
        'threadUid': 'chosen', 'child': {'id': 'unrelated', 'name': 'Wrong'}, 'messages': []}))
    result = decode(wire('get_message_thread', {'thread_uid': 'chosen'}))
    assert 'child' not in result
    assert 'Wrong' not in json.dumps(result)


def test_attachment_download_keeps_internal_url_but_wire_has_metadata_only(wire, real_client, monkeypatch):
    import vigilo_connector.client as module
    transport(real_client, lambda request: httpx.Response(200, json={'messages': [
        {'attachments': [{'name': 'picture.bin', 'mimeType': 'application/octet-stream', 'url': SAS}]}]}))
    downloads = []
    def download(url, **kwargs):
        downloads.append(url)
        return httpx.Response(200, content=b'\x00\x01', request=httpx.Request('GET', url))
    monkeypatch.setattr(module, 'logged_get', download)
    result = decode(wire('read_message_attachments', {'thread_uid': 'chosen'}))
    assert downloads == [SAS]
    assert result[0]['name'] == 'picture.bin'
    assert 'url' not in result[0]
    assert 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('error_type', [ValueError, httpx.TransportError])
def test_error_text_is_sanitized_on_actual_mcp_wire(wire, monkeypatch, error_type):
    class Broken:
        def get_children(self, **kwargs):
            raise error_type('Synthetic failure ' + SAS)
    monkeypatch.setattr(server, '_client', Broken())
    result = wire('list_children', {})
    assert result.get('isError') is True
    assert 'synthetic-secret' not in json.dumps(result).lower()
    assert 'blob.core.windows.net' not in json.dumps(result).lower()


def test_selected_child_membership_paging_does_not_invent_names(wire, real_client):
    pages = []
    def handler(request):
        assert request.method == 'GET'
        if request.url.path == '/api/children':
            return httpx.Response(200, json=[{'childId': 'mine'}])
        if request.url.path == '/api/message-threads':
            page = int(request.url.params.get('pageId', 0))
            pages.append(page)
            rows = [{'threadUid': f'other-{i}'} for i in range(50)] if page == 0 else [{'threadUid': 'chosen'}]
            return httpx.Response(200, json={'pageId': page, 'messageThreads': rows})
        return httpx.Response(200, json={'threadUid': 'chosen', 'child': {'id': 'wrong', 'name': 'Wrong Name'}, 'messages': []})
    transport(real_client, handler)
    result = decode(wire('get_message_thread', {'thread_uid': 'chosen', 'child_id': 'mine'}))
    assert pages == [0, 1]
    assert result['child'].get('childId', result['child'].get('id')) == 'mine'
    assert 'Wrong' not in json.dumps(result)
    assert not result['child'].get('name')
    assert not result['child'].get('firstName')


def test_compact_snippet_sanitizes_before_cutting_url(wire, real_client):
    body = 'x' * 160 + SAS
    transport(real_client, lambda request: httpx.Response(200, json={'pageId': 0, 'messageThreads': [
        {'threadUid': 'chosen', 'lastMessage': {'body': body}}]}))
    result = decode(wire('list_message_threads', {'child_id': 'mine'}))
    assert len(result['messageThreads'][0]['lastMessage']['snippet']) <= 200
    assert 'blob.core.windows.net' not in json.dumps(result).lower()


def test_signed_url_in_arbitrary_json_key_is_private(wire, real_client):
    transport(real_client, lambda request: httpx.Response(200, json={SAS: 'synthetic'}))
    result = wire('api_get', {'path': '/synthetic'})
    assert not result.get('isError'), result
    assert 'synthetic-secret' not in json.dumps(result).lower()


@pytest.mark.parametrize('url', [
    'HTTPS://synthetic.blob.core.windows.net/file?SIG=synthetic-secret&sv=1',
    'https://synthetic.blob.core.windows.net/file?sv=1&AMP;sig=synthetic-secret',
    'https://synthetic.blob.core.windows.net/file?sv=1&#38;sig=synthetic-secret',
    'https://synthetic.blob.core.windows.net/file?sv=1&#x26;sig=synthetic-secret',
    r'https:\/\/synthetic.blob.core.windows.net\/file?sv=1&sig=synthetic-secret',
])
def test_encoded_signed_url_variants_in_nested_text(wire, real_client, url):
    transport(real_client, lambda request: httpx.Response(200, json={'text': json.dumps({'link': url})}))
    result = wire('api_get', {'path': '/synthetic'})
    assert not result.get('isError'), result
    assert 'synthetic-secret' not in json.dumps(result).lower()


@pytest.mark.parametrize('page_id', [None, True, -1])
def test_child_membership_invalid_paging_fails_before_detail(wire, real_client, page_id):
    paths = []
    def handler(request):
        paths.append(request.url.path)
        assert request.method == 'GET'
        if request.url.path == '/api/children':
            return httpx.Response(200, json=[{'id': 'mine'}])
        assert request.url.path == '/api/message-threads'
        return httpx.Response(200, json={'pageId': page_id, 'messageThreads': [{'threadUid': 'chosen'}]})
    transport(real_client, handler)
    assert wire('get_message_thread', {'thread_uid': 'chosen', 'child_id': 'mine'}).get('isError') is True
    assert '/api/messages/threads/chosen' not in paths


def test_child_membership_stops_at_page_limit_without_detail(wire, real_client):
    pages = []
    def handler(request):
        assert request.method == 'GET'
        if request.url.path == '/api/children':
            return httpx.Response(200, json=[{'id': 'mine'}])
        assert request.url.path == '/api/message-threads'
        page = int(request.url.params.get('pageId', 0))
        pages.append(page)
        return httpx.Response(200, json={'pageId': page, 'messageThreads': [
            {'threadUid': f'other-{page}-{i}'} for i in range(50)]})
    transport(real_client, handler)
    result = wire('get_message_thread', {'thread_uid': 'chosen', 'child_id': 'mine'})
    assert result.get('isError') is True
    assert pages == list(range(100))
