"""«Siste nytt» via app-API-et (GET /api/news-feed, som NewsFeedRemoteMediator).

Berre syntetiske data.
"""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, visible_error, wire  # noqa: F401
from tests.test_parent_actions import client, transport  # noqa: F401
from tests.test_pickup_notes import CHILDREN
from vigilo_connector import client as client_module

AUTHOR = {'hasProfilePicture': False, 'id': 'test-employee', 'firstName': 'Test', 'lastName': 'Tilsett'}
FEED = [
    {'id': 'test-out', 'title': None, 'postedTime': '2026-10-30T14:01:47Z', 'type': 'checkOut',
     'postedBy': AUTHOR},
    {'id': 'test-post', 'title': 'Testoppslag', 'text': '<p>Testtekst</p>',
     'postedTime': '2026-10-30T12:00:00Z', 'type': 'post', 'postedBy': AUTHOR,
     'attachments': [{'id': 'test-att', 'name': 'test.jpeg', 'mimeType': 'image/jpeg',
                      'sequence': 0, 'description': None, 'resizedImages': {}}]},
    {'id': 'test-in', 'title': None, 'postedTime': '2026-10-30T06:00:00Z', 'type': 'checkIn',
     'postedBy': AUTHOR},
    # 23:30 UTC 28.10. er 29.10. i Oslo.
    {'id': 'test-plan', 'title': 'Testplan', 'postedTime': '2026-10-28T23:30:00Z',
     'type': 'pedagogicalPlan', 'postedBy': AUTHOR},
]


def api(requests, feed=None):
    def handler(request):
        requests.append(request)
        assert request.method == 'GET' and 'userId' not in request.url.params
        if request.url.path == '/api/children':
            return httpx.Response(200, json=CHILDREN)
        if request.url.path == '/api/news-feed':
            return httpx.Response(200, json=FEED if feed is None else feed)
        raise AssertionError(f'Uventa kall: {request.url.path}')
    return handler


def feed_request(requests):
    found = [r for r in requests if r.url.path == '/api/news-feed']
    assert len(found) == 1
    return dict(found[0].url.params)


@pytest.fixture
def today(monkeypatch):
    class Clock(client_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return client_module.datetime(2026, 10, 31, 9, 0, tzinfo=tz)
    monkeypatch.setattr(client_module, 'datetime', Clock)


def test_default_page_matches_app_query(client, today):
    requests = []
    transport(client, api(requests))
    result = client.news_feed('test-child', page_size=4)
    # Som appen: lastDate = i morgon (eksklusiv), minPageSize, childId i eintal.
    assert feed_request(requests) == {'childId': 'test-child', 'lastDate': '2026-11-01',
                                      'minPageSize': '4'}
    assert [i['id'] for i in result['items']] == ['test-post', 'test-plan']
    assert result['skippedCheckIns'] == 2
    assert result['items'][0]['attachments'][0]['id'] == 'test-att'
    assert result['nextBeforeDate'] == '2026-10-29'


def test_include_check_ins_keeps_everything(client):
    requests = []
    transport(client, api(requests))
    result = client.news_feed('test-child', before_date='2026-10-31', page_size=4,
                              include_check_ins=True)
    assert feed_request(requests)['lastDate'] == '2026-10-31'
    assert [i['id'] for i in result['items']] == ['test-out', 'test-post', 'test-in', 'test-plan']
    assert result['skippedCheckIns'] == 0


def test_short_page_has_no_next(client):
    requests = []
    transport(client, api(requests))
    assert client.news_feed('test-child', page_size=20)['nextBeforeDate'] is None
    requests.clear()
    transport(client, api(requests, feed=[]))
    result = client.news_feed('test-child')
    assert result['items'] == [] and result['nextBeforeDate'] is None


@pytest.mark.parametrize('args,match', [
    ({'child_id': 'test-unknown'}, 'Fann ikkje barnet'),
    ({'child_id': ' '}, 'child_id'),
    ({'before_date': '31.10.2026'}, 'YYYY-MM-DD'),
    ({'page_size': 0}, '1–50'),
    ({'page_size': 51}, '1–50'),
    ({'page_size': True}, '1–50'),
])
def test_invalid_arguments_make_no_feed_request(client, args, match):
    requests = []
    transport(client, api(requests))
    with pytest.raises(ValueError, match=match):
        client.news_feed(**{'child_id': 'test-child', **args})
    assert not [r for r in requests if r.url.path == '/api/news-feed']


def test_unexpected_shape_is_rejected(client):
    transport(client, api([], feed={'items': []}))
    with pytest.raises(ValueError, match='Ugyldig svar'):
        client.news_feed('test-child')


def test_news_feed_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    result = wire('news_feed', {'child_id': 'test-child', 'page_size': 4})
    assert result.get('isError') is not True, result
    body = json.loads(result['content'][0]['text'])
    assert [i['type'] for i in body['items']] == ['post', 'pedagogicalPlan']
    assert feed_request(requests)['minPageSize'] == '4'


def test_invalid_page_size_is_visible_over_mcp_wire(wire, real_client):
    requests = []
    transport(real_client, api(requests))
    error = visible_error(wire('news_feed', {'child_id': 'test-child', 'page_size': 99}))
    assert '1–50' in error
    assert not [r for r in requests if r.url.path == '/api/news-feed']
