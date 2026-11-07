"""HmacSigner unit tests."""

from __future__ import annotations

import time

import pytest

from llm_gateway import HmacSigner, SignatureInvalid


def test_sign_verify_roundtrip():
    s = HmacSigner()
    headers = s.sign("secret", b'{"hi":1}', timestamp=1700000000)
    s.verify(
        "secret",
        b'{"hi":1}',
        timestamp_header=headers["X-Timestamp"],
        signature_header=headers["X-Signature"],
        nonce_header=headers["X-Nonce"],
        now=1700000010,
    )


def test_wrong_secret_rejected():
    s = HmacSigner()
    headers = s.sign("secret-a", b"hi", timestamp=1700000000)
    with pytest.raises(SignatureInvalid):
        s.verify(
            "secret-b",
            b"hi",
            timestamp_header=headers["X-Timestamp"],
            signature_header=headers["X-Signature"],
            nonce_header=headers["X-Nonce"],
            now=1700000010,
        )


def test_body_tamper_rejected():
    s = HmacSigner()
    headers = s.sign("secret", b'{"hi":1}', timestamp=1700000000)
    with pytest.raises(SignatureInvalid):
        s.verify(
            "secret",
            b'{"hi":2}',  # changed body
            timestamp_header=headers["X-Timestamp"],
            signature_header=headers["X-Signature"],
            nonce_header=headers["X-Nonce"],
            now=1700000010,
        )


def test_expired_timestamp_rejected():
    s = HmacSigner(max_skew_seconds=60)
    headers = s.sign("secret", b"hi", timestamp=1700000000)
    with pytest.raises(SignatureInvalid):
        s.verify(
            "secret",
            b"hi",
            timestamp_header=headers["X-Timestamp"],
            signature_header=headers["X-Signature"],
            nonce_header=headers["X-Nonce"],
            now=1700001000,  # 1000s later
        )


def test_missing_headers_rejected():
    s = HmacSigner()
    with pytest.raises(SignatureInvalid):
        s.verify("secret", b"hi", timestamp_header=None, signature_header="v1=abc")
    with pytest.raises(SignatureInvalid):
        s.verify("secret", b"hi", timestamp_header="1700000000", signature_header=None)


def test_bad_signature_scheme_rejected():
    s = HmacSigner()
    with pytest.raises(SignatureInvalid):
        s.verify(
            "secret",
            b"hi",
            timestamp_header="1700000000",
            signature_header="v2=anything",
            now=1700000001,
        )


def test_non_integer_timestamp_rejected():
    s = HmacSigner()
    with pytest.raises(SignatureInvalid):
        s.verify(
            "secret",
            b"hi",
            timestamp_header="not-a-number",
            signature_header="v1=abc",
        )


def _verify(s, headers, body=b"hi", now=1700000010):
    s.verify(
        "secret",
        body,
        timestamp_header=headers["X-Timestamp"],
        signature_header=headers["X-Signature"],
        nonce_header=headers["X-Nonce"],
        now=now,
    )


def test_replayed_request_rejected():
    s = HmacSigner()
    headers = s.sign("secret", b"hi", timestamp=1700000000)
    _verify(s, headers)
    with pytest.raises(SignatureInvalid, match="replay"):
        _verify(s, headers)


def test_identical_requests_with_fresh_nonces_accepted():
    s = HmacSigner()
    first = s.sign("secret", b"hi", timestamp=1700000000)
    second = s.sign("secret", b"hi", timestamp=1700000000)
    _verify(s, first)
    _verify(s, second)


def test_nonce_is_covered_by_the_signature():
    s = HmacSigner()
    headers = s.sign("secret", b"hi", timestamp=1700000000)
    headers["X-Nonce"] = "a" * 22
    with pytest.raises(SignatureInvalid, match="HMAC mismatch"):
        _verify(s, headers)


def test_missing_or_malformed_nonce_rejected():
    s = HmacSigner()
    headers = s.sign("secret", b"hi", timestamp=1700000000)
    for nonce in (None, "", "short", "has.a.dot.in.it.0123456789"):
        with pytest.raises(SignatureInvalid):
            _verify(s, {**headers, "X-Nonce": nonce})


def test_replay_cache_drops_entries_once_their_timestamp_expires():
    s = HmacSigner(max_skew_seconds=60)
    for _ in range(3):
        _verify(s, s.sign("secret", b"hi", timestamp=1700000000), now=1700000000)
    _verify(s, s.sign("secret", b"hi", timestamp=1700000100), now=1700000100)
    assert len(s._seen) == 1
