"""End-to-end gateway tests via httpx.ASGITransport."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from llm_gateway import (
    AuditLog,
    HmacSigner,
    MockBackend,
    Tenant,
    TenantStore,
    create_app,
)


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
    app = create_app(tenants, backend, audit, signer=signer, require_signature=False)
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
