"""
AWS Cognito JWT verification.

Fetches the Cognito User Pool JWKS, caches it for one hour, and uses it to
verify incoming ID tokens. Only RS256 tokens with ``token_use == "id"`` are
accepted.
"""

import json
import time
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

# Clock-skew tolerance (seconds) for JWT iat/nbf/exp validation. Absorbs normal
# drift between this host's clock and Cognito's; larger drift should be fixed by
# syncing the host clock (NTP), not by widening this.
_CLOCK_SKEW_LEEWAY_SECONDS = 60


class _JWKSCache:
    """In-process JWKS cache keyed by ``kid``."""

    _TTL = 3600.0  # seconds

    def __init__(self) -> None:
        self._keys: dict[str, dict] = {}
        self._fetched_at: float = 0.0

    def is_stale(self) -> bool:
        return time.monotonic() - self._fetched_at > self._TTL

    def update(self, jwks: dict[str, Any]) -> None:
        self._keys = {k["kid"]: k for k in jwks["keys"]}
        self._fetched_at = time.monotonic()

    def get_key(self, kid: str) -> dict | None:
        return self._keys.get(kid)


class CognitoVerifier:
    """
    Verifies Cognito ID tokens.

    A single instance should be reused across requests (the JWKS cache is
    instance-level). Create via ``app/auth/dependencies.py::get_verifier``.
    """

    def __init__(self, jwks_url: str, issuer: str, client_id: str) -> None:
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.client_id = client_id
        self._cache = _JWKSCache()

    async def _refresh_jwks(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(self.jwks_url, timeout=10.0)
            resp.raise_for_status()
            self._cache.update(resp.json())

    async def verify(self, token: str) -> dict[str, Any]:
        """
        Verify *token* and return its decoded claims.

        Raises ``jwt.PyJWTError`` (or a subclass) on any verification failure:
        expired signature, unknown key, wrong issuer, wrong ``token_use``, etc.
        """
        header = jwt.get_unverified_header(token)
        kid: str = header.get("kid", "")

        if self._cache.is_stale() or self._cache.get_key(kid) is None:
            await self._refresh_jwks()

        jwk = self._cache.get_key(kid)
        if jwk is None:
            raise jwt.InvalidTokenError(f"No matching key found for kid={kid!r}")

        public_key = RSAAlgorithm.from_jwk(json.dumps(jwk))

        claims: dict = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            audience=self.client_id,
            # Tolerate small clock skew between this host and Cognito's issuer
            # clock. Without leeway, a host running even a second or two behind
            # rejects freshly-issued tokens with ImmatureSignatureError
            # ("The token is not yet valid (iat)"). This relaxes only the iat/
            # nbf/exp timing checks; signature, issuer, and audience are still
            # fully enforced. Standard OIDC-client practice.
            leeway=_CLOCK_SKEW_LEEWAY_SECONDS,
        )

        if claims.get("iss") != self.issuer:
            raise jwt.InvalidIssuerError(
                f"Token issuer {claims.get('iss')!r} does not match expected {self.issuer!r}"
            )

        if claims.get("token_use") != "id":
            raise jwt.InvalidTokenError(
                f"Expected token_use='id', got {claims.get('token_use')!r}. "
                "Send the Cognito ID token, not the access token."
            )

        return claims
