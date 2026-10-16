"""Synthetic HTTP failures must remain private on the actual gateway MCP wire."""
import json

import httpx
import pytest

from tests.test_manual_regressions import real_client, wire
from vigilo_connector import server


@pytest.mark.parametrize('status', [404, 503])
def test_http_status_error_scrubs_signed_url_decoded_from_path(wire, monkeypatch, status):
    path = '/api/https%3A%2F%2Fsynthetic.blob.core.windows.net%2Ffile%3Fsig%3Dsynthetic-secret'
    request = httpx.Request('GET', 'https://app.example.test' + path)
    assert 'https://synthetic.blob.core.windows.net/file?sig=synthetic-secret' in request.url.path

    class BrokenClient:
        def get_children(self, **kwargs):
            httpx.Response(status, request=request).raise_for_status()

    monkeypatch.setattr(server, '_client', BrokenClient())
    result = wire('list_children', {})
    assert result.get('isError') is True
    output = json.dumps(result).lower()
    assert f'http {status}' in output
    assert 'synthetic-secret' not in output
    assert 'blob.core.windows.net' not in output
