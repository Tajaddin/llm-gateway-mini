"""Append-only SQLite audit log.

Every completed (or failed) request gets one row. The schema is wide on
purpose: tenants + auditors typically want to filter by tenant, model,
status, and a time range without joining other tables.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at_ms  INTEGER NOT NULL,
    completed_at_ms INTEGER NOT NULL,
    latency_ms      INTEGER NOT NULL,
    tenant_id       TEXT NOT NULL,
    model           TEXT NOT NULL,
    status          TEXT NOT NULL,
    error_kind      TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_cents      INTEGER NOT NULL DEFAULT 0,
    request_id      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_tenant_time ON audit (tenant_id, received_at_ms);
CREATE INDEX IF NOT EXISTS audit_status ON audit (status);
"""


@dataclass
class AuditRecord:
    received_at_ms: int
    completed_at_ms: int
    tenant_id: str
    model: str
    status: str
    request_id: str
    error_kind: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_cents: int = 0

    @property
    def latency_ms(self) -> int:
        return max(0, self.completed_at_ms - self.received_at_ms)


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def write(self, record: AuditRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO audit (received_at_ms, completed_at_ms, latency_ms, tenant_id,
                               model, status, error_kind, input_tokens, output_tokens,
                               cost_cents, request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.received_at_ms,
                record.completed_at_ms,
                record.latency_ms,
                record.tenant_id,
                record.model,
                record.status,
                record.error_kind,
                record.input_tokens,
                record.output_tokens,
                record.cost_cents,
                record.request_id,
            ),
        )

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]

    def count_by_tenant(self, tenant_id: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM audit WHERE tenant_id=?", (tenant_id,)
        ).fetchone()[0]

    def count_by_status(self, status: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM audit WHERE status=?", (status,)
        ).fetchone()[0]

    def latencies_for(self, tenant_id: str) -> list[int]:
        rows = self._conn.execute(
            "SELECT latency_ms FROM audit WHERE tenant_id=?", (tenant_id,)
        ).fetchall()
        return [r[0] for r in rows]
