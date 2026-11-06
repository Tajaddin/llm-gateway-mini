"""QuotaEnforcer unit tests."""

from __future__ import annotations

import pytest

from llm_gateway import QuotaEnforcer, QuotaExceeded, Tenant


def _tenant() -> Tenant:
    return Tenant.new("t1", daily_token_budget=1000, monthly_cost_cents=500)


def test_under_budget_passes():
    q = QuotaEnforcer()
    t = _tenant()
    q.check(t, requested_tokens=100, est_cost_cents=10)
    q.record(t, tokens=100, cost_cents=10)
    q.check(t, requested_tokens=100, est_cost_cents=10)


def test_over_daily_tokens_raises_named_kind():
    q = QuotaEnforcer()
    t = _tenant()
    q.record(t, tokens=950, cost_cents=10)
    with pytest.raises(QuotaExceeded) as exc:
        q.check(t, requested_tokens=100, est_cost_cents=10)
    assert exc.value.kind == "daily_tokens"


def test_over_monthly_cost_raises_named_kind():
    q = QuotaEnforcer()
    t = _tenant()
    q.record(t, tokens=10, cost_cents=480)
    with pytest.raises(QuotaExceeded) as exc:
        q.check(t, requested_tokens=10, est_cost_cents=50)
    assert exc.value.kind == "monthly_cost"


def test_snapshot_returns_zeros_for_unseen_tenant():
    q = QuotaEnforcer()
    snap = q.snapshot("never-seen")
    assert snap["tokens_today"] == 0
    assert snap["cost_cents_this_month"] == 0


def test_tenants_isolated():
    q = QuotaEnforcer()
    t1 = Tenant.new("a", daily_token_budget=100)
    t2 = Tenant.new("b", daily_token_budget=100)
    q.record(t1, tokens=99, cost_cents=0)
    q.check(t2, requested_tokens=99, est_cost_cents=0)  # must not raise


def test_reserve_holds_budget_until_settled():
    q = QuotaEnforcer()
    t = _tenant()
    q.reserve(t, tokens=600, cost_cents=10)
    with pytest.raises(QuotaExceeded) as exc:
        q.reserve(t, tokens=600, cost_cents=10)
    assert exc.value.kind == "daily_tokens"


def test_settle_replaces_the_estimate_with_real_usage():
    q = QuotaEnforcer()
    t = _tenant()
    r = q.reserve(t, tokens=600, cost_cents=100)
    q.settle(r, tokens=150, cost_cents=20)
    snap = q.snapshot("t1")
    assert snap["tokens_today"] == 150
    assert snap["cost_cents_this_month"] == 20


def test_release_returns_the_reservation():
    q = QuotaEnforcer()
    t = _tenant()
    r = q.reserve(t, tokens=600, cost_cents=100)
    q.release(r)
    assert q.snapshot("t1")["tokens_today"] == 0
    q.reserve(t, tokens=1000, cost_cents=500)  # whole budget is free again
