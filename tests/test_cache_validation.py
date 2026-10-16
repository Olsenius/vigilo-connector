"""Independent cache checks using synthetic HTTP transport only."""
from concurrent.futures import ThreadPoolExecutor
import threading

import httpx
import pytest

from tests.test_parent_actions import FakeStore, transport
from vigilo_connector import server
from vigilo_connector.client import VigiloClient, WriteOutcomeUnknown
from vigilo_connector.read_cache import ReadCache


@pytest.fixture
def client():
    value = VigiloClient(FakeStore())
    yield value
    value.close()


def listing(page_id=0, threads=None):
    return {"pageId": page_id, "messageThreads": threads or []}


def detail(page_id=0, messages=None):
    return {"threadUid": "test-thread", "pageId": page_id,
            "messages": messages or [], "isReplyDisabled": False}


def read_list(client, page_size=20, page_id=None, force_refresh=False):
    with client.fresh_reads(force_refresh):
        return client.get_message_threads_page("test-child", page_size, True, page_id)


class ManualTimer:
    """Captures the sweeper instead of starting a real thread."""
    created = []

    def __init__(self, delay, callback):
        self.delay, self.callback, self.cancelled, self.daemon = delay, callback, False, False
        ManualTimer.created.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def test_repeated_list_uses_no_duplicate_get_and_deep_copies(client):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=listing(threads=[{"threadUid": "test-a"}]))
    transport(client, handle)
    first = read_list(client)
    first["messageThreads"][0]["threadUid"] = "mutated"
    assert read_list(client)["messageThreads"][0]["threadUid"] == "test-a"
    assert len(calls) == 1


@pytest.mark.parametrize("page_id", [None, 1])
def test_thread_details_are_never_cached(client, page_id):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=detail(page_id or 0, [{"id": f"m{len(calls)}", "body": "test"}]))
    transport(client, handle)
    assert client.get_thread("test-thread", 50, page_id)["messages"][0]["id"] == "m1"
    assert client.get_thread("test-thread", 50, page_id)["messages"][0]["id"] == "m2"
    assert len(calls) == 2


def test_page_size_page_id_and_after_school_are_distinct_cache_keys(client):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=listing())
    transport(client, handle)
    variants = [(20, None, True), (20, 1, True), (7, None, True), (20, None, False)]
    for _ in range(2):
        for size, page, sfo in variants:
            client.get_message_threads_page("test-child", size, sfo, page)
    assert len(calls) == 4


@pytest.mark.parametrize("failure", [503, "read"])
def test_errors_are_not_cached(client, failure):
    calls = []
    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            if failure == "read":
                raise httpx.ReadError("synthetic", request=request)
            return httpx.Response(failure)
        return httpx.Response(200, json=listing())
    transport(client, handle)
    with pytest.raises(httpx.HTTPError):
        read_list(client)
    assert read_list(client) == listing()
    assert read_list(client) == listing()
    assert len(calls) == 2


def test_concurrent_same_list_is_single_flight(client):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def handle(request):
        calls.append(request)
        entered.set()
        assert release.wait(3)
        return httpx.Response(200, json=listing())
    transport(client, handle)
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(read_list, client) for _ in range(5)]
        assert entered.wait(3)
        release.set()
        results = [future.result(timeout=3) for future in futures]
    assert len(calls) == 1
    assert all(result == listing() for result in results)
    assert len({id(result) for result in results}) == 5


def test_search_always_reads_live_pages(client):
    calls = []
    pages = [detail(0, [{"id": "test-a", "body": "test"}, {"id": "test-b", "body": "test"}]),
             detail(1, [{"id": "test-c", "body": "test"}]), detail(2)]
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=pages[(len(calls) - 1) % 3])
    transport(client, handle)
    result = client.search_message_thread("test-thread", "test", page_size=2)
    assert result["complete"] is True
    assert result["messagesScanned"] == 3
    assert client.search_message_thread("test-thread", "test", page_size=2) == result
    assert len(calls) == 6


def test_expiry_refetches_over_http(client):
    clock = [0.0]
    client._read_cache.clock = lambda: clock[0]
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=listing())
    transport(client, handle)
    read_list(client)
    clock[0] = client._read_cache.ttl - 0.01
    read_list(client)
    assert len(calls) == 1
    clock[0] += 0.02
    read_list(client)
    assert len(calls) == 2


