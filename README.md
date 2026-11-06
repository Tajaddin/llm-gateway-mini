# llm-gateway-mini

> Multi-tenant LLM proxy in ~800 lines: per-tenant API keys, HMAC request signing, token-bucket rate limiting, daily-token + monthly-cost quotas, append-only SQLite audit log. Concurrent benchmark: **10 tenants × 200 requests, 276 QPS, p99 = 69.2 ms** end-to-end through the full middleware stack, 0 errors, 2000 audit rows written.

[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE) [![Tests](https://img.shields.io/badge/tests-54%20passing-brightgreen)](#tests) [![Python](https://img.shields.io/badge/python-3.10%2B-blue)]()

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
  "wall_clock_seconds": 7.246,
  "total_requests": 2000,
  "ok_count": 2000,
  "rate_limited_429": 0,
  "server_errors_5xx": 0,
  "qps": 276.0,
  "latency_ms": {"p50": 34.45, "p95": 53.84, "p99": 69.22, "p99_9": 102.18, "max": 108.84, "mean": 36.11},
  "audit_rows": 2000
}
```

Median (by QPS) of three runs on a Windows 11 desktop with Python 3.12, 2026-11-06; the three runs gave 269–290 QPS and p99 69–71 ms. An earlier README reported 349.7 QPS and p99 41.73 ms from a different machine. The code before the November 2026 quota, pricing and signing fixes measured 259–298 QPS (p99 60–97 ms) on this one, so the added checks do not show up beyond run-to-run noise.

Each of those 2000 requests passes through: API-key lookup → capped body read → HMAC verify + replay check → rate-limit bucket acquire → model allowlist → quota reservation → backend call → quota settle → audit-log write. With a 2 ms mock backend, the **gateway's own overhead is the remaining ~32 ms at p50, ~67 ms at p99**, dominated by SQLite audit writes and Pydantic body validation.

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
| `X-Nonce` | fresh random value per request, 16-128 chars of `[A-Za-z0-9_-]` |
| `X-Signature` | `v1=<hex_hmac_sha256(secret, "{ts}.{nonce}." + raw_body)>` |
| `Content-Type` | `application/json` |

Response status codes:

| Code | When |
|---|---|
| 200 | Backend completed; usage recorded; audit row `status=ok` |
| 400 | Body failed Pydantic validation, or `model` is not in the gateway's price table |
| 401 | Unknown API key (checked before the body is read) OR signature invalid OR timestamp out of skew tolerance OR replay of an already-accepted signed request |
| 402 | Daily-token or monthly-cost quota would be exceeded by this request |
| 413 | Body longer than `max_body_bytes` (1 MiB by default); reading stops at the limit |
| 429 | Token bucket empty for this tenant |
| 502 | Backend raised |

Every code (even rejections) writes one row to `audit` with a named `error_kind`: `unknown_tenant`, `bad_signature`, `rate_limited`, `quota_daily_tokens`, `quota_monthly_cost`, `backend_error`, `bad_request`, `unknown_model`, `body_too_large`.

## Quickstart

```bash
pip install -e ".[anthropic,dev]"

# server side — prints the demo tenant's api_key + hmac_secret
llm-gateway --backend anthropic --port 8000

# in another shell — sign a request and POST it
python - <<'PY'
import json, time, hmac, hashlib, requests, secrets
API_KEY = "sk_..."  # from the gateway's startup output
SECRET  = "..."     # ditto
body = json.dumps({"model": "claude-haiku-4-5-20251001",
                   "messages": [{"role": "user", "content": "say hi"}],
                   "max_tokens": 50}).encode()
ts = int(time.time())
nonce = secrets.token_urlsafe(16)
sig = hmac.new(SECRET.encode(), f"{ts}.{nonce}.".encode() + body, hashlib.sha256).hexdigest()
r = requests.post(
    "http://127.0.0.1:8000/v1/completions",
    data=body,
    headers={"Authorization": f"Bearer {API_KEY}",
             "X-Timestamp": str(ts), "X-Nonce": nonce, "X-Signature": "v1="+sig,
             "Content-Type": "application/json"},
)
print(r.status_code, r.json())
PY
```

Programmatic embed:

```python
from llm_gateway import (
    AuditLog, HmacSigner, MockBackend, ModelPrice, Tenant, TenantStore, create_app,
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
    # The price table is also the allowlist: other models get 400.
    prices={"claude-haiku-4-5-20251001": ModelPrice("1", "5")},  # USD per million tokens
    signer=HmacSigner(max_skew_seconds=300),
    require_signature=True,
)
uvicorn.run(app, host="0.0.0.0", port=8000)
```

## Components, one paragraph each

* **`tenants.py`** — `Tenant` dataclass with `tenant_id`, `api_key`, `hmac_secret`, and per-tenant knobs (`rpm`, `rps`, `daily_token_budget`, `monthly_cost_cents`). `TenantStore` is a double-indexed in-memory registry (by id + by key). Swap for Redis/DB in production by implementing the two `get_by_*` methods.

* **`rate_limit.py`** — `TokenBucket(capacity, refill_per_second)`. `acquire(n)` non-blocking, returns True/False; `acquire_or_raise` raises `RateLimitExceeded`. Refill computed lazily from `time.monotonic()` deltas, no background thread.

* **`quota.py`** — `QuotaEnforcer.reserve(...)` checks an estimate against the tenant's `daily_token_budget` (UTC day-aligned) and `monthly_cost_cents` (UTC month-aligned) and holds it in the same locked step, raising `QuotaExceeded(kind=...)` if it does not fit. Concurrent requests therefore see each other's holds. `settle(...)` swaps the hold for the real usage once the backend replies; `release(...)` drops it if the call failed. Counters reset at the day/month boundary on next access — no scheduler required.

* **`signing.py`** — `HmacSigner.sign(secret, body, timestamp)` returns `{X-Timestamp, X-Nonce, X-Signature}`. `verify(...)` checks HMAC-SHA256 against `f"{ts}.{nonce}.".encode() + body` and rejects if `|now - ts| > max_skew_seconds` (default 300). Uses `hmac.compare_digest` (constant time). Every accepted signature is remembered until its timestamp leaves the skew window, so replaying a captured request inside the window gets 401; the per-request nonce keeps two identical bodies sent in the same second distinct. Entries expire with the window, so the cache holds at most one window of accepted requests. Sign every attempt afresh: a retry that resends the same headers after a 429 or 502 counts as a replay.

* **`audit.py`** — SQLite in WAL mode. Wide row: `received_at_ms`, `completed_at_ms`, `latency_ms`, `tenant_id`, `model`, `status`, `error_kind`, `input_tokens`, `output_tokens`, `cost_cents`, `request_id`. Indexed by `(tenant_id, received_at_ms)` and `status`.

* **`backends.py`** — `LLMBackend` protocol = one async `complete(LLMRequest) -> LLMResponse`. `MockBackend` (deterministic for tests + bench), `AnthropicBackend` (wraps `AsyncAnthropic.messages.stream` and collects the final message, with an explicit `timeout` of 60 s per read and `max_retries=2`, so a stalled upstream fails fast while long completions still finish). Backends report token counts; they do not price them.

* **`pricing.py`** — `ModelPrice(input_usd_per_mtok, output_usd_per_mtok)` as `Decimal`, so real list prices ($1 / $5 per million tokens, or $0.10) and the cost of a single token are exact. The table passed to `create_app(prices=...)` is the model allowlist: a request for any other model gets 400 before it reaches the backend, and each request is billed at its own model's price. `ANTHROPIC_PRICES` holds three Anthropic list prices for the CLI; check them against the pricing page before billing with them.

* **`gateway.py`** — FastAPI app. `POST /v1/completions` runs the six concerns in order, writes one audit row per request (success or failure), returns JSON. Health check at `/healthz`.

## Tests

```bash
pytest -v
```

```
test_rate_limit.py    7 passed   bucket: empty/full/refill/exceptions/bad config
test_quota.py         8 passed   daily/monthly budgets, named exceptions, tenant isolation,
                                 reserve/settle/release
test_signing.py      12 passed   roundtrip, wrong secret, body tamper, expired ts, missing/bad headers,
                                 replay rejected, fresh nonces accepted, nonce signed, cache expiry
test_audit.py         3 passed   write/count/by-tenant/by-status/latencies
test_tenants.py       3 passed   store lookup, fresh keys per Tenant.new
test_pricing.py       2 passed   sub-cent prices exact, float prices read as written
test_backends.py      2 passed   Anthropic client streams with explicit timeout + retries
test_gateway.py      17 passed   happy path, 401 unknown key, 401 bad sig, 429 rate limit,
                                 402 quota, signature-disabled flag, tenant isolation,
                                 400 malformed body, /healthz, concurrent requests within budget,
                                 prompt tokens counted, reservation released on 502,
                                 400 unknown model, per-model cost, 401 before body read,
                                 413 body cap, 401 replay
─────────────────────────────────
54 passed in 1.86s
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
│   ├── quota.py          # QuotaEnforcer (daily tokens + monthly cost, reserve/settle)
│   ├── pricing.py        # ModelPrice + ANTHROPIC_PRICES (the model allowlist)
│   ├── signing.py        # HmacSigner (sign + verify with ts skew check + replay cache)
│   ├── audit.py          # AuditLog (SQLite WAL)
│   ├── backends.py       # MockBackend + AnthropicBackend behind LLMBackend protocol
│   ├── gateway.py        # FastAPI app + POST /v1/completions
│   └── cli.py            # `llm-gateway` server
├── tests/                # 54 pytest cases across 8 files
└── bench/run_benchmark.py
```

## Limitations

**In-memory state.** TenantStore, token buckets, quota counters and the replay cache all live in process. A multi-replica deployment needs Redis (or similar) for these four. The interfaces are small and explicitly shaped for a swap.

**Synchronous SQLite audit.** With 2000 requests/run the audit DB is the dominant tail-latency contributor. Production deployments should buffer writes and flush in batches (or push to Kafka).

**Token-bucket only.** No leaky-bucket / sliding-window. Token bucket is the right primitive for "small burst OK, sustained spam not OK"; if you need stricter sustained smoothing, sliding-window is a 30-line addition.

**Quota reservations are estimates.** Each request reserves a deliberately high estimate — one token per character of the JSON-serialized messages, plus `max_tokens` — before the backend call, and settles to the real token count afterwards. A request whose estimate does not fit the remaining budget is rejected even if its real usage would have fit. That's intentional — better to reject early than to bill a customer past their plan. The prompt estimate is a heuristic, not a tokenizer: if a prompt ever tokenizes to more than its character count, that request can overshoot the budget by the difference.

**No request streaming.** This endpoint returns the full response in one shot. For SSE streaming, the same middleware stack composes onto an EventSourceResponse — see [stream-llm-fastapi](https://github.com/Tajaddin/stream-llm-fastapi) for the SSE half of this pair.

## License

MIT — see [LICENSE](LICENSE).
