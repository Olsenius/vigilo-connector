"""OAuth 2.1-autorisasjonsserver for gatewayen.

MCP-SDK-en står for endepunktene (/register, /authorize, /token, /revoke),
PKCE-sjekk og metadata. Her ligger lagringen og koblingen til passordsiden:
`authorize` parkerer forespørselen og sender nettleseren til /login, og når
passordet er godtatt fullfører `complete_authorization` flyten med en kode.
"""

import hmac

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .oauth_store import OAuthStore

SCOPE = "vigilo"
PENDING_TTL = 600
CODE_TTL = 300
API_TOKEN_CLIENT = "api-token"


class VigiloOAuthProvider:
    def __init__(self, store: OAuthStore, public_url: str, api_token: str | None):
        self._store = store
        self._public_url = public_url
        self._api_token = api_token

    # --- Klienter ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        raw = self._store.get_client(client_id)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._store.save_client(client_info.client_id, client_info.model_dump_json())

    # --- Authorize → /login → kode -------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req_id = self._store.create_pending(
            {
                "client_id": client.client_id,
                "client_name": client.client_name or client.client_id,
                "state": params.state,
                "scopes": params.scopes or [SCOPE],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
            ttl=PENDING_TTL,
        )
        return construct_redirect_uri(f"{self._public_url}/login", req=req_id)

    def pending_info(self, req_id: str) -> dict | None:
        """Det passordsiden viser: hvem som ber om tilgang, og hvor svaret sendes."""
        p = self._store.get_pending(req_id)
        if p is None:
            return None
        return {"client_name": p["client_name"], "redirect_uri": p["redirect_uri"]}

    def complete_authorization(self, req_id: str) -> str:
        """Kalles etter godkjent passord. Returnerer redirect-URL med code og state."""
        p = self._store.get_pending(req_id)
        if p is None:
            raise KeyError(req_id)
        self._store.delete_pending(req_id)
        code = self._store.create_code(
            {k: p[k] for k in (
                "client_id", "scopes", "code_challenge", "redirect_uri",
                "redirect_uri_provided_explicitly", "resource",
            )},
            ttl=CODE_TTL,
        )
        return construct_redirect_uri(p["redirect_uri"], code=code, state=p["state"])

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self._store.peek_code(authorization_code)
        if data is None or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=data["expires_at"],
            client_id=data["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=AnyUrl(data["redirect_uri"]),
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data["resource"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        data = self._store.take_code(authorization_code.code)
        if data is None or data["client_id"] != client.client_id:
            raise TokenError("invalid_grant", "Koden er ugyldig, utløpt eller allerede brukt.")
        access, refresh, expires_in = self._store.issue_tokens(
            client.client_id, data["scopes"], data["resource"]
        )
        return OAuthToken(
            access_token=access,
            expires_in=expires_in,
            scope=" ".join(data["scopes"]),
            refresh_token=refresh,
        )

    # --- Refresh ---------------------------------------------------------------

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        data = self._store.get_refresh(refresh_token)
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, **data)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        rotated = self._store.rotate_refresh(refresh_token.token)
        if rotated is None:
            raise TokenError("invalid_grant", "Refresh-tokenet er ugyldig eller allerede brukt.")
        access, refresh, expires_in = rotated
        return OAuthToken(
            access_token=access,
            expires_in=expires_in,
            scope=" ".join(refresh_token.scopes),
            refresh_token=refresh,
        )

    # --- Access-tokens -----------------------------------------------------------

    async def load_access_token(self, token: str) -> AccessToken | None:
        if self._api_token and hmac.compare_digest(token.encode(), self._api_token.encode()):
            return AccessToken(token=token, client_id=API_TOKEN_CLIENT, scopes=[SCOPE])
        data = self._store.get_access(token)
        return AccessToken(token=token, **data) if data else None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if token.client_id != API_TOKEN_CLIENT:
            self._store.revoke(token.token)
