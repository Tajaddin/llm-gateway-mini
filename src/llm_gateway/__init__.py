"""llm-gateway-mini — multi-tenant LLM proxy.

Exports:

* :class:`Tenant`, :class:`TenantStore` — tenant model + in-memory registry.
* :class:`TokenBucket` — per-tenant rate limiter.
* :class:`QuotaEnforcer` — daily-token + monthly-cost gates.
* :class:`HmacSigner` — request signing + verification.
* :class:`AuditLog` — append-only SQLite log of every request.
* :class:`MockBackend`, :class:`AnthropicBackend` — pluggable LLM providers.
* :func:`create_app` — FastAPI app factory.
"""

from llm_gateway.audit import AuditLog, AuditRecord
from llm_gateway.backends import (
    AnthropicBackend,
    LLMBackend,
    LLMRequest,
    LLMResponse,
    MockBackend,
)
from llm_gateway.gateway import GatewayError, create_app
from llm_gateway.quota import QuotaEnforcer, QuotaExceeded
from llm_gateway.rate_limit import RateLimitExceeded, TokenBucket
from llm_gateway.signing import HmacSigner, SignatureInvalid
from llm_gateway.tenants import Tenant, TenantStore

__all__ = [
    "AnthropicBackend",
    "AuditLog",
    "AuditRecord",
    "GatewayError",
    "HmacSigner",
    "LLMBackend",
    "LLMRequest",
    "LLMResponse",
    "MockBackend",
    "QuotaEnforcer",
    "QuotaExceeded",
    "RateLimitExceeded",
    "SignatureInvalid",
    "Tenant",
    "TenantStore",
    "TokenBucket",
    "create_app",
]
