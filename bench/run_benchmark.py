"""Multi-tenant gateway benchmark.

Spawns N tenants, each firing K concurrent requests, through the in-process
ASGI app with a MockBackend. Measures:

* p50 / p95 / p99 / p99.9 end-to-end latency per request
* effective QPS (total_requests / wall_clock)
* per-tenant pass / 4xx / 429 / 5xx counts from the audit log

The bench is wall-clock fair: every tenant starts at the same moment with
its own asyncio.Task running ``send_burst``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from llm_gateway import (
    AuditLog,
    HmacSigner,
    MockBackend,
    Tenant,
    TenantStore,
    create_app,
)


BENCH_DIR = Path(__file__).resolve().parent
RESULTS = BENCH_DIR / "results.json"


def _pct(arr, p):
    if not arr:
        return 0
    s = sorted(arr)
    idx = min(len(s) - 1, max(0, int(p * (len(s) - 1))))
    return s[idx]


async def _send_burst(client, t, signer, body, n, latencies, statuses):
    headers = {"Authorization": f"Bearer {t.api_key}", "Content-Type": "application/json"}
    for _ in range(n):
        sig = signer.sign(t.hmac_secret, body)
        h = {**headers, **sig}
        t0 = time.perf_counter()
        try:
            r = await client.post("/v1/completions", content=body, headers=h)
            latencies.append((time.perf_counter() - t0) * 1000.0)
            statuses.append(r.status_code)
        except Exception:
            latencies.append((time.perf_counter() - t0) * 1000.0)
            statuses.append(0)


async def run(args) -> dict:
    tenants = TenantStore()
    for i in range(args.tenants):
        # Generous limits — we want to saturate the gateway, not the rate limiter.
        tenants.add(Tenant.new(f"t{i}", rpm=args.requests + 100, rps=args.requests + 100,
                               daily_token_budget=10_000_000, monthly_cost_cents=10_000_000))

    backend = MockBackend(latency_ms=args.backend_latency_ms)
    audit_path = BENCH_DIR / "bench_audit.sqlite"
    if audit_path.exists():
        audit_path.unlink()
    audit = AuditLog(audit_path)
    signer = HmacSigner()
    app = create_app(tenants, backend, audit, signer=signer, require_signature=True)

    body = json.dumps(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    ).encode()

    latencies: list[float] = []
    statuses: list[int] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bench", timeout=30) as client:
        t0 = time.perf_counter()
        tasks = [
            _send_burst(client, t, signer, body, args.requests, latencies, statuses)
            for t in tenants.all()
        ]
        await asyncio.gather(*tasks)
        wall = time.perf_counter() - t0

    total = len(latencies)
    ok_count = sum(1 for s in statuses if s == 200)
    s_429 = sum(1 for s in statuses if s == 429)
    s_5xx = sum(1 for s in statuses if 500 <= s < 600)
    other = total - ok_count - s_429 - s_5xx

    out = {
        "config": vars(args),
        "wall_clock_seconds": round(wall, 3),
        "total_requests": total,
        "ok_count": ok_count,
        "rate_limited_429": s_429,
        "server_errors_5xx": s_5xx,
        "other": other,
        "qps": round(total / max(wall, 1e-9), 1),
        "latency_ms": {
            "p50": round(_pct(latencies, 0.50), 2),
            "p95": round(_pct(latencies, 0.95), 2),
            "p99": round(_pct(latencies, 0.99), 2),
            "p99_9": round(_pct(latencies, 0.999), 2),
            "max": round(max(latencies) if latencies else 0, 2),
            "mean": round(statistics.mean(latencies) if latencies else 0, 2),
        },
        "audit_rows": audit.count(),
    }
    audit.close()
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--tenants", type=int, default=10)
    ap.add_argument("--requests", type=int, default=200, help="requests per tenant")
    ap.add_argument("--backend-latency-ms", type=int, default=2)
    args = ap.parse_args()

    out = asyncio.run(run(args))
    RESULTS.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"\nresults written to {RESULTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
