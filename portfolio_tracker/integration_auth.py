from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .config import APP_DIR, ensure_dirs

PROVIDERS = ("kite", "upstox", "indmoney")
CAPABILITIES = {
    "kite": {"read": True, "status": True, "reconciliation": True, "live_writes": False},
    "upstox": {"read": True, "status": True, "reconciliation": True, "live_writes": False},
    "indmoney": {"read": True, "status": True, "reconciliation": False, "live_writes": False},
}


def storage_path() -> Path:
    return Path(os.environ.get("PORTFOLIO_INTEGRATIONS_PATH", str(APP_DIR / "integrations.enc"))).expanduser()


def _key(secret_key: str | bytes, salt: bytes) -> bytes:
    raw = secret_key.encode() if isinstance(secret_key, str) else secret_key
    derived = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000).derive(raw)
    return base64.urlsafe_b64encode(derived)


def _load(secret_key: str | bytes) -> dict[str, Any]:
    path = storage_path()
    if not path.exists():
        return {"providers": {}}
    try:
        envelope = json.loads(path.read_text())
        salt = base64.b64decode(envelope["salt"])
        return json.loads(Fernet(_key(secret_key, salt)).decrypt(envelope["ciphertext"]).decode())
    except (OSError, ValueError, KeyError, InvalidToken, json.JSONDecodeError):
        raise RuntimeError("integration secret storage could not be decrypted")


def _save(secret_key: str | bytes, data: dict[str, Any]) -> None:
    ensure_dirs()
    path = storage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    token = Fernet(_key(secret_key, salt)).encrypt(json.dumps(data, sort_keys=True).encode()).decode()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "salt": base64.b64encode(salt).decode(), "ciphertext": token}))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    tmp.replace(path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry(expires_in: Any) -> str | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def get_provider(secret_key: str | bytes, provider: str) -> dict[str, Any]:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    data = _load(secret_key).get("providers", {}).get(provider, {})
    return dict(data)


def save_config(secret_key: str | bytes, provider: str, values: dict[str, str]) -> None:
    if provider not in PROVIDERS:
        raise KeyError(provider)
    data = _load(secret_key)
    record = data.setdefault("providers", {}).setdefault(provider, {})
    for key, value in values.items():
        if value:
            record[key] = value.strip()
    _save(secret_key, data)


def disconnect(secret_key: str | bytes, provider: str) -> None:
    data = _load(secret_key)
    record = data.setdefault("providers", {}).setdefault(provider, {})
    for key in ("access_token", "refresh_token", "token_type", "expires_at", "last_authorized_at"):
        record.pop(key, None)
    _save(secret_key, data)


def status(secret_key: str | bytes, provider: str) -> dict[str, Any]:
    record = get_provider(secret_key, provider)
    connected = bool(record.get("access_token"))
    expires_at = record.get("expires_at")
    if expires_at:
        try:
            connected = connected and datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)
        except ValueError:
            connected = False
    configured = bool(record.get("api_key") or record.get("client_id"))
    return {
        "provider": provider,
        "configured": configured,
        "connected": connected,
        "status": "connected" if connected else ("configured" if configured else "unconfigured"),
        "last_authorized_at": record.get("last_authorized_at"),
        "expires_at": expires_at,
        "redirect_uri": record.get("redirect_uri"),
        "capabilities": CAPABILITIES[provider],
        "live_writes": False,
        "read_only": provider == "indmoney",
    }


def authorization_url(secret_key: str | bytes, provider: str, state: str) -> str:
    record = get_provider(secret_key, provider)
    if provider == "kite":
        return "https://kite.zerodha.com/connect/login?v=3&" + urllib.parse.urlencode({"api_key": record["api_key"]})
    if provider == "upstox":
        return "https://api.upstox.com/v2/login/authorization/dialog?" + urllib.parse.urlencode({"response_type": "code", "client_id": record["client_id"], "redirect_uri": record["redirect_uri"]}, safe="")
    raise ValueError("INDmoney uses an MCP-capable client and has no app callback here")


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())


def exchange_code(secret_key: str | bytes, provider: str, code: str) -> dict[str, Any]:
    record = get_provider(secret_key, provider)
    if provider == "kite":
        request_token = code
        checksum = hashlib.sha256((record["api_key"] + record["api_secret"] + request_token).encode()).hexdigest()
        payload = _post_form("https://api.kite.trade/session/token", {"api_key": record["api_key"], "request_token": request_token, "checksum": checksum})
    elif provider == "upstox":
        payload = _post_form("https://api.upstox.com/v2/login/authorization/token", {"code": code, "client_id": record["client_id"], "client_secret": record["client_secret"], "redirect_uri": record["redirect_uri"], "grant_type": "authorization_code"})
    else:
        raise ValueError("INDmoney does not support this OAuth callback")
    access_token = payload.get("access_token")
    if not access_token:
        raise ValueError("provider callback did not return an access token")
    data = _load(secret_key)
    saved = data.setdefault("providers", {}).setdefault(provider, {})
    for key in ("access_token", "refresh_token", "token_type"):
        if payload.get(key):
            saved[key] = payload[key]
    saved["last_authorized_at"] = _now()
    saved["expires_at"] = _expiry(payload.get("expires_in"))
    _save(secret_key, data)
    return status(secret_key, provider)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("[redacted]" if any(part in k.lower() for part in ("secret", "token", "password")) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
