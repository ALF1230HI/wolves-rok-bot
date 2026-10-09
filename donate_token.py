"""Lightweight signed tokens for the /donate web-form link.

Dependency-free (hmac + base64 + json from the stdlib only) so it works
identically whether it's imported from the Discord bot (to generate a
personalized link for whoever ran /donate) or from the standalone web app
(to verify that link when they open it).

The token carries who the link is for (Discord user/guild/channel) so the
website can know exactly where to log the donation and where to post the
confirmation, without needing the visitor to log in to anything. It's
signed so nobody can forge a link pointing at a different server/channel,
and it expires after DEFAULT_MAX_AGE seconds.
"""

import base64
import hashlib
import hmac
import json
import os
import time

# In production this should be set via an env var (e.g. a long random
# string) so tokens can't be forged. Falls back to a dev-only default so
# this still works out of the box while testing locally.
SECRET = os.getenv("DONATE_LINK_SECRET", "dev-insecure-secret-change-me").encode()

DEFAULT_MAX_AGE = 30 * 60  # 30 minutes


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def generate_token(payload: dict) -> str:
    """Sign `payload` (must be JSON-serializable) and return a URL-safe token."""
    body = dict(payload)
    body["ts"] = int(time.time())
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(SECRET, raw, hashlib.sha256).digest()
    return f"{_b64e(raw)}.{_b64e(sig)}"


def verify_token(token: str, max_age: int = DEFAULT_MAX_AGE):
    """Return the original payload dict if `token` is valid and not expired,
    else None."""
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = _b64d(raw_b64)
        sig = _b64d(sig_b64)
        expected_sig = hmac.new(SECRET, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        payload = json.loads(raw)
        if time.time() - payload.get("ts", 0) > max_age:
            return None
        return payload
    except Exception:
        return None
