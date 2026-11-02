"""Canonical idempotency keys for broker-scoped order deduplication."""

from __future__ import annotations

import hashlib


def canonical_idempotency_key(
    broker_id: str,
    account_id: str | None,
    portfolio_id: str | None,
    user_key: str,
) -> str:
    """Return the single storage key for a user idempotency key and its scope."""
    if not all(isinstance(value, str) for value in (broker_id, user_key)):
        raise TypeError("broker_id and user_key must be strings")
    if account_id is not None and not isinstance(account_id, str):
        raise TypeError("account_id must be a string or None")
    if portfolio_id is not None and not isinstance(portfolio_id, str):
        raise TypeError("portfolio_id must be a string or None")
    namespace = "\x1f".join((broker_id, account_id or "", portfolio_id or "", user_key))
    return "paper-" + hashlib.sha256(namespace.encode("utf-8")).hexdigest()
