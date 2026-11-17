"""Evidence-bound, deterministic research and decision support.

This module intentionally has no network or model client. Callers must supply verified
facts and durable evidence; absent evidence is reported as unavailable.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

MODEL_VERSION = "deterministic-local/v1"
STRATEGY_VERSION = "evidence-gated/v1"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("captured_at must be timezone-aware")
    age_seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if age_seconds < 0:
        raise ValueError("captured_at is in the future")
    return int(age_seconds)


def content_hash(excerpt: str) -> str:
    return hashlib.sha256(excerpt.encode("utf-8")).hexdigest()


def add_evidence(db: Any, payload: dict[str, Any]) -> int:
    """Store evidence; only trusted internal callers may explicitly attest it."""
    excerpt = str(payload.get("excerpt", ""))
    if not str(payload.get("source", "")).strip() or not str(payload.get("source_identifier", payload.get("url", ""))).strip():
        raise ValueError("source and source_identifier are required")
    captured_at = str(payload.get("captured_at", ""))
    if not captured_at:
        raise ValueError("captured_at is required")
    _age_seconds(captured_at)
    verified = payload.get("verified", False)
    if not isinstance(verified, bool):
        raise ValueError("verified must be a boolean")
    expected_hash = content_hash(excerpt)
    if "content_hash" in payload and payload["content_hash"] != expected_hash:
        raise ValueError("content_hash does not match excerpt SHA-256")
    stored_excerpt = excerpt[:4000]
    return db.create_research_evidence(source=str(payload["source"]), source_identifier=str(payload.get("source_identifier", payload.get("url"))), captured_at=captured_at, provider=str(payload.get("provider", "manual")), excerpt=stored_excerpt, content_hash=content_hash(stored_excerpt), symbol=str(payload.get("symbol", "")), topic=str(payload.get("topic", "")), verified=verified)


def _validate_max_age(max_age_seconds: Any) -> int:
    if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int) or not 0 <= max_age_seconds <= 86400:
        raise ValueError("max_age_seconds must be an integer between 0 and 86400")
    return max_age_seconds


def _facts(db: Any, supplied: list[dict[str, Any]], topic: str, max_age_seconds: int) -> tuple[list[dict[str, Any]], list[str]]:
    verified, rejected = [], []
    stock_match = re.fullmatch(r"stock_analysis:([A-Z0-9][A-Z0-9.-]{0,19})", topic)
    stock_evidence_topics = {"market", "earnings", "news", "financials", "filings", "fundamentals"}
    for fact in supplied:
        evidence_id = fact.get("evidence_id")
        if not evidence_id:
            rejected.append("missing evidence_id")
            continue
        try:
            if isinstance(evidence_id, bool) or not isinstance(evidence_id, (int, str)):
                raise ValueError
            normalized_evidence_id = int(evidence_id)
            if normalized_evidence_id <= 0:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            rejected.append("invalid evidence_id")
            continue
        rows = db.list_research_evidence(limit=500)
        evidence = next((dict(row) for row in rows if int(row["id"]) == normalized_evidence_id), None)
        if not evidence or not evidence["verified"]:
            rejected.append(f"evidence {evidence_id} unavailable or unverified")
            continue
        evidence_symbol = str(evidence.get("symbol") or "").upper()
        evidence_topic = str(evidence.get("topic") or "")
        requested_symbol = str(fact.get("symbol") or "").upper()
        requested_topic = fact.get("topic")
        if requested_symbol and requested_symbol != evidence_symbol:
            rejected.append(f"evidence {evidence_id} symbol scope mismatch")
            continue
        if requested_topic is not None and str(requested_topic) != evidence_topic:
            rejected.append(f"evidence {evidence_id} topic scope mismatch")
            continue
        if stock_match:
            if evidence_symbol != stock_match.group(1):
                rejected.append(f"evidence {evidence_id} symbol scope mismatch")
                continue
            if evidence_topic not in stock_evidence_topics:
                rejected.append(f"evidence {evidence_id} topic is not allowed for stock analysis")
                continue
        elif evidence_topic != topic:
            rejected.append(f"evidence {evidence_id} topic scope mismatch")
            continue
        try:
            age = _age_seconds(str(evidence["captured_at"]))
        except ValueError:
            rejected.append(f"evidence {evidence_id} invalid timestamp")
            continue
        if age > max_age_seconds:
            rejected.append(f"evidence {evidence_id} stale")
            continue
        item = {"evidence_id": int(evidence_id), "evidence": evidence, "age_seconds": age,
                "symbol": evidence_symbol, "statement": str(evidence.get("excerpt") or "")}
        verified.append(item)
    return verified, rejected


def run_research(db: Any, *, topic: str, facts: list[dict[str, Any]] | None = None, max_age_seconds: int = 86400, model_version: str = MODEL_VERSION, strategy_version: str = STRATEGY_VERSION) -> dict[str, Any]:
    max_age_seconds = _validate_max_age(max_age_seconds)
    if facts is None:
        facts = []
    elif not isinstance(facts, list) or any(not isinstance(fact, dict) for fact in facts):
        raise ValueError("facts must be a list of objects")
    started = now()
    usable, rejected = _facts(db, facts, topic, max_age_seconds)
    findings: list[dict[str, Any]] = []
    if usable:
        for fact in usable[:50]:
            finding = {"finding_type": topic, "symbol": fact["symbol"], "statement": fact["statement"], "confidence": "medium", "risks": ["Supplied evidence may become stale."], "invalidation": ["Evidence exceeds freshness window or is corrected."], "evidence_ids": [fact["evidence_id"]], "model_version": model_version, "strategy_version": strategy_version}
            findings.append(finding)
        status, message = "succeeded", "Findings use only supplied verified evidence."
    else:
        status, message = "unavailable", "No current verified external evidence was supplied; no external call was made and no claim was fabricated."
    result = {"status": status, "message": message, "provider": "none", "external_call": False, "verified_fact_count": len(usable), "rejected_fact_count": len(rejected), "rejections": rejected, "findings": findings, "model_version": model_version, "strategy_version": strategy_version}
    run_id = db.create_research_run(topic=topic, status=status, provider="none", model_version=model_version, strategy_version=strategy_version, started_at=started, finished_at=now(), inputs={"facts": facts, "max_age_seconds": max_age_seconds}, result=result)
    for finding in findings:
        finding["id"] = db.create_research_finding(run_id=run_id, **finding)
    result["run_id"] = run_id
    return result


def portfolio_health(db: Any, *, portfolio: str | None = None) -> dict[str, Any]:
    holdings = [dict(row) for row in db.list_holdings(portfolio)]
    if not holdings:
        return {"status": "unavailable", "reason": "no holdings supplied", "evidence_ids": [], "external_call": False}
    total = sum(Decimal(str(row["shares"])) * Decimal(str(row["avg_cost"])) for row in holdings)
    return {"status": "available", "portfolio": portfolio or "all", "position_count": len(holdings), "cost_basis": str(total), "evidence_ids": [], "external_call": False, "message": "Deterministic health from local ledger facts; current market valuation is unavailable without fresh verified quotes."}


def concentration_risk(db: Any, *, portfolio: str | None = None) -> dict[str, Any]:
    holdings = [dict(row) for row in db.list_holdings(portfolio)]
    total = sum(Decimal(str(row["shares"])) * Decimal(str(row["avg_cost"])) for row in holdings)
    if total <= 0:
        return {"status": "unavailable", "reason": "no positive cost basis", "candidates": [], "external_call": False}
    weights = [{"symbol": row["symbol"], "weight": str((Decimal(str(row["shares"])) * Decimal(str(row["avg_cost"])) / total).quantize(Decimal("0.0001")))} for row in holdings]
    return {"status": "available", "basis": "cost_basis", "weights": sorted(weights, key=lambda x: x["weight"], reverse=True), "external_call": False, "message": "Concentration is based on supplied cost basis, not fabricated current quotes."}


def placeholder(topic: str) -> dict[str, Any]:
    return {"status": "unavailable", "topic": topic, "reason": "No verified provider evidence is configured or supplied; no external call was made.", "external_call": False, "evidence_ids": []}
