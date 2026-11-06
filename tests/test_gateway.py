"""End-to-end gateway tests via httpx.ASGITransport."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import httpx
import pytest

from llm_gateway import (
    AuditLog,
    HmacSigner,
    MockBackend,
    ModelPrice,
    Tenant,
    TenantStore,
    create_app,
)

PRICES = {"m": ModelPrice("1", "5")}


def _setup(tmp_path, *, rpm=10, require_signature=True, daily_tokens=1_000_000):
    tenants = TenantStore()
    t = Tenant.new("t1", rpm=rpm, rps=max(1.0, rpm / 60.0), daily_token_budget=daily_tokens)
    tenants.add(t)
    backend = MockBackend(latency_ms=1)
    audit = AuditLog(tmp_path / "audit.sqlite")
    signer = HmacSigner()
    app = create_app(
        tenants,
        backend,
        audit,
        prices=PRICES,
        signer=signer,
        require_signature=require_signature,
    )
    return app, t, audit, signer, backend


def _headers(tenant, body, signer):
    h = {"Authorization": f"Bearer {tenant.api_key}", "Content-Type": "application/json"}
    h.update(signer.sign(tenant.hmac_secret, body))
    return h


def _body():
    return json.dumps({"model": "m", "messages": [{"role": "user", "content": "hi"}]}).encode()


@pytest.mark.asyncio
async def test_happy_path_returns_200_and_writes_audit(tmp_path):
    app, t, audit, signer, backend = _setup(tmp_path)
    body = _body()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=_headers(t, body, signer))
    assert r.status_code == 200, r.text
    j = r.json()
    assert "request_id" in j and "text" in j and "usage" in j
    assert audit.count_by_tenant("t1") == 1
    assert audit.count_by_status("ok") == 1
    assert backend.calls == 1


@pytest.mark.asyncio
async def test_unknown_api_key_returns_401_and_logs(tmp_path):
    app, t, audit, signer, _ = _setup(tmp_path)
    body = _body()
    headers = {"Authorization": "Bearer sk_bogus", "Content-Type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=headers)
    assert r.status_code == 401
    assert audit.count_by_status("denied") == 1


@pytest.mark.asyncio
async def test_bad_signature_returns_401_and_logs(tmp_path):
    app, t, audit, signer, _ = _setup(tmp_path)
    body = _body()
    h = {"Authorization": f"Bearer {t.api_key}", "Content-Type": "application/json"}
    h["X-Timestamp"] = str(int(time.time()))
    h["X-Signature"] = "v1=deadbeef"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=h)
    assert r.status_code == 401
    assert audit.count_by_status("denied") == 1


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_burst(tmp_path):
    app, t, audit, signer, _ = _setup(tmp_path, rpm=2)
    body = _body()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/v1/completions", content=body, headers=_headers(t, body, signer))
        r2 = await c.post("/v1/completions", content=body, headers=_headers(t, body, signer))
        r3 = await c.post("/v1/completions", content=body, headers=_headers(t, body, signer))
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert audit.count_by_status("denied") >= 1


@pytest.mark.asyncio
async def test_quota_exceeded_returns_402(tmp_path):
    app, t, audit, signer, _ = _setup(tmp_path, daily_tokens=10)
    body = _body()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=_headers(t, body, signer))
    # max_tokens defaults to 1024 which is > 10 daily budget.
    assert r.status_code == 402
    assert audit.count_by_status("denied") == 1


@pytest.mark.asyncio
async def test_signature_can_be_disabled(tmp_path):
    app, t, audit, signer, _ = _setup(tmp_path, require_signature=False)
    body = _body()
    headers = {"Authorization": f"Bearer {t.api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=headers)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_isolation_between_tenants(tmp_path):
    tenants = TenantStore()
    a = Tenant.new("a", rpm=1, rps=0.01)
    b = Tenant.new("b", rpm=1, rps=0.01)
    tenants.add(a)
    tenants.add(b)
    backend = MockBackend()
    audit = AuditLog(tmp_path / "audit.sqlite")
    signer = HmacSigner()
    app = create_app(
        tenants, backend, audit, prices=PRICES, signer=signer, require_signature=False
    )
    body = _body()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        ra1 = await c.post(
            "/v1/completions",
            content=body,
            headers={"Authorization": f"Bearer {a.api_key}", "Content-Type": "application/json"},
        )
        ra2 = await c.post(
            "/v1/completions",
            content=body,
            headers={"Authorization": f"Bearer {a.api_key}", "Content-Type": "application/json"},
        )
        rb1 = await c.post(
            "/v1/completions",
            content=body,
            headers={"Authorization": f"Bearer {b.api_key}", "Content-Type": "application/json"},
        )
    # Tenant a exhausted: ra2 is 429.
    assert ra1.status_code == 200
    assert ra2.status_code == 429
    # Tenant b unaffected.
    assert rb1.status_code == 200


@pytest.mark.asyncio
async def test_malformed_body_returns_400(tmp_path):
    app, t, audit, signer, _ = _setup(tmp_path, require_signature=False)
    body = b"not json"
    headers = {"Authorization": f"Bearer {t.api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=headers)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_healthz(tmp_path):
    app, t, audit, _, _ = _setup(tmp_path, require_signature=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["tenants"] == 1


def _unsigned_app(tmp_path, tenant, backend, prices=PRICES):
    tenants = TenantStore()
    tenants.add(tenant)
    audit = AuditLog(tmp_path / "audit.sqlite")
    return create_app(tenants, backend, audit, prices=prices, require_signature=False), audit


def _auth(tenant):
    return {"Authorization": f"Bearer {tenant.api_key}", "Content-Type": "application/json"}


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_overspend_the_daily_budget(tmp_path):
    t = Tenant.new("t1", rpm=100, rps=100, daily_token_budget=2_000)
    app, _ = _unsigned_app(tmp_path, t, MockBackend(latency_ms=50))
    body = json.dumps(
        {"model": "m", "messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 100}
    ).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        rs = await asyncio.gather(
            *[c.post("/v1/completions", content=body, headers=_auth(t)) for _ in range(20)]
        )
    codes = [r.status_code for r in rs]
    assert 200 in codes and 402 in codes
    assert app.state.quota.snapshot("t1")["tokens_today"] <= t.daily_token_budget


@pytest.mark.asyncio
async def test_quota_counts_prompt_tokens_not_just_max_tokens(tmp_path):
    t = Tenant.new("t1", daily_token_budget=1_000)
    backend = MockBackend()
    app, audit = _unsigned_app(tmp_path, t, backend)
    body = json.dumps(
        {"model": "m", "messages": [{"role": "user", "content": "x" * 8_000}], "max_tokens": 10}
    ).encode()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=_auth(t))
    assert r.status_code == 402
    assert backend.calls == 0


class _FailingBackend:
    async def complete(self, req):
        raise RuntimeError("upstream down")


@pytest.mark.asyncio
async def test_backend_error_releases_the_reservation(tmp_path):
    t = Tenant.new("t1")
    app, _ = _unsigned_app(tmp_path, t, _FailingBackend())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=_body(), headers=_auth(t))
    assert r.status_code == 502
    assert app.state.quota.snapshot("t1")["tokens_today"] == 0


@pytest.mark.asyncio
async def test_model_outside_the_price_table_is_rejected(tmp_path):
    t = Tenant.new("t1")
    backend = MockBackend()
    app, audit = _unsigned_app(tmp_path, t, backend)
    body = json.dumps({"model": "some-expensive-model", "messages": [{"role": "user", "content": "hi"}]})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/v1/completions", content=body, headers=_auth(t))
    assert r.status_code == 400
    assert backend.calls == 0
    assert audit.count_by_status("denied") == 1


@pytest.mark.asyncio
async def test_cost_follows_the_requested_models_price(tmp_path):
    t = Tenant.new("t1")
    prices = {"cheap": ModelPrice("1", "5"), "pricey": ModelPrice("15", "75")}
    app, _ = _unsigned_app(tmp_path, t, MockBackend(), prices=prices)
    costs = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        for model in prices:
            # MockBackend: 4,000 chars -> 1,000 input tokens; output capped at max_tokens.
            body = json.dumps(
                {"model": model, "messages": [{"role": "user", "content": "x" * 4_000}], "max_tokens": 1_000}
            )
            r = await c.post("/v1/completions", content=body, headers=_auth(t))
            assert r.status_code == 200, r.text
            costs[model] = r.json()["usage"]["cost_cents"]
    assert costs == {"cheap": 0.6, "pricey": 9.0}
    assert app.state.quota.snapshot("t1")["cost_cents_this_month"] == Decimal("9.6")


async def _raw_post(app, headers, chunks):
    """Drive the ASGI app directly; returns the status and the body messages
    the app actually pulled from receive()."""
    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    pulled = []

    async def receive():
        if pending:
            pulled.append(pending.pop(0))
            return pulled[-1]
        return {"type": "http.disconnect"}

    status = {}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/completions",
        "raw_path": b"/v1/completions",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return status["code"], pulled


@pytest.mark.asyncio
async def test_unknown_key_is_rejected_without_reading_the_body(tmp_path):
    app, *_ = _setup(tmp_path)
    code, pulled = await _raw_post(app, {"Authorization": "Bearer sk_bogus"}, [b"x" * 1000] * 5)
    assert code == 401
    assert pulled == []


@pytest.mark.asyncio
async def test_body_over_the_cap_is_rejected_while_streaming(tmp_path):
    t = Tenant.new("t1")
    tenants = TenantStore()
    tenants.add(t)
    app = create_app(
        tenants,
        MockBackend(),
        AuditLog(tmp_path / "audit.sqlite"),
        prices=PRICES,
        require_signature=False,
        max_body_bytes=1024,
    )
    code, pulled = await _raw_post(app, _auth(t), [b"x" * 1000] * 5)
    assert code == 413
    assert len(pulled) == 2  # stopped at the first chunk past the cap

    declared = {**_auth(t), "Content-Length": "5000"}
    code, pulled = await _raw_post(app, declared, [b"x" * 1000] * 5)
    assert code == 413
    assert pulled == []


@pytest.mark.asyncio
async def test_replayed_signed_request_is_rejected(tmp_path):
    app, t, audit, signer, backend = _setup(tmp_path)
    body = _body()
    headers = _headers(t, body, signer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        first = await c.post("/v1/completions", content=body, headers=headers)
        replay = await c.post("/v1/completions", content=body, headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 401
    assert backend.calls == 1
