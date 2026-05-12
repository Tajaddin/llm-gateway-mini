"""``llm-gateway`` CLI — boot the server with a Mock or Anthropic backend."""

from __future__ import annotations

import argparse
import os

import uvicorn

from llm_gateway.audit import AuditLog
from llm_gateway.backends import AnthropicBackend, MockBackend
from llm_gateway.gateway import create_app
from llm_gateway.tenants import Tenant, TenantStore


def cli() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["mock", "anthropic"], default="mock")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--audit-db", default="audit.sqlite")
    ap.add_argument("--no-signature", action="store_true")
    args = ap.parse_args()

    tenants = TenantStore()
    demo = Tenant.new("demo")
    tenants.add(demo)
    print(f"demo tenant_id: {demo.tenant_id}")
    print(f"demo api_key:   {demo.api_key}")
    print(f"demo hmac:      {demo.hmac_secret}")

    if args.backend == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY not set")
        backend = AnthropicBackend()
    else:
        backend = MockBackend(latency_ms=10, jitter_ms=5)

    audit = AuditLog(args.audit_db)
    app = create_app(tenants, backend, audit, require_signature=not args.no_signature)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    cli()
