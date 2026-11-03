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
