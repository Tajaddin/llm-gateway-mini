"""FastAPI gateway endpoint that ties everything together.

Request lifecycle for ``POST /v1/completions``:

1. Read API key from ``Authorization: Bearer <api_key>``. 401 if unknown.
2. Verify HMAC signature against the raw body + ``X-Timestamp``. 401 on fail.
3. Acquire one token from the tenant's rate-limit bucket. 429 on empty.
4. Pre-flight quota check (estimated tokens + cost). 402 on exceeded.
5. Forward to the LLM backend, await response.
6. Record real usage in the quota counters and write an audit row.
7. Return the response body.

Failures at any step write an audit row with a named ``error_kind``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from llm_gateway.audit import AuditLog, AuditRecord
from llm_gateway.backends import LLMBackend, LLMRequest
from llm_gateway.quota import QuotaEnforcer, QuotaExceeded
from llm_gateway.rate_limit import RateLimitExceeded, TokenBucket
from llm_gateway.signing import HmacSigner, SignatureInvalid
from llm_gateway.tenants import Tenant, TenantStore


class GatewayError(Exception):
    pass


class _CompletionRequest(BaseModel):
    model: str = Field(..., min_length=1)
    messages: list[dict] = Field(..., min_length=1)
    max_tokens: int = Field(1024, gt=0, le=32_000)


def create_app(
    tenants: TenantStore,
    backend: LLMBackend,
    audit_log: AuditLog,
    *,
    quota: QuotaEnforcer | None = None,
    signer: HmacSigner | None = None,
    require_signature: bool = True,
    rate_limit_cost_per_request: float = 1.0,
) -> FastAPI:
    quota = quota or QuotaEnforcer()
    signer = signer or HmacSigner()

    app = FastAPI(title="llm-gateway-mini")
    app.state.tenants = tenants
    app.state.audit = audit_log
    app.state.quota = quota
    app.state.signer = signer

    # one bucket per tenant_id (lazy create)
    buckets: dict[str, TokenBucket] = {}

    def _bucket_for(t: Tenant) -> TokenBucket:
        b = buckets.get(t.tenant_id)
        if b is None:
            b = TokenBucket(capacity=t.rpm, refill_per_second=t.rps)
            buckets[t.tenant_id] = b
        return b

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "tenants": len(tenants),
            "audit_rows": audit_log.count(),
        }

    @app.post("/v1/completions")
    async def completions(request: Request):
        received_at_ms = int(time.time() * 1000)
        request_id = secrets.token_urlsafe(12)
        body = await request.body()

        auth = request.headers.get("authorization", "")
        api_key = auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""
        tenant = tenants.get_by_key(api_key) if api_key else None

        def _audit(status: str, error_kind: str | None, model: str, in_t=0, out_t=0, cost=0):
            audit_log.write(
                AuditRecord(
                    received_at_ms=received_at_ms,
                    completed_at_ms=int(time.time() * 1000),
                    tenant_id=tenant.tenant_id if tenant else "anonymous",
                    model=model,
                    status=status,
                    error_kind=error_kind,
                    input_tokens=in_t,
                    output_tokens=out_t,
                    cost_cents=cost,
                    request_id=request_id,
                )
            )

        if tenant is None:
            _audit("denied", "unknown_tenant", model="-")
            raise HTTPException(status_code=401, detail="unknown api key")

        if require_signature:
            try:
                signer.verify(
                    tenant.hmac_secret,
                    body,
                    timestamp_header=request.headers.get("x-timestamp"),
                    signature_header=request.headers.get("x-signature"),
                )
            except SignatureInvalid as exc:
                _audit("denied", "bad_signature", model="-")
                raise HTTPException(status_code=401, detail=str(exc))

        bucket = _bucket_for(tenant)
        try:
            bucket.acquire_or_raise(rate_limit_cost_per_request)
        except RateLimitExceeded as exc:
            _audit("denied", "rate_limited", model="-")
            raise HTTPException(status_code=429, detail=str(exc))

        try:
            parsed = _CompletionRequest.model_validate(json.loads(body))
        except Exception as exc:  # noqa: BLE001
            _audit("denied", "bad_request", model="-")
            raise HTTPException(status_code=400, detail=str(exc))

        est_tokens = parsed.max_tokens
        est_cost = max(1, est_tokens // 1000 * 125)
        try:
            quota.check(tenant, requested_tokens=est_tokens, est_cost_cents=est_cost)
        except QuotaExceeded as exc:
            _audit("denied", f"quota_{exc.kind}", model=parsed.model)
            raise HTTPException(status_code=402, detail=str(exc))

        try:
            resp = await backend.complete(
                LLMRequest(
                    model=parsed.model,
                    messages=parsed.messages,
                    max_tokens=parsed.max_tokens,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _audit("error", "backend_error", model=parsed.model)
            raise HTTPException(status_code=502, detail=str(exc))

        quota.record(tenant, tokens=resp.input_tokens + resp.output_tokens, cost_cents=resp.cost_cents)
        _audit(
            "ok",
            error_kind=None,
            model=resp.model,
            in_t=resp.input_tokens,
            out_t=resp.output_tokens,
            cost=resp.cost_cents,
        )
        return JSONResponse(
            {
                "request_id": request_id,
                "text": resp.text,
                "model": resp.model,
                "usage": {
                    "input_tokens": resp.input_tokens,
                    "output_tokens": resp.output_tokens,
                    "cost_cents": resp.cost_cents,
                },
            }
        )

    return app
