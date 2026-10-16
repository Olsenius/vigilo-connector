"""Uttømmende søk bruker bare dokumentert paging for ordinære detaljer."""

import httpx
import pytest

from tests.test_parent_actions import FakeStore, transport
from vigilo_connector.client import SearchIncomplete, VigiloClient


@pytest.fixture
def client():
    c = VigiloClient(FakeStore())
    yield c
    c.close()


def message(id_, body='Testinnhold', attachments=None):
    return {'id': id_, 'body': body, 'attachments': attachments or [], 'isDeleted': False, 'timeCreated': '2026-10-01T12:00:00Z'}


def page(id_, messages):
    return {'threadUid': 'test-thread', 'pageId': id_, 'messages': messages,
            'isReplyDisabled': False}


def test_search_finds_older_body_and_attachment_name_until_empty_page(client):
    pages = [page(0, [message('test-new', 'Ny melding')]),
             page(1, [message('test-old', 'ELDRE informasjon')]),
             page(2, [message('test-file', attachments=[{'name': 'eldre-plan.pdf'}])]),
             page(3, [])]
    calls = []
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/api/messages/threads/test-thread'
        assert request.url.params['pageSize'] == '7'
        assert request.url.params.get('pageId') == (str(len(calls)) if calls else None)
        calls.append(request)
        return httpx.Response(200, json=pages[len(calls)-1])
    transport(client, handler)
    result = client.search_message_thread('test-thread', 'eldre', page_size=7)
    assert result['complete'] is True
    assert result['messagesScanned'] == 3
    assert result['pagesFetched'] == 4
    assert [m['message']['id'] for m in result['matches']] == ['test-old', 'test-file']
    assert [m['pageId'] for m in result['matches']] == [1, 2]
    # Each nonempty page was shorter than page_size; that is not a terminal signal.
    assert len(calls) == 4


def test_search_deduplicates_overlapping_messages_but_continues(client):
    pages = iter([page(0, [message('test-a')]),
                  page(1, [message('test-a'), message('test-b')]), page(2, [])])
    transport(client, lambda r: httpx.Response(200, json=next(pages)))
    result = client.search_message_thread('test-thread', 'test')
    assert result['messagesScanned'] == 2
    assert len(result['matches']) == 2


def test_search_no_match_is_complete_only_after_empty_page(client):
    pages = iter([page(0, [message('test-a')]), page(1, [])])
    transport(client, lambda r: httpx.Response(200, json=next(pages)))
    result = client.search_message_thread('test-thread', 'finnes ikke')
    assert result['matches'] == []
    assert result['complete'] is True
    assert result['pagesFetched'] == 2


@pytest.mark.parametrize('broken', [
    page(0, [message('test-b')]),  # repeated page id
    page(2, [message('test-b')]),  # skipped requested page
    page(1, [message('test-a')]),  # no message progress
    {'threadUid': 'test-thread', 'messages': [message('test-b')]},
    {'threadUid': 'other-thread', 'pageId': 1, 'messages': []},
    {'threadUid': 'test-thread', 'pageId': 1, 'messages': 'not a list'},
    page(1, [{'body': 'Missing id'}]),
    page(1, [{'id': 'test-b', 'body': None}]),
    page(1, [{'id': 'test-b', 'body': 'test', 'attachments': [None]}]),
])
def test_broken_paging_never_claims_complete(client, broken):
    pages = iter([page(0, [message('test-a')]), broken])
    transport(client, lambda r: httpx.Response(200, json=next(pages)))
    with pytest.raises(RuntimeError):
        client.search_message_thread('test-thread', 'test')


def test_local_page_limit_never_silently_truncates(client):
    transport(client, lambda r: httpx.Response(200, json=page(0, [message('test-a')])))
    with pytest.raises(RuntimeError, match='Sidegrensa'):
        client.search_message_thread('test-thread', 'test', max_pages=1)


@pytest.mark.parametrize('failure', [403, 500, 'timeout'])
def test_search_failure_does_not_return_partial_matches(client, failure):
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json=page(0, [message('test-a')]))
        if failure == 'timeout':
            raise httpx.ReadTimeout('synthetic timeout', request=request)
        return httpx.Response(failure)
    transport(client, handler)
    with pytest.raises(SearchIncomplete):
        client.search_message_thread('test-thread', 'test')
    assert len(calls) == 2


@pytest.mark.parametrize('changes', [
    {'thread_uid': '..'}, {'thread_uid': ''}, {'query': ' '}, {'query': None},
    {'page_size': 0}, {'max_pages': 0}, {'max_pages': True},
])
def test_invalid_search_inputs_never_reach_api(client, changes):
    transport(client, lambda r: pytest.fail('Invalid search reached API'))
    args = {'thread_uid': 'test-thread', 'query': 'test'}
    args.update(changes)
    with pytest.raises(ValueError):
        client.search_message_thread(**args)
