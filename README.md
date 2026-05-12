# llm-gateway-mini

> Multi-tenant LLM proxy in ~600 lines: per-tenant API keys, HMAC request signing, token-bucket rate limiting, daily-token + monthly-cost quotas, append-only SQLite audit log. Concurrent benchmark: **10 tenants × 200 requests, 350 QPS, p99 = 41.7 ms** end-to-end through the full middleware stack, 0 errors, 2000 audit rows written.

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE) [![Tests](https://img.shields.io/badge/tests-34%20passing-brightgreen)](#tests) [![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()

## Why this exists

Every LLM-powered SaaS hits the same plumbing problem: you need to put a control plane between your users and the model API. That control plane has six concerns, and most teams reinvent each one badly:

1. **Auth** — who is calling? (Bearer key → tenant.)
2. **Integrity** — is this body the one they signed? (HMAC.)
3. **Pace** — should this caller wait? (Token bucket.)
4. **Spend** — is this caller still within their plan? (Daily-token + monthly-cost quotas.)
5. **Audit** — what happened? (Append-only log, structured for filtering.)
6. **Routing** — which backend? (Pluggable, here Mock + Anthropic.)

`llm-gateway-mini` ships all six behind one `POST /v1/completions`. Each concern is one module, individually unit-tested, and the gateway endpoint composes them in a single 100-line function so the contract is auditable.

## Hero benchmark

`python bench/run_benchmark.py --tenants 10 --requests 200 --backend-latency-ms 2`

```json
{
  "config": {"tenants": 10, "requests": 200, "backend_latency_ms": 2},
  "wall_clock_seconds": 5.72,
  "total_requests": 2000,
  "ok_count": 2000,
  "rate_limited_429": 0,
  "server_errors_5xx": 0,
  "qps": 349.7,
  "latency_ms": {"p50": 28.11, "p95": 38.02, "p99": 41.73, "p99_9": 46.13, "max": 47.45, "mean": 28.5},
  "audit_rows": 2000
}
```

Each of those 2000 requests passes through: API-key lookup → HMAC verify → rate-limit bucket acquire → quota pre-check → backend call → quota record → audit-log write. With a 2 ms mock backend, the **gateway's own overhead is the remaining ~26 ms at p50, ~40 ms at p99**, dominated by SQLite audit writes and Pydantic body validation.

Stress dimensions:

| Setting | Effect on benchmark |
|---|---|
| `--tenants` | Per-tenant token buckets isolate; doubling tenants ~doubles QPS until CPU saturates |
| `--requests` | Sequential within a tenant; total = tenants × requests |
| `--backend-latency-ms` | Adds to every p* number linearly |

## Endpoint contract

`POST /v1/completions`

```json
{
  "model": "claude-haiku-4-5-20251001",
  "messages": [{"role": "user", "content": "summarize..."}],
  "max_tokens": 512
}
```

Required request headers:

| Header | Value |
|---|---|
| `Authorization` | `Bearer <api_key>` |
| `X-Timestamp` | unix seconds at sign time |
| `X-Signature` | `v1=<hex_hmac_sha256(secret, "{ts}." + raw_body)>` |
| `Content-Type` | `application/json` |

Response status codes:

| Code | When |
|---|---|
| 200 | Backend completed; usage recorded; audit row `status=ok` |
| 400 | Body failed Pydantic validation |
| 401 | Unknown API key OR signature invalid OR timestamp out of skew tolerance |
| 402 | Daily-token or monthly-cost quota would be exceeded by this request |
| 429 | Token bucket empty for this tenant |
| 502 | Backend raised |

Every code (even rejections) writes one row to `audit` with a named `error_kind`: `unknown_tenant`, `bad_signature`, `rate_limited`, `quota_daily_tokens`, `quota_monthly_cost`, `backend_error`, `bad_request`.

## Quickstart

```bash
pip install -e ".[anthropic,dev]"

# server side — prints the demo tenant's api_key + hmac_secret
llm-gateway --backend anthropic --port 8000

# in another shell — sign a request and POST it
python - <<'PY'
import json, time, hmac, hashlib, requests, os
API_KEY = "sk_..."  # from the gateway's startup output
SECRET  = "..."     # ditto
body = json.dumps({"model": "claude-haiku-4-5-20251001",
                   "messages": [{"role": "user", "content": "say hi"}],
                   "max_tokens": 50}).encode()
ts = int(time.time())
sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
r = requests.post(
    "http://127.0.0.1:8000/v1/completions",
    data=body,
    headers={"Authorization": f"Bearer {API_KEY}",
             "X-Timestamp": str(ts), "X-Signature": "v1="+sig,
             "Content-Type": "application/json"},
)
print(r.status_code, r.json())
PY
```

Programmatic embed:

```python
from llm_gateway import (
    AuditLog, HmacSigner, MockBackend, Tenant, TenantStore, create_app,
)
import uvicorn

tenants = TenantStore()
demo = Tenant.new("demo", rpm=60, rps=1.0,
                  daily_token_budget=100_000, monthly_cost_cents=10_000)
tenants.add(demo)

app = create_app(
    tenants,
    MockBackend(latency_ms=10),
    AuditLog("audit.sqlite"),
    signer=HmacSigner(max_skew_seconds=300),
    require_signature=True,
)
uvicorn.run(app, host="0.0.0.0", port=8000)
```

## Components, one paragraph each

* **`tenants.py`** — `Tenant` dataclass with `tenant_id`, `api_key`, `hmac_secret`, and per-tenant knobs (`rpm`, `rps`, `daily_token_budget`, `monthly_cost_cents`). `TenantStore` is a double-indexed in-memory registry (by id + by key). Swap for Redis/DB in production by implementing the two `get_by_*` methods.

* **`rate_limit.py`** — `TokenBucket(capacity, refill_per_second)`. `acquire(n)` non-blocking, returns True/False; `acquire_or_raise` raises `RateLimitExceeded`. Refill computed lazily from `time.monotonic()` deltas, no background thread.

* **`quota.py`** — `QuotaEnforcer.check(...)` raises `QuotaExceeded(kind=...)` if the request would push the tenant past its `daily_token_budget` (UTC day-aligned) or `monthly_cost_cents` (UTC month-aligned). Counters reset at the day/month boundary on next access — no scheduler required.

* **`signing.py`** — `HmacSigner.sign(secret, body, timestamp)` returns `{X-Timestamp, X-Signature}`. `verify(...)` checks HMAC-SHA256 against `"{ts}.".encode() + body` and rejects if `|now - ts| > max_skew_seconds` (default 300). Uses `hmac.compare_digest` (constant time).

* **`audit.py`** — SQLite in WAL mode. Wide row: `received_at_ms`, `completed_at_ms`, `latency_ms`, `tenant_id`, `model`, `status`, `error_kind`, `input_tokens`, `output_tokens`, `cost_cents`, `request_id`. Indexed by `(tenant_id, received_at_ms)` and `status`.

* **`backends.py`** — `LLMBackend` protocol = one async `complete(LLMRequest) -> LLMResponse`. `MockBackend` (deterministic for tests + bench), `AnthropicBackend` (wraps `AsyncAnthropic.messages.create`). Cost computed from input/output tokens × per-1k-token cents.

* **`gateway.py`** — FastAPI app. `POST /v1/completions` runs the six concerns in order, writes one audit row per request (success or failure), returns JSON. Health check at `/healthz`.

## Tests

```bash
pytest -v
```

```
test_rate_limit.py    7 passed   bucket: empty/full/refill/exceptions/bad config
test_quota.py         5 passed   daily/monthly budgets, named exceptions, tenant isolation
test_signing.py       7 passed   roundtrip, wrong secret, body tamper, expired ts, missing/bad headers
test_audit.py         3 passed   write/count/by-tenant/by-status/latencies
test_tenants.py       3 passed   store lookup, fresh keys per Tenant.new
test_gateway.py       9 passed   happy path, 401 unknown key, 401 bad sig, 429 rate limit,
                                 402 quota, signature-disabled flag, tenant isolation,
                                 400 malformed body, /healthz
─────────────────────────────────
34 passed in 1.24s
```

Two tests worth calling out:

* **`test_isolation_between_tenants`** — two tenants share the gateway. Tenant A blows its bucket; tenant B's request must still succeed. Proves the per-tenant bucket dict isolates and that audit rows are correctly attributed.

* **`test_quota_exceeded_returns_402`** — daily-token budget set to 10, request asks for 1024 max_tokens. Pre-flight check rejects before any backend call, returns 402, writes `error_kind=quota_daily_tokens`. Pre-flight matters because *post*-flight rejection still costs you the token spend.

## Project layout

```
.
├── src/llm_gateway/
│   ├── tenants.py        # Tenant + TenantStore
│   ├── rate_limit.py     # TokenBucket
│   ├── quota.py          # QuotaEnforcer (daily tokens + monthly cost)
│   ├── signing.py        # HmacSigner (sign + verify with ts skew check)
│   ├── audit.py          # AuditLog (SQLite WAL)
│   ├── backends.py       # MockBackend + AnthropicBackend behind LLMBackend protocol
│   ├── gateway.py        # FastAPI app + POST /v1/completions
│   └── cli.py            # `llm-gateway` server
├── tests/                # 34 pytest cases across 6 files
└── bench/run_benchmark.py
```

## Limitations

**In-memory state.** TenantStore, token buckets, and quota counters all live in process. A multi-replica deployment needs Redis (or similar) for these three. The interfaces are small and explicitly shaped for a swap.

**Synchronous SQLite audit.** With 2000 requests/run the audit DB is the dominant tail-latency contributor. Production deployments should buffer writes and flush in batches (or push to Kafka).

**Token-bucket only.** No leaky-bucket / sliding-window. Token bucket is the right primitive for "small burst OK, sustained spam not OK"; if you need stricter sustained smoothing, sliding-window is a 30-line addition.

**Quota check is pre-flight only.** It uses `max_tokens` as the upper bound. A model that returns fewer tokens than requested is correctly billed at the real number in `record(...)`, but the *pre-check* rejects on the upper bound. That's intentional — better to reject early than to bill a customer past their plan.

**No request streaming.** This endpoint returns the full response in one shot. For SSE streaming, the same middleware stack composes onto an EventSourceResponse — see [stream-llm-fastapi](https://github.com/Tajaddin/stream-llm-fastapi) for the SSE half of this pair.

## License

MIT — see [LICENSE](LICENSE).
