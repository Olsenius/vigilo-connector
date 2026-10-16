"""Independent regressions for the failed manual checks; synthetic data only."""
import json
from contextlib import nullcontext

import httpx
import pytest
from starlette.testclient import TestClient

from tests.gateway.test_app import API_TOKEN, PUBLIC, _mcp_session, _rpc, _sse_json
from tests.test_parent_actions import FakeStore, transport
from vigilo_connector import config, server
from vigilo_connector.client import VigiloClient
from vigilo_connector.gateway import app as gateway
from vigilo_connector.gateway.settings import load_settings

PROFILE_URL = 'https://images.example.test/avatar?sig=synthetic-profile-signature'
ATTACHMENT_URL = 'https://files.example.test/plan.pdf?sig=synthetic-attachment-signature'


@pytest.fixture
def real_client():
    instance = VigiloClient(FakeStore())
    yield instance
    instance.close()


@pytest.fixture
def wire(tmp_path, monkeypatch, real_client):
    monkeypatch.setattr(config, 'CONFIG_DIR', tmp_path / 'vigilo')
    monkeypatch.setattr(config, 'CONFIG_FILE', tmp_path / 'vigilo' / 'config.json')
    settings = load_settings({'DATA_DIR': str(tmp_path), 'PUBLIC_URL': PUBLIC,
                             'ADMIN_PASSWORD': 'synthetic-password-123', 'API_TOKEN': API_TOKEN})
    previous = server._client
    application = gateway.create_app(settings, PUBLIC, vigilo_client=real_client)
    with TestClient(application, base_url=PUBLIC) as http:
        session = _mcp_session(http, API_TOKEN)
        def call(name, arguments):
            response = _rpc(http, API_TOKEN, 'tools/call',
                            {'name': name, 'arguments': arguments}, session=session)
            assert response.status_code == 200
            return _sse_json(response)['result']
        call.list_tools = lambda: _sse_json(_rpc(http, API_TOKEN, 'tools/list', {}, session=session))['result']['tools']
        yield call
    server._client = previous


def thread_page(page_id, body='Testinnhold'):
    return {'threadUid': 'test-thread', 'pageId': page_id, 'isReplyDisabled': False,
            'messages': [{'id': f'test-message-{page_id}', 'body': body, 'timeCreated': '2026-10-01T12:00:00Z',
                          'sender': {'id': 'test-sender', 'profilePicture': {'url': PROFILE_URL}},
                          'attachments': [{'name': 'Testplan.pdf', 'url': ATTACHMENT_URL}]}]}


def visible_error(result):
    assert result.get('isError') is True
    text = '\n'.join(block.get('text', '') for block in result['content'])
    assert 'unexpected error' not in text.lower()
    return text


def test_actual_gateway_wire_exposes_page_limit_and_progress(wire, real_client):
    def handler(request):
        assert request.method == 'GET'
        return httpx.Response(200, json=thread_page(int(request.url.params.get('pageId', 0))))
    transport(real_client, handler)
    text = visible_error(wire('search_message_thread', {
        'thread_uid': 'test-thread', 'query': 'test', 'max_pages': 1}))
    assert 'max_pages' in text
    assert 'ikke komplett' in text.lower() or 'ikkje komplett' in text.lower()
    assert '1' in text  # The completed page count must survive the MCP protocol.


