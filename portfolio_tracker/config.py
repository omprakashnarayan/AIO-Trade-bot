from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os

APP_DIR = Path(os.environ.get("PORTFOLIO_TRACKER_HOME", Path.home() / ".portfolio_tracker")).expanduser()
DB_PATH = APP_DIR / "portfolio.sqlite3"
SETTINGS_PATH = APP_DIR / "settings.json"
REPORTS_DIR = APP_DIR / "reports"

DEFAULT_SETTINGS = {
    "owner_name": "",
    "risk_profile": "balanced",
    "monthly_contribution": "",
    "reminder_time": "08:00",
    "notes": "",
    # Trading controls are operator-editable in the UI and persisted in settings.json.
    "trading_mode": "paper",
    "max_order_notional": "",
    "max_position_weight": "",
    "scheduler_enabled": False,
    "default_account_id": "operator",
    "default_portfolio_id": "Main",
    "default_currency": "INR",
    "provider_modes": {"kite": "mcp_only", "upstox": "mcp_only", "indmoney": "mcp_only"},
}

# Verified official hosted MCP endpoints. Provider reads never use REST/API when
# the mode is mcp_only; authorization is performed by the provider in a browser.
OFFICIAL_MCP_ENDPOINTS = {
    "kite": {"url": "https://mcp.kite.trade/mcp", "read_only": True},
    "upstox": {"url": "https://mcp.upstox.com/mcp", "read_only": True},
    "indmoney": {"url": "https://mcp.indmoney.com/mcp", "read_only": True},
}



def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def load_settings() -> dict:
    ensure_dirs()
    if SETTINGS_PATH.exists():
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_PATH.read_text())}
    SETTINGS_PATH.write_text(json.dumps(DEFAULT_SETTINGS, indent=2))
    return dict(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    ensure_dirs()
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2, sort_keys=True))


def app_home() -> Path:
    ensure_dirs()
    return APP_DIR
