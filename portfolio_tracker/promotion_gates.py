"""Durable Phase 6 promotion-gate records; gates never enable live execution."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

GATE_DEFINITIONS = (
    ("paper_soak", "Paper soak"),
    ("sandbox_test", "Sandbox test"),
    ("provider_reconciliation", "Provider reconciliation"),
    ("fault_injection", "Fault injection"),
    ("kill_switch", "Kill switch"),
    ("duplicate_order", "Duplicate-order protection"),
    ("token_expiry", "Token expiry"),
    ("partial_fill", "Partial fill"),
    ("human_approval", "Human approval"),
    ("separate_credentials", "Separate credentials"),
    ("per_order_approval", "Per-order approval"),
    ("manual_recovery", "Manual recovery"),
)
GATE_STATUSES = frozenset({"unknown", "pass", "fail"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS promotion_gates (
  gate_key TEXT PRIMARY KEY, label TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('unknown','pass','fail')),
  evidence TEXT NOT NULL DEFAULT '', reviewer TEXT NOT NULL DEFAULT '', reviewed_at TEXT,
  updated_at TEXT NOT NULL
);
"""

class PromotionGates:
    def __init__(self, db):
        self.db = db
        self.ensure()

    def ensure(self):
        with self.db.connect() as conn:
            conn.executescript(_SCHEMA)
            now = datetime.now(timezone.utc).isoformat()
            for key, label in GATE_DEFINITIONS:
                conn.execute("INSERT OR IGNORE INTO promotion_gates(gate_key,label,status,updated_at) VALUES(?,?,?,?)", (key, label, "unknown", now))

    def list(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM promotion_gates ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]

    def record(self, gate_key: str, *, status: str, evidence: str = "", reviewer: str = "", reviewed_at: str | None = None, actor: str = "operator") -> dict[str, Any]:
        if gate_key not in dict(GATE_DEFINITIONS):
            raise ValueError("unknown promotion gate")
        if status not in GATE_STATUSES:
            raise ValueError("status must be unknown, pass, or fail")
        timestamp = reviewed_at or datetime.now(timezone.utc).isoformat()
        with self.db.connect() as conn:
            conn.execute("UPDATE promotion_gates SET status=?, evidence=?, reviewer=?, reviewed_at=?, updated_at=? WHERE gate_key=?", (status, str(evidence)[:10000], str(reviewer)[:256], timestamp, timestamp, gate_key))
        self.db.append_audit_event(event_type="promotion_gate.recorded", entity_type="promotion_gate", entity_id=gate_key, payload={"status": status, "evidence": str(evidence)[:10000], "reviewer": str(reviewer)[:256], "actor": actor}, account_id=actor)
        return next(item for item in self.list() if item["gate_key"] == gate_key)

    def status(self) -> dict[str, Any]:
        gates = self.list()
        passed = sum(g["status"] == "pass" for g in gates)
        return {"ready": len(gates) == len(GATE_DEFINITIONS) and passed == len(gates), "passed": passed, "total": len(gates), "gates": gates}

    def live_allowed(self) -> bool:
        return bool(self.status()["ready"])