@pytest.mark.parametrize('name,args', [
    ('list_children', {}),
    ('get_message_thread', {'thread_uid': 'test-thread'}),
    ('message_participants', {'thread_uid': 'test-thread', 'child_id': 'test-child'}),
    ('api_get', {'path': '/api/test'}),
    ('web_api_get', {'path': '/api/test'}),
])
def test_profile_urls_absent_from_actual_wire_and_attachments_preserved(wire, real_client, name, args):
    payload = {'items': [{'sender': {'profilePicture': {'url': PROFILE_URL},
                                   'nested': {'profilePictureDto': {'url': PROFILE_URL}}},
                          'attachments': [{'name': 'Testplan.pdf', 'url': ATTACHMENT_URL}]}]}
    transport(real_client, lambda request: httpx.Response(200, json=payload))
    real_client._web.close()
    real_client._web = httpx.Client(base_url='https://web.example.test',
                                   transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    result = wire(name, args)
    assert result.get('isError') is not True, result
    encoded = json.dumps(result)
    assert PROFILE_URL not in encoded
    assert 'synthetic-profile-signature' not in encoded
    assert ATTACHMENT_URL not in encoded
    assert 'hasProfilePicture' in encoded


def test_list_wire_is_compact_with_sender_and_paging(wire, real_client):
    def handler(request):
        return httpx.Response(200, json={'pageId': 7, 'unreadMessageThreadsCount': 3,
            'messageThreads': [{'threadUid': f'test-thread-{index}', 'title': 'Testtittel',
                'lastMessage': {'body': 'x' * 1000,
                    'sender': {'id': 'test-employee', 'firstName': 'Test', 'lastName': 'Ansatt',
                               'communicatorType': 'EMPLOYEE', 'profilePicture': {'url': PROFILE_URL}},
                    'attachments': [{'url': ATTACHMENT_URL}]}} for index in range(2)]})
    transport(real_client, handler)
    result = wire('list_message_threads', {'child_id': 'test-child', 'page_size': 2})
    assert result.get('isError') is not True
    page = json.loads(result['content'][0]['text'])
    assert page['pageId'] == 7
    assert page['hasMore'] is True
    assert page['nextPageId'] == 8
    assert len(page['messageThreads']) == 2
    for thread in page['messageThreads']:
        assert len(thread['lastMessage']['snippet']) <= 200
        assert thread['lastMessage']['sender']['id'] == 'test-employee'
        assert thread['lastMessage']['sender']['name'] == 'Test Ansatt'
        assert thread['lastMessage']['sender']['communicatorType'] == 'EMPLOYEE'
        assert 'body' not in thread['lastMessage']
    assert PROFILE_URL not in json.dumps(result)


@pytest.mark.parametrize('tool', server.TOOLS, ids=lambda tool: tool.__name__)
def test_profile_url_policy_on_every_registered_tool_wire(wire, monkeypatch, tool):
    """Fake client methods never dispatch: covers every registered output path."""
    import inspect
    payload = {'profilePicture': {'url': PROFILE_URL},
               'items': [{'deep': [{'profilePictureDto': {'url': PROFILE_URL}}]}],
               'attachments': [{'url': ATTACHMENT_URL}],
               'messageThreads': [], 'pageId': 0}
    class OutputClient:
        def fresh_reads(self, force_refresh=True):
            return nullcontext()

        def __getattr__(self, name):
            if name in {'get', 'web_get'}:
                return lambda *a, **kw: httpx.Response(200, json=payload)
            return lambda *a, **kw: payload
    monkeypatch.setattr(server, '_client', OutputClient())
    arguments = {}
    for name, parameter in inspect.signature(tool).parameters.items():
        if parameter.default is inspect.Parameter.empty:
            arguments[name] = [] if name == 'recipients' else 'test-placeholder'
    result = wire(tool.__name__, arguments)
    assert result.get('isError') is not True, result
    text = json.dumps(result)
    assert 'synthetic-profile-signature' not in text
    assert 'hasProfilePicture' in text
    assert ATTACHMENT_URL not in text


def test_raw_json_is_sanitized_before_output_truncation(wire, real_client):
    payload = {'profilePicture': {'url': PROFILE_URL}, 'filler': 'x' * 25000}
    transport(real_client, lambda request: httpx.Response(200, json=payload))
    result = wire('api_get', {'path': '/api/test'})
    assert result.get('isError') is not True
    assert 'synthetic-profile-signature' not in json.dumps(result)
    assert 'hasProfilePicture' in json.dumps(result)


@pytest.mark.parametrize('failure', ['connection', 'invalid-json', 'http'])
@pytest.mark.parametrize('tool', ['search_message_thread'])
def test_search_failure_modes_are_explicit_incomplete_on_wire(wire, real_client, failure, tool):
    def handler(request):
        assert request.method == 'GET'
        if failure == 'connection':
            raise httpx.ConnectError('synthetic-sensitive-url-must-not-forward', request=request)
        if failure == 'invalid-json':
            return httpx.Response(200, content=b'synthetic-invalid-json-must-not-forward')
        return httpx.Response(403, json={'message': 'synthetic-body-must-not-forward'})
    transport(real_client, handler)
    arguments = {'query': 'test', 'thread_uid': 'test-id'}
    text = visible_error(wire(tool, arguments))
    assert 'ikkje komplett' in text or 'ikke komplett' in text
    assert 'pagesFetched=' in text
    assert 'messagesScanned=' in text
    assert 'synthetic-' not in text


@pytest.mark.parametrize('tool', ['search_message_thread'])
def test_invalid_search_paging_error_is_nynorsk(wire, tool):
    args = {'query': 'test', 'page_size': 0,
            'thread_uid': 'test-id'}
    text = visible_error(wire(tool, args))
    assert 'page_size' in text
    assert 'må vere' in text
    assert 'heiltal' in text
    assert 'være' not in text
