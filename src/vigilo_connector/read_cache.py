"""Bounded, process-local cache for explicitly selected authenticated JSON reads."""

from collections import OrderedDict
from concurrent.futures import Future, wait
from copy import deepcopy
import json
import threading
import time


class ReadCache:
    def __init__(self, ttl=60.0, max_entries=512, max_bytes=8 * 1024 * 1024,
                 clock=time.monotonic, timer=threading.Timer):
        self.ttl, self.max_entries, self.max_bytes = ttl, max_entries, max_bytes
        self.clock = clock
        self._timer = timer
        self._lock = threading.Lock()
        # key -> (expires, snapshot, size, ticket)
        self._entries = OrderedDict()
        # key -> (generation, ticket, future)
        self._flights = {}
        self._bytes = 0
        self._generation = 0
        self._tickets = 0
        self._sweeper = None

    def clear(self):
        with self._lock:
            self._generation += 1
            self._entries.clear()
            self._bytes = 0

    def close(self):
        self.clear()
        with self._lock:
            if self._sweeper is not None:
                self._sweeper.cancel()
                self._sweeper = None

    def purge(self):
        """Drop expired entries so response data does not outlive its TTL in memory."""
        with self._lock:
            self._sweeper = None
            self._purge_locked(self.clock())
            self._schedule_locked()

    def read(self, key, load, *, bypass=False, check=lambda: None):
        with self._lock:
            self._purge_locked(self.clock())
            flight = self._flights.get(key)
            if not bypass:
                entry = self._entries.get(key)
                if entry is not None:
                    self._entries.move_to_end(key)
                    return deepcopy(entry[1])
                if flight is not None and flight[0] == self._generation:
                    joined = flight[2]
                else:
                    joined = None
            else:
                # A forced read never joins an earlier snapshot, but only this key is refreshed.
                joined = None
            if joined is None:
                generation, ticket, future = self._generation, self._next_ticket(), Future()
                self._flights[key] = (generation, ticket, future)
        if joined is None:
            return self._load(key, load, generation, ticket, future)
        while not wait([joined], timeout=0.1).done:
            check()
        if joined.exception() is None:
            return deepcopy(joined.result())
        # The owner's failure may stem from its own budget or context; retry independently.
        with self._lock:
            generation, ticket = self._generation, self._next_ticket()
        return self._load(key, load, generation, ticket, None)

    def _next_ticket(self):
        self._tickets += 1
        return self._tickets

    def _load(self, key, load, generation, ticket, future):
        try:
            snapshot = deepcopy(load())
            size = len(json.dumps(snapshot, ensure_ascii=True).encode("utf-8"))
            with self._lock:
                self._store_locked(key, snapshot, size, generation, ticket)
            if future is not None:
                future.set_result(snapshot)
            return deepcopy(snapshot)
        except BaseException as exc:
            if future is not None:
                future.set_exception(exc)
            raise
        finally:
            if future is not None:
                with self._lock:
                    current = self._flights.get(key)
                    if current is not None and current[2] is future:
                        del self._flights[key]

    def _store_locked(self, key, snapshot, size, generation, ticket):
        if generation != self._generation or size > self.max_bytes or self.max_entries <= 0:
            return
        flight = self._flights.get(key)
        if flight is not None and flight[1] > ticket:
            return  # A newer read of this key is in progress; do not store older data.
        existing = self._entries.get(key)
        if existing is not None:
            if existing[3] > ticket:
                return  # A read that started later has already stored newer data.
            del self._entries[key]
            self._bytes -= existing[2]
        while self._entries and (len(self._entries) >= self.max_entries
                                 or self._bytes + size > self.max_bytes):
            _, (_, _, removed, _) = self._entries.popitem(last=False)
            self._bytes -= removed
        self._entries[key] = (self.clock() + self.ttl, snapshot, size, ticket)
        self._bytes += size
        self._schedule_locked()

    def _purge_locked(self, now):
        for old_key, (expires, _, size, _) in list(self._entries.items()):
            if expires <= now:
                self._bytes -= size
                del self._entries[old_key]

    def _schedule_locked(self):
        if not self._entries or self._sweeper is not None or self._timer is None:
            return
        soonest = min(expires for expires, _, _, _ in self._entries.values())
        delay = min(max(soonest - self.clock(), 0.0), self.ttl) + 0.05
        self._sweeper = self._timer(delay, self.purge)
        self._sweeper.daemon = True
        self._sweeper.start()
