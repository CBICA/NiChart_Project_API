"""
PKCE (Proof Key for Code Exchange) and CSRF state utilities.

All values are generated with cryptographically secure randomness.
"""

import base64
import hashlib
import secrets


def generate_state() -> str:
    """Return a random opaque value for CSRF state validation."""
    return secrets.token_urlsafe(32)


def generate_pkce_pair() -> tuple[str, str]:
    """
    Return ``(code_verifier, code_challenge)`` for PKCE S256.

    ``code_verifier``  — sent to the token endpoint at callback time.
    ``code_challenge`` — sent to the authorization endpoint at login time.
    """
    verifier = secrets.token_urlsafe(64)  # 86-char base64url string, well above the 43-byte minimum
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
