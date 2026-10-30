"""Bearer-token auth for the HTTP transport, and signed viewer links.

Browsers cannot attach a bearer header to a link the reviewer clicks, so viewer and crop URLs carry
an HMAC of the document hash (derived from MCP_BEARER_TOKEN) instead of the token itself.
"""

from __future__ import annotations

import hashlib
import hmac

from .config import settings


def sign(sha: str) -> str | None:
    tok = settings().mcp_bearer_token
    if not tok:
        return None
    return hmac.new(tok.encode(), f"view:{sha}".encode(), hashlib.sha256).hexdigest()[:24]


def check_signature(sha: str, sig: str | None) -> bool:
    expected = sign(sha)
    if expected is None:
        return True
    return bool(sig) and hmac.compare_digest(expected, sig)


def check_bearer(header: str | None) -> bool:
    tok = settings().mcp_bearer_token
    if not tok:
        return True
    if not header or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), tok.encode())
