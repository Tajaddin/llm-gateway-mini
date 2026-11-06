"""Daily-token + monthly-cost quotas.

The enforcer keeps two running counters per tenant:

* ``tokens_today`` — sum of (input + output) tokens for completed requests
  in the current UTC day. Reset at UTC midnight.
* ``cost_cents_this_month`` — sum of costs in the current UTC calendar
  month, reset at month boundary.

The gateway uses :meth:`QuotaEnforcer.reserve` before calling the backend:
the estimate is checked and added to the counters in one locked step, so
concurrent requests see each other's holds and cannot jointly pass a
budget. :meth:`QuotaEnforcer.settle` then swaps the estimate for the real
usage, or :meth:`QuotaEnforcer.release` drops it if the call failed.

Counters are in-memory; a real deployment persists them to Redis or DB.
"""

from __future__ import annotations

import datetime as _dt
import threading
from dataclasses import dataclass
from decimal import Decimal

from llm_gateway.tenants import Tenant


class QuotaExceeded(Exception):
    """Raised when a tenant is over its daily-token or monthly-cost budget."""

    def __init__(self, kind: str, used: int | Decimal, limit: int) -> None:
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(f"quota exceeded: kind={kind} used={used} limit={limit}")


@dataclass(frozen=True)
class Reservation:
    """Usage held against a tenant's budget for one in-flight request."""

    tenant_id: str
    day: str
    month: str
    tokens: int
    cost_cents: Decimal


@dataclass
class _Usage:
    day: str
    month: str
    tokens_today: int = 0
    cost_cents_this_month: Decimal = Decimal(0)


def _today_key() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")


def _month_key() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m")


class QuotaEnforcer:
    """In-memory quota state across all tenants."""

    def __init__(self) -> None:
        self._state: dict[str, _Usage] = {}
        self._lock = threading.Lock()

    def _slot(self, tenant_id: str) -> _Usage:
        day = _today_key()
        month = _month_key()
        u = self._state.get(tenant_id)
        if u is None:
            u = _Usage(day=day, month=month)
            self._state[tenant_id] = u
            return u
        if u.day != day:
            u.day = day
            u.tokens_today = 0
        if u.month != month:
            u.month = month
            u.cost_cents_this_month = 0
        return u

    def check(self, tenant: Tenant, requested_tokens: int, est_cost_cents: Decimal) -> None:
        """Raise :class:`QuotaExceeded` if the request would push past a limit."""
        with self._lock:
            self._check(self._slot(tenant.tenant_id), tenant, requested_tokens, est_cost_cents)

    def reserve(self, tenant: Tenant, tokens: int, cost_cents: Decimal) -> Reservation:
        """Check an estimate against the budget and hold it, atomically."""
        with self._lock:
            u = self._slot(tenant.tenant_id)
            self._check(u, tenant, tokens, cost_cents)
            u.tokens_today += tokens
            u.cost_cents_this_month += cost_cents
            return Reservation(tenant.tenant_id, u.day, u.month, tokens, cost_cents)

    def settle(self, reservation: Reservation, tokens: int, cost_cents: Decimal) -> None:
        """Replace a reservation's estimate with the usage actually incurred."""
        with self._lock:
            u = self._slot(reservation.tenant_id)
            # A day or month reset since the reservation already dropped it.
            held_tokens = reservation.tokens if u.day == reservation.day else 0
            held_cost = reservation.cost_cents if u.month == reservation.month else Decimal(0)
            u.tokens_today += tokens - held_tokens
            u.cost_cents_this_month += cost_cents - held_cost

    def release(self, reservation: Reservation) -> None:
        """Drop a reservation for a request that got no response."""
        self.settle(reservation, tokens=0, cost_cents=Decimal(0))

    @staticmethod
    def _check(u: _Usage, tenant: Tenant, requested_tokens: int, est_cost_cents: Decimal) -> None:
        if u.tokens_today + requested_tokens > tenant.daily_token_budget:
            raise QuotaExceeded(
                kind="daily_tokens",
                used=u.tokens_today,
                limit=tenant.daily_token_budget,
            )
        if u.cost_cents_this_month + est_cost_cents > tenant.monthly_cost_cents:
            raise QuotaExceeded(
                kind="monthly_cost",
                used=u.cost_cents_this_month,
                limit=tenant.monthly_cost_cents,
            )

    def record(self, tenant: Tenant, tokens: int, cost_cents: Decimal) -> None:
        with self._lock:
            u = self._slot(tenant.tenant_id)
            u.tokens_today += tokens
            u.cost_cents_this_month += cost_cents

    def snapshot(self, tenant_id: str) -> dict:
        u = self._slot(self._ensure_id_in_state(tenant_id))
        return {
            "tokens_today": u.tokens_today,
            "cost_cents_this_month": u.cost_cents_this_month,
            "day": u.day,
            "month": u.month,
        }

    def _ensure_id_in_state(self, tenant_id: str) -> str:
        # Make sure the slot exists so snapshot doesn't return zeros for an
        # unseen tenant.
        if tenant_id not in self._state:
            self._state[tenant_id] = _Usage(day=_today_key(), month=_month_key())
        return tenant_id
