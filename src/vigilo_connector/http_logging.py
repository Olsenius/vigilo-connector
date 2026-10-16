"""Valfri HTTP-metadata til stderr. Innhald og identifikatorar blir aldri logga."""

import json
import logging
import os
import re
import sys
import threading
import time
import uuid

import httpx

_OUTPUT_LOCK = threading.Lock()
_EXTENSION = 'vigilo_http_log'
_STATIC_ROUTES = frozenset({
    '/connect/token', '/connect/authorize', '/api/children', '/api/children/my',
    '/api/current-user', '/api/messages', '/api/messages/contact-list',
    '/api/message-threads', '/api/absences', '/api/student-absences',
    '/api/absencecodes', '/api/news-feed', '/api/consent-forms', '/api/school-years',
})
_DYNAMIC_ROUTES = (
    (r'/api/messages/threads/[^/]+', '/api/messages/threads/{threadId}'),
    (r'/api/messages/[^/]+/reply', '/api/messages/{threadId}/reply'),
    (r'/api/message-threads/[^/]+/participants', '/api/message-threads/{threadId}/participants'),
    (r'/api/students/[^/]+/lessons', '/api/students/{childId}/lessons'),
    (r'/api/scheduling-events/[^/]+/student', '/api/scheduling-events/{childId}/student'),
    (r'/api/posts/[^/]+/attachments/[^/]+', '/api/posts/{postId}/attachments/{attachmentId}'),
    (r'/api/posts/[^/]+', '/api/posts/{postId}'),
)


def _route(request: httpx.Request, origin: str) -> tuple[str, str]:
    path = request.url.path
    if 'filestorage' in path.casefold():
        return 'attachment', '/filestorage/{redacted}'
    if origin == 'attachment':
        return origin, '/{attachment}'
    if path in _STATIC_ROUTES:
        return origin, path
    for pattern, template in _DYNAMIC_ROUTES:
        if re.fullmatch(pattern, path):
            return origin, template
    return origin, '/{redacted}'


def _emit(record: dict) -> None:
    """Eigen stderr-utgang verkar òg i stdio, utan logging.basicConfig."""
    if os.environ.get('VIGILO_HTTP_LOG') != '1':
        return
    with _OUTPUT_LOCK:
        sys.stderr.write(json.dumps(record, separators=(',', ':'), ensure_ascii=True) + '\n')
        sys.stderr.flush()


def _request_log(request: httpx.Request, origin: str) -> None:
    try:
        if os.environ.get('VIGILO_HTTP_LOG') != '1':
            return
        category, route = _route(request, origin)
        method = request.method if request.method in {'GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS'} else 'OTHER'
        metadata = {'callId': uuid.uuid4().hex, 'method': method, 'origin': category, 'route': route}
        request.extensions[_EXTENSION] = (metadata, time.monotonic())
        _emit({'event': 'request', **metadata})
    except Exception:
        # Logging kan aldri endre utfallet av API-kallet.
        pass


def _finish_log(request: httpx.Request, *, status=None, error=None) -> None:
    try:
        state = request.extensions.get(_EXTENSION)
        if state is None:
            return
        metadata, started = state
        if error is not None:
            if request.extensions.get('vigilo_http_error_logged'):
                return
            request.extensions['vigilo_http_error_logged'] = True
        record = {'event': 'error' if error is not None else 'response', **metadata,
                  'status': status, 'elapsedMs': round(max(0, time.monotonic() - started) * 1000, 3)}
        if error is not None:
            safe_classes = {'ConnectTimeout', 'ReadTimeout', 'WriteTimeout', 'PoolTimeout',
                            'ConnectError', 'ReadError', 'WriteError', 'CloseError',
                            'RemoteProtocolError', 'LocalProtocolError', 'ProxyError',
                            'UnsupportedProtocol', 'DecodingError', 'TooManyRedirects'}
            name = type(error).__name__
            record['errorClass'] = name if name in safe_classes else 'TransportError'
        _emit(record)
    except Exception:
        pass


class _LoggedStream(httpx.SyncByteStream):
    """Observer sein transportfeil utan å lese eller buffre straumen."""

    def __init__(self, stream, request):
        self._stream = stream
        self._request = request

    def __iter__(self):
        try:
            yield from self._stream
        except httpx.TransportError as error:
            _finish_log(self._request, error=error)
            raise

    def close(self):
        self._stream.close()


def _response_log(response):
    _finish_log(response.request, status=response.status_code)
    try:
        if response.request.extensions.get(_EXTENSION) and isinstance(response.stream, httpx.SyncByteStream):
            response.stream = _LoggedStream(response.stream, response.request)
    except Exception:
        pass


class LoggedClient(httpx.Client):
    """Vanleg sync-httpx med metadatahooks og fangst av transportfeil."""

    def __init__(self, *args, origin: str, **kwargs):
        if origin not in {'app', 'web', 'oauth', 'attachment'}:
            raise ValueError('Ukjend HTTP-loggkategori.')
        if os.environ.get('VIGILO_HTTP_LOG') == '1':
            # httpx INFO og httpcore DEBUG kan elles skrive rå, signerte URL-ar.
            # Eigen metadatautgang er komplett og treng ikkje desse loggane.
            try:
                names = {'httpx', 'httpcore', *list(logging.Logger.manager.loggerDict)}
                for name in names:
                    if name in {'httpx', 'httpcore'} or name.startswith(('httpx.', 'httpcore.')):
                        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
            except Exception:
                pass
        hooks = {key: list(value) for key, value in (kwargs.pop('event_hooks', None) or {}).items()}
        hooks.setdefault('request', []).append(lambda request: _request_log(request, origin))
        hooks.setdefault('response', []).append(_response_log)
        super().__init__(*args, event_hooks=hooks, **kwargs)

    def send(self, request, **kwargs):
        try:
            return super().send(request, **kwargs)
        except httpx.RequestError as error:
            try:
                failed_request = error.request
            except RuntimeError:
                failed_request = request
            _finish_log(failed_request, error=error)
            raise


def logged_get(url, *, origin: str, timeout=5.0, follow_redirects=False, **kwargs):
    """Einskild GET med same standardar som httpx.get og utan global klient."""
    with LoggedClient(origin=origin, timeout=timeout, follow_redirects=follow_redirects) as client:
        return client.get(url, **kwargs)
