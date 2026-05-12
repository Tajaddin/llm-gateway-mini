"""TenantStore unit tests."""

from __future__ import annotations

from llm_gateway import Tenant, TenantStore


def test_lookup_by_id_and_key():
    s = TenantStore()
    t = Tenant.new("t1")
    s.add(t)
    assert s.get_by_id("t1") is t
    assert s.get_by_key(t.api_key) is t
    assert s.get_by_id("missing") is None
    assert s.get_by_key("sk_bogus") is None


def test_len_and_all():
    s = TenantStore()
    s.add(Tenant.new("a"))
    s.add(Tenant.new("b"))
    assert len(s) == 2
    assert {t.tenant_id for t in s.all()} == {"a", "b"}


def test_new_creates_fresh_keys():
    a = Tenant.new("x")
    b = Tenant.new("x")
    assert a.api_key != b.api_key
    assert a.hmac_secret != b.hmac_secret
