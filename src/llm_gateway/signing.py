"""HMAC-SHA256 request signing.

Clients sign requests with the tenant's ``hmac_secret`` over
``"{timestamp}.{nonce}." + raw_body``. The gateway rejects a timestamp
outside the tolerance (default 5 minutes) and remembers every accepted
signature until its timestamp leaves that window, so a captured request
cannot be replayed inside it either. The nonce lets a client send the
same body twice in one second as two distinct requests.
"""

from __future__ import annotations

import hashlib
import heapq
import hmac
import re
import secrets
import threading
import time

_NONCE = re.compile(r"[A-Za-z0-9_-]{16,128}")


class SignatureInvalid(Exception):
    """Raised when the HMAC check fails, the timestamp is out of tolerance,
    or the request was already accepted once."""


class HmacSigner:
    def __init__(self, max_skew_seconds: int = 300) -> None:
        self.max_skew_seconds = max_skew_seconds
        # Accepted signatures -> the time after which their timestamp is rejected anyway.
        self._seen: dict[str, int] = {}
        self._expiry_heap: list[tuple[int, str]] = []
        self._lock = threading.Lock()

    def sign(
        self,
        secret: str,
        body: bytes,
        *,
        timestamp: int | None = None,
        nonce: str | None = None,
    ) -> dict[str, str]:
        ts = int(timestamp if timestamp is not None else time.time())
        nonce = nonce or secrets.token_urlsafe(16)
        return {
            "X-Timestamp": str(ts),
            "X-Nonce": nonce,
            "X-Signature": "v1=" + _digest(secret, ts, nonce, body),
        }

    def verify(
        self,
        secret: str,
        body: bytes,
        *,
        timestamp_header: str | None,
        signature_header: str | None,
        nonce_header: str | None = None,
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
        if not nonce_header or not _NONCE.fullmatch(nonce_header):
            raise SignatureInvalid("X-Nonce must be 16-128 characters of [A-Za-z0-9_-]")

        expected = _digest(secret, ts, nonce_header, body)
        if not hmac.compare_digest(expected, provided):
            raise SignatureInvalid("HMAC mismatch")
        if not self._first_use(expected, ts + self.max_skew_seconds, now_ts):
            raise SignatureInvalid("replayed request: this signature was already accepted")

    def _first_use(self, signature: str, expires_at: int, now: int) -> bool:
        with self._lock:
            while self._expiry_heap and self._expiry_heap[0][0] < now:
                _, old = heapq.heappop(self._expiry_heap)
                del self._seen[old]
            if signature in self._seen:
                return False
            self._seen[signature] = expires_at
            heapq.heappush(self._expiry_heap, (expires_at, signature))
            return True


def _digest(secret: str, ts: int, nonce: str, body: bytes) -> str:
    msg = f"{ts}.{nonce}.".encode() + body
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
