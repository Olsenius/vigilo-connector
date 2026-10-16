"""Observerte HTTP-standarder fra Android APK 3.3.0-4 (OkHttp 4.10.0).

Gjelder app-API/OAuth, ikke web-portalen, browser-login eller eksterne vedlegg.
TLS-fingeravtrykk og Androids cache/retry-mekanisme etterlignes ikke.
"""

import json
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

from .http_logging import LoggedClient

ANDROID_USER_AGENT = "okhttp/4.10.0"
JSON_CONTENT_TYPE = "application/json; charset=UTF-8"
ANDROID_TIMEOUT = httpx.Timeout(10, connect=100, read=100)


class _NoCookies(DefaultCookiePolicy):
    """OkHttp-klienten bruker standard CookieJar.NO_COOKIES."""

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False


def is_filestorage(path: str | httpx.URL) -> bool:
    return "filestorage" in httpx.URL(path).path.casefold()


def android_client(
    base_url: str, *, app_api: bool = False, transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """HTTP/2 med HTTP/1.1-fallback, gzip og ingen persisterte cookies.

    Skrivekall overstyrer redirect-policy slik at en skrivehandling ikke spilles
    av på nytt via 307/308. Transporten har ingen automatiske retries.
    """
    def prepare(request: httpx.Request) -> None:
        if "accept-encoding" not in request.headers and "range" not in request.headers:
            request.headers["Accept-Encoding"] = "gzip"
        request.headers.pop("Cookie", None)
        if request.headers.get("content-type") == "application/json":
            request.headers["Content-Type"] = JSON_CONTENT_TYPE
        if app_api:
            if is_filestorage(request.url):
                request.headers.pop("Authorization", None)
                request.headers.pop("appVersion", None)
            else:
                # Lokal sesjonsmarkør i Android. Ingen userId-query på wire.
                request.url = request.url.copy_remove_param("userId")

    client = LoggedClient(
        origin="app" if app_api else "oauth",
        base_url=base_url, http2=True, follow_redirects=True,
        max_redirects=20, timeout=ANDROID_TIMEOUT,
        headers={"User-Agent": ANDROID_USER_AGENT},
        cookies=CookieJar(policy=_NoCookies()),
        event_hooks={"request": [prepare]}, transport=transport,
    )
    # BridgeInterceptor legger ikke til Accept, og gzip avhenger av Range.
    client.headers.pop("Accept", None)
    client.headers.pop("Accept-Encoding", None)
    return client


def android_json(payload: dict) -> bytes:
    """Kompakt UTF-8 med Gsons standard HTML- og linjeseparator-escaping."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for char in ("<", ">", "&", "=", "'", "\u2028", "\u2029"):
        text = text.replace(char, f"\\u{ord(char):04x}")
    return text.encode("utf-8")
