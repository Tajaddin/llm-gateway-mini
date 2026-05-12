"""AuditLog unit tests."""

from __future__ import annotations

import time

from llm_gateway import AuditLog, AuditRecord


def _rec(tenant_id="t1", status="ok", error_kind=None, in_t=10, out_t=20, cost=5):
    now = int(time.time() * 1000)
    return AuditRecord(
        received_at_ms=now - 50,
        completed_at_ms=now,
        tenant_id=tenant_id,
        model="m",
        status=status,
        error_kind=error_kind,
        input_tokens=in_t,
        output_tokens=out_t,
        cost_cents=cost,
        request_id="r123",
    )


def test_write_and_count(tmp_path):
    log = AuditLog(tmp_path / "a.sqlite")
    log.write(_rec())
    log.write(_rec())
    assert log.count() == 2


def test_count_by_tenant_and_status(tmp_path):
    log = AuditLog(tmp_path / "a.sqlite")
    log.write(_rec(tenant_id="t1", status="ok"))
    log.write(_rec(tenant_id="t1", status="denied", error_kind="rate_limited"))
    log.write(_rec(tenant_id="t2", status="ok"))
    assert log.count_by_tenant("t1") == 2
    assert log.count_by_tenant("t2") == 1
    assert log.count_by_status("ok") == 2
    assert log.count_by_status("denied") == 1


def test_latency_is_completed_minus_received(tmp_path):
    log = AuditLog(tmp_path / "a.sqlite")
    log.write(_rec(tenant_id="t1"))
    latencies = log.latencies_for("t1")
    assert latencies == [50]
