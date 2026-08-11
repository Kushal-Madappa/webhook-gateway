"""Signature verification and admin auth — the trust boundary of the gateway.

Every function here uses `hmac.compare_digest` for the final secret comparison.
"""
from __future__ import annotations

import hashlib
import hmac

from app.config import settings

_SIG_SCHEME = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the *raw* request body, hex-encoded.

    Signing the raw bytes (not a re-serialized dict) is essential: any
    whitespace/key-order difference from re-encoding would change the digest and
    reject a legitimate webhook.
    """
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    """Return True iff `X-Signature: sha256=<hex>` matches the body HMAC."""
    if not header_value or not header_value.startswith(_SIG_SCHEME):
        return False
    provided = header_value[len(_SIG_SCHEME) :]
    expected = compute_signature(secret, body)
    # compare_digest, NOT ==. A normal string compare short-circuits on the first
    # differing byte, so its runtime leaks how many leading hex chars an attacker
    # guessed correctly — enough, over many requests, to forge a signature one
    # byte at a time. compare_digest runs in time independent of where the
    # mismatch is, closing that timing side-channel.
    return hmac.compare_digest(expected, provided)


def verify_admin_token(authorization: str | None) -> bool:
    """Return True iff the `Authorization: Bearer <token>` matches ADMIN_TOKEN."""
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        return False
    provided = authorization[len(prefix) :]
    # Same constant-time reasoning as above — the admin token is a secret too.
    return hmac.compare_digest(provided, settings.admin_token)
