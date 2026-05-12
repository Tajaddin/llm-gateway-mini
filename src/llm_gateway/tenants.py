"""Tenant model + in-memory registry."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field


@dataclass
class Tenant:
    """One isolated billing / rate-limit unit."""

    tenant_id: str
    api_key: str
    hmac_secret: str
    # Rate-limit knobs
    rpm: int = 60          # requests per minute (token-bucket capacity)
    rps: float = 1.0       # refill rate per second
    # Quota knobs
    daily_token_budget: int = 100_000
    monthly_cost_cents: int = 10_000

    @staticmethod
    def new(
        tenant_id: str,
        *,
        rpm: int = 60,
        rps: float = 1.0,
        daily_token_budget: int = 100_000,
        monthly_cost_cents: int = 10_000,
    ) -> "Tenant":
        return Tenant(
            tenant_id=tenant_id,
            api_key="sk_" + secrets.token_urlsafe(24),
            hmac_secret=secrets.token_urlsafe(32),
            rpm=rpm,
            rps=rps,
            daily_token_budget=daily_token_budget,
            monthly_cost_cents=monthly_cost_cents,
        )


class TenantStore:
    """In-memory registry keyed by ``tenant_id`` AND ``api_key``."""

    def __init__(self) -> None:
        self._by_id: dict[str, Tenant] = {}
        self._by_key: dict[str, Tenant] = {}

    def add(self, tenant: Tenant) -> None:
        self._by_id[tenant.tenant_id] = tenant
        self._by_key[tenant.api_key] = tenant

    def get_by_id(self, tenant_id: str) -> Tenant | None:
        return self._by_id.get(tenant_id)

    def get_by_key(self, api_key: str) -> Tenant | None:
        return self._by_key.get(api_key)

    def all(self) -> list[Tenant]:
        return list(self._by_id.values())

    def __len__(self) -> int:
        return len(self._by_id)