def test_forced_read_reaches_http_and_refreshes_only_that_key(client):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=listing() if "threads" in request.url.path else [])
    transport(client, handle)
    client.get_children()
    read_list(client)
    read_list(client, force_refresh=True)
    read_list(client)
    client.get_children()
    assert calls == ["/api/children", "/api/message-threads", "/api/message-threads"]


def test_forced_result_replaces_cached_value(client):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=listing(threads=[{"threadUid": f"t{len(calls)}"}]))
    transport(client, handle)
    assert read_list(client)["messageThreads"][0]["threadUid"] == "t1"
    assert read_list(client, force_refresh=True)["messageThreads"][0]["threadUid"] == "t2"
    assert read_list(client)["messageThreads"][0]["threadUid"] == "t2"
    assert len(calls) == 2


def test_401_still_refreshes_token_before_success_cache(client):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(401 if len(calls) == 1 else 200, json=listing())
    transport(client, handle)
    assert read_list(client) == listing()
    assert calls[0].headers["authorization"] == "Bearer old"
    assert calls[1].headers["authorization"] == "Bearer new"
    assert True in client._store.refreshes


@pytest.mark.parametrize("unknown", [False, True])
def test_attempted_write_invalidates_cached_list_even_with_unknown_outcome(client, unknown):
    reads = []

    def handle(request):
        if request.method == "GET":
            reads.append(request)
            return httpx.Response(200, json=listing(threads=[{"threadUid": str(len(reads))}]))
        if unknown:
            raise httpx.ReadTimeout("synthetic write timeout", request=request)
        return httpx.Response(204)

    transport(client, handle)
    assert read_list(client)["messageThreads"][0]["threadUid"] == "1"
    if unknown:
        with pytest.raises(WriteOutcomeUnknown):
            client._write_json("POST", "/api/test-write", {})
    else:
        client._write_json("POST", "/api/test-write", {})
    assert read_list(client)["messageThreads"][0]["threadUid"] == "2"
    assert read_list(client)["messageThreads"][0]["threadUid"] == "2"
    assert len(reads) == 2


def test_reply_preflight_reads_live_detail_and_blocks_disabled_reply(client):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        state = detail()
        state["isReplyDisabled"] = len(calls) > 1
        return httpx.Response(200, json=state)

    transport(client, handle)
    assert client.get_thread("test-thread")["isReplyDisabled"] is False
    with pytest.raises(ValueError, match="deaktivert"):
        client.reply_message("test-thread", "Testsvar")
    assert len(calls) == 2


def test_changed_auth_identity_cannot_reuse_previous_cached_list(client):
    identity = ["synthetic-session-a"]
    client._store.access_token = lambda force_refresh=False: identity[0]
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=listing(threads=[{"threadUid": str(len(calls))}]))

    transport(client, handle)
    assert read_list(client)["messageThreads"][0]["threadUid"] == "1"
    identity[0] = "synthetic-session-b"
    assert read_list(client)["messageThreads"][0]["threadUid"] == "2"
    assert read_list(client)["messageThreads"][0]["threadUid"] == "2"
    assert len(calls) == 2


def test_child_verification_retries_stale_list_before_failing(client):
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == "/api/children":
            return httpx.Response(200, json=[{"childId": "test-child"}])
        if request.url.path == "/api/message-threads":
            known = calls.count("/api/message-threads") > 1
            return httpx.Response(200, json=listing(
                threads=[{"threadUid": "test-new"}] if known else [{"threadUid": "test-old"}]))
        return httpx.Response(200, json={"threadUid": "test-new", "pageId": 0, "messages": []})
    transport(client, handle)
    previous = server._client
    server.use_client(client)
    try:
        read_list(client, 50)  # Warm the list cache before the new thread exists.
        result = server.get_message_thread("test-new", child_id="test-child")
    finally:
        server._client = previous
    assert '"childId": "test-child"' in result
    assert calls.count("/api/message-threads") == 2


# --- ReadCache unit checks --------------------------------------------------

