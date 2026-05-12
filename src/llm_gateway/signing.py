"""HMAC-SHA256 request signing.

Clients sign requests with the tenant's ``hmac_secret``. The gateway
verifies the signature against the raw request body + a timestamp. A
narrow timestamp tolerance (default 5 minutes) blocks replay attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import time


class SignatureInvalid(Exception):
    """Raised when the HMAC check fails or the timestamp is out of tolerance."""


class HmacSigner:
    def __init__(self, max_skew_seconds: int = 300) -> None:
        self.max_skew_seconds = max_skew_seconds

    def sign(self, secret: str, body: bytes, *, timestamp: int | None = None) -> dict[str, str]:
        ts = int(timestamp if timestamp is not None else time.time())
        msg = f"{ts}.".encode() + body
        sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        return {"X-Timestamp": str(ts), "X-Signature": "v1=" + sig}

    def verify(
        self,
        secret: str,
        body: bytes,
        *,
        timestamp_header: str | None,
        signature_header: str | None,
        now: int | None = None,
    ) -> None:
        if not timestamp_header or not signature_header:
            raise SignatureInvalid("missing X-Timestamp or X-Signature header")
        try:
            ts = int(timestamp_header)
        except ValueError as exc:
            raise SignatureInvalid("X-Timestamp not an integer") from exc
        now_ts = int(now if now is not None else time.time())
        if abs(now_ts - ts) > self.max_skew_seconds:
            raise SignatureInvalid(
                f"timestamp skew {abs(now_ts - ts)}s exceeds tolerance {self.max_skew_seconds}s"
            )

        scheme, _, provided = signature_header.partition("=")
        if scheme != "v1" or not provided:
            raise SignatureInvalid("signature must be v1=<hex>")

        msg = f"{ts}.".encode() + body
        expected = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, provided):
            raise SignatureInvalid("HMAC mismatch")
