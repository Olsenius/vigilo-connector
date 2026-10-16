"""Independent timeout regressions. All requests and credentials are synthetic."""
import json
import re
import threading
import time

import httpx
import pytest

from tests.test_manual_regressions import real_client, wire, visible_error
from tests.test_parent_actions import transport
from vigilo_connector import auth
from vigilo_connector.transport import android_client


def assert_diagnostic(text, phase, configured=45):
    assert f'phase={phase}' in text
    assert re.search(rf'timeout_s={configured}(?:\.0)?(?:\D|$)', text)
    match = re.search(r'elapsed_s=(\d+(?:\.\d+)?)', text)
    assert match, text
    assert float(match.group(1)) <= 20.001
    assert 'synthetic-secret' not in text


def test_actual_wire_thread_timeout_has_connect_five_read_fifteen(wire, real_client):
    observed = []
    def handler(request):
        observed.append(request.extensions['timeout'])
        raise httpx.ReadTimeout('synthetic-secret', request=request)
    transport(real_client, handler)
    text = visible_error(wire('search_message_thread', {'thread_uid': 'test-thread', 'query': 'test'}))
    assert_diagnostic(text, 'scan')
    assert len(observed) == 1
    assert observed[0]['connect'] == pytest.approx(5, abs=0.05)
    assert observed[0]['read'] == pytest.approx(15, abs=0.05)


def expired_store(tmp_path):
    store = auth.TokenStore(tmp_path / 'synthetic-tokens.json')
    store.path.write_text(json.dumps({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh',
                                     'expires_in': 0, 'obtained_at': 0}))
    return store


def install_auth_transport(monkeypatch, handler):
    monkeypatch.setattr(auth, '_basic_auth', lambda: 'synthetic-basic')
    monkeypatch.setattr(auth, 'android_client', lambda base_url: android_client(
        base_url, transport=httpx.MockTransport(handler)))


def test_actual_wire_auth_timeout_is_bounded_and_does_not_dispatch_thread(wire, real_client, tmp_path, monkeypatch):
    store = expired_store(tmp_path)
    real_client._store = store
    auth_calls, api_calls = [], []
    def handler(request):
        auth_calls.append(request)
        assert request.extensions['timeout']['connect'] <= 5
        assert request.extensions['timeout']['read'] <= 15
        raise httpx.ReadTimeout('synthetic-secret', request=request)
    install_auth_transport(monkeypatch, handler)
    transport(real_client, lambda request: api_calls.append(request))
    before = store.path.read_text()
    started = time.monotonic()
    text = visible_error(wire('search_message_thread', {'thread_uid': 'test-thread', 'query': 'test'}))
    assert time.monotonic() - started < 20
    assert_diagnostic(text, 'auth')
    assert len(auth_calls) == 1 and not api_calls
    assert store.path.read_text() == before


def test_actual_wire_refresh_lock_wait_respects_search_deadline(wire, real_client, tmp_path):
    store = expired_store(tmp_path)
    real_client._store = store
    store._lock.acquire()
    try:
        started = time.monotonic()
        text = visible_error(wire('search_message_thread', {'thread_uid': 'test-thread', 'query': 'test', 'timeout_s': 0.04}))
        assert time.monotonic() - started < 0.8
        assert_diagnostic(text, 'auth_lock', configured=0.04)
    finally:
        store._lock.release()


def test_rotated_refresh_is_saved_before_expired_search_returns(wire, real_client, tmp_path, monkeypatch):
    store = expired_store(tmp_path)
    real_client._store = store
    calls = []
    def handler(request):
        calls.append(request)
        # A response can arrive at the edge of the total deadline. Persist the
        # rotated pair before propagating cancellation; never retry old tokens.
        time.sleep(0.035)
        return httpx.Response(200, json={'access_token': 'synthetic-new',
            'refresh_token': 'synthetic-rotated', 'expires_in': 3600})
    install_auth_transport(monkeypatch, handler)
    api_calls = []
    transport(real_client, lambda request: api_calls.append(request))
    text = visible_error(wire('search_message_thread', {'thread_uid': 'test-thread', 'query': 'test', 'timeout_s': 0.02}))
    assert_diagnostic(text, 'auth', configured=0.02)
    saved = store.load()
    assert saved['access_token'] == 'synthetic-new'
    assert saved['refresh_token'] == 'synthetic-rotated'
    assert len(calls) == 1 and not api_calls


def test_concurrent_searches_share_single_refresh_and_rotated_pair(real_client, tmp_path, monkeypatch):
    store = expired_store(tmp_path)
    real_client._store = store
    calls = []
    started = threading.Event()
    def handler(request):
        calls.append(request)
        started.set()
        time.sleep(0.04)
        return httpx.Response(200, json={'access_token': 'synthetic-new',
            'refresh_token': 'synthetic-rotated', 'expires_in': 3600})
    install_auth_transport(monkeypatch, handler)
    def api(request):
        assert request.headers['authorization'] == 'Bearer synthetic-new'
        return httpx.Response(200, json={'threadUid': 'test-thread', 'messages': []})
    transport(real_client, api)
    outcomes = []
    def search():
        try:
            outcomes.append(real_client.search_message_thread('test-thread', 'test'))
        except Exception as error:
            outcomes.append(error)
    first = threading.Thread(target=search)
    second = threading.Thread(target=search)
    first.start()
    assert started.wait(1)
    second.start()
    first.join(1)
    second.join(1)
    assert not first.is_alive() and not second.is_alive()
    assert len(outcomes) == 2 and all(isinstance(item, dict) and item['complete'] for item in outcomes)
    assert len(calls) == 1
    assert store.load()['refresh_token'] == 'synthetic-rotated'


def test_auth_lock_wait_has_own_fifteen_second_cap(wire, real_client, tmp_path):
    store = expired_store(tmp_path)
    waits = []
    class BusyLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            return False
    store._lock = BusyLock()
    real_client._store = store
    text = visible_error(wire('search_message_thread', {'thread_uid': 'test-thread', 'query': 'test'}))
    assert_diagnostic(text, 'auth_lock')
    assert len(waits) == 1 and 0 < waits[0] <= 15


def test_thread_auth_failure_keeps_one_correct_phase(wire, real_client, tmp_path, monkeypatch):
    store = expired_store(tmp_path)
    store.save({'access_token': 'synthetic-old', 'refresh_token': 'synthetic-refresh', 'expires_in': 3600})
    real_client._store = store
    calls = []
    def api(request):
        calls.append(request)
        return httpx.Response(401)
    def oauth(request):
        raise httpx.ReadTimeout('synthetic-secret', request=request)
    transport(real_client, api)
    install_auth_transport(monkeypatch, oauth)
    text = visible_error(wire('search_message_thread', {'thread_uid': 'test-thread', 'query': 'test'}))
    assert_diagnostic(text, 'auth')
    assert text.count('phase=') == 1
    assert len(calls) == 1