def test_owner_failure_is_not_shared_with_waiters():
    cache = ReadCache(timer=None)
    started, release = threading.Event(), threading.Event()
    def owner_load():
        started.set()
        assert release.wait(3)
        raise TimeoutError("owner budget expired")
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.read, "key", owner_load)
        assert started.wait(3)
        waiter = pool.submit(cache.read, "key", lambda: "own result")
        release.set()
        with pytest.raises(TimeoutError):
            owner.result(timeout=3)
        assert waiter.result(timeout=3) == "own result"


def test_waiter_budget_check_stops_waiting():
    cache = ReadCache(timer=None)
    started, release = threading.Event(), threading.Event()
    def slow():
        started.set()
        assert release.wait(3)
        return "late"
    def expired():
        raise RuntimeError("waiter budget expired")
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(cache.read, "key", slow)
        assert started.wait(3)
        with pytest.raises(RuntimeError, match="waiter budget"):
            cache.read("key", lambda: "unused", check=expired)
        release.set()
        assert owner.result(timeout=3) == "late"
    assert not cache._flights


def test_older_flight_cannot_overwrite_newer_forced_result():
    cache = ReadCache(timer=None)
    started, release = threading.Event(), threading.Event()
    def old_load():
        started.set()
        assert release.wait(3)
        return "old"
    with ThreadPoolExecutor(max_workers=1) as pool:
        older = pool.submit(cache.read, "key", old_load)
        assert started.wait(3)
        assert cache.read("key", lambda: "new", bypass=True) == "new"
        release.set()
        assert older.result(timeout=3) == "old"
    assert cache.read("key", lambda: pytest.fail("should be cached")) == "new"
    assert not cache._flights


def test_forced_flight_is_not_removed_by_finished_older_flight():
    cache = ReadCache(timer=None)
    old_started, old_release = threading.Event(), threading.Event()
    new_started, new_release = threading.Event(), threading.Event()
    def old_load():
        old_started.set()
        assert old_release.wait(3)
        return "old"
    def new_load():
        new_started.set()
        assert new_release.wait(3)
        return "new"
    loads = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        older = pool.submit(cache.read, "key", old_load)
        assert old_started.wait(3)
        forced = pool.submit(cache.read, "key", new_load, bypass=True)
        assert new_started.wait(3)
        old_release.set()
        assert older.result(timeout=3) == "old"
        # A later reader joins the forced flight instead of starting a duplicate load.
        joined = pool.submit(cache.read, "key", lambda: loads.append(1) or "dup")
        new_release.set()
        assert forced.result(timeout=3) == "new"
        assert joined.result(timeout=3) == "new"
    assert loads == []


def test_clear_prevents_in_flight_result_from_being_stored():
    cache = ReadCache(timer=None)
    def load():
        cache.clear()  # Simulates a write attempt while the read is in flight.
        return "stale"
    assert cache.read("key", load) == "stale"
    assert cache.read("key", lambda: "fresh") == "fresh"


def test_sweeper_drops_expired_entries_without_new_reads():
    ManualTimer.created.clear()
    now = [0.0]
    cache = ReadCache(ttl=60, clock=lambda: now[0], timer=ManualTimer)
    cache.read("key", lambda: {"body": "sensitive"})
    timer = ManualTimer.created[-1]
    assert 60 <= timer.delay <= 60.1
    now[0] = 61
    timer.callback()
    assert not cache._entries and cache._bytes == 0
    assert len(ManualTimer.created) == 1  # Nothing left, so no new sweep.


def test_close_cancels_sweeper_and_clears_entries():
    ManualTimer.created.clear()
    cache = ReadCache(timer=ManualTimer)
    cache.read("key", lambda: "value")
    cache.close()
    assert ManualTimer.created[-1].cancelled
    assert not cache._entries


def test_byte_accounting_and_bounds():
    cache = ReadCache(max_entries=2, max_bytes=40, timer=None)
    cache.read("a", lambda: "x" * 10)
    cache.read("a", lambda: "y" * 20, bypass=True)
    assert cache._bytes == 22
    cache.read("b", lambda: "z" * 10)
    cache.read("c", lambda: "w" * 10)  # Entry limit evicts least recently used "a".
    assert list(cache._entries) == ["b", "c"] and cache._bytes == 24
    assert cache.read("huge", lambda: "h" * 100) == "h" * 100
    assert "huge" not in cache._entries and cache._bytes == 24
