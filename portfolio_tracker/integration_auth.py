"""Browser OAuth/PKCE support for hosted MCP providers.

All state and token material is encrypted at rest.  This module deliberately
contains no broker REST/API transport; OAuth is only used to authorize the MCP
HTTP transport.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .config import APP_DIR, OFFICIAL_MCP_ENDPOINTS, ensure_dirs

PROVIDERS = ("kite", "upstox", "indmoney")
CAPABILITIES = {name: {"read": True, "status": True, "reconciliation": True, "live_writes": False} for name in PROVIDERS}


def storage_path() -> Path:
    return Path(os.environ.get("PORTFOLIO_INTEGRATIONS_PATH", str(APP_DIR / "integrations.enc"))).expanduser()


def _key(secret_key: str | bytes, salt: bytes) -> bytes:
    raw = secret_key.encode() if isinstance(secret_key, str) else secret_key
    derived = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000).derive(raw)
    return base64.urlsafe_b64encode(derived)


def _load(secret_key: str | bytes) -> dict[str, Any]:
    path = storage_path()
    if not path.exists(): return {"providers": {}, "oauth": {}}
    try:
        envelope = json.loads(path.read_text())
        salt = base64.b64decode(envelope["salt"])
        return json.loads(Fernet(_key(secret_key, salt)).decrypt(envelope["ciphertext"]).decode())
    except (OSError, ValueError, KeyError, InvalidToken, json.JSONDecodeError):
        raise RuntimeError("integration secret storage could not be decrypted")


def _save(secret_key: str | bytes, data: dict[str, Any]) -> None:
    ensure_dirs(); path = storage_path(); path.parent.mkdir(parents=True, exist_ok=True)
    salt = secrets.token_bytes(16)
    token = Fernet(_key(secret_key, salt)).encrypt(json.dumps(data, sort_keys=True).encode()).decode()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": 2, "salt": base64.b64encode(salt).decode(), "ciphertext": token}))
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR); tmp.replace(path); os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _now() -> str: return datetime.now(timezone.utc).isoformat()


def _expiry(expires_in: Any) -> str | None:
    try: return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(expires_in)))).isoformat()
    except (TypeError, ValueError): return None


def get_provider(secret_key: str | bytes, provider: str) -> dict[str, Any]:
    if provider not in PROVIDERS: raise KeyError(provider)
    return dict(_load(secret_key).get("providers", {}).get(provider, {}))


def save_config(secret_key: str | bytes, provider: str, values: dict[str, str]) -> None:
    if provider not in PROVIDERS: raise KeyError(provider)
    data = _load(secret_key); record = data.setdefault("providers", {}).setdefault(provider, {})
    for key, value in values.items():
        if value and key not in {"client_secret", "api_secret"}: record[key] = value.strip()
    _save(secret_key, data)


def disconnect(secret_key: str | bytes, provider: str) -> None:
    data = _load(secret_key); record = data.setdefault("providers", {}).setdefault(provider, {})
    for key in ("access_token", "refresh_token", "token_type", "expires_at", "last_authorized_at", "oauth_session", "mcp_verified", "mcp_tools"):
        record.pop(key, None)
    _save(secret_key, data)


def status(secret_key: str | bytes, provider: str) -> dict[str, Any]:
    record = get_provider(secret_key, provider)
    expires_at = record.get("expires_at"); expired = False
    if expires_at:
        try: expired = datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc)
        except ValueError: expired = True
    oauth_authorized = bool(record.get("access_token")) and not expired
    verified = bool(record.get("mcp_verified")) and oauth_authorized
    endpoint = OFFICIAL_MCP_ENDPOINTS.get(provider)
    return {"provider": provider, "configured": bool(endpoint), "connected": verified,
            "oauth_authorized": oauth_authorized, "status": "connected" if verified else ("authorized" if oauth_authorized else ("authorization_required" if endpoint else "unconfigured")),
            "last_authorized_at": record.get("last_authorized_at"), "expires_at": expires_at,
            "capabilities": CAPABILITIES[provider], "live_writes": False, "read_only": True,
            "mcp_verified": verified, "discovered_tools": record.get("mcp_tools", []) if isinstance(record.get("mcp_tools", []), list) else []}


def _post(url: str, form: dict[str, str], *, opener=None) -> dict[str, Any]:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
    try:
        with (opener or urllib.request).urlopen(req, timeout=20) as response: return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"provider OAuth request failed with HTTP {exc.code}") from None


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as response: return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"provider OAuth registration failed with HTTP {exc.code}") from None


def fetch_metadata(endpoint: str, *, metadata_url: str | None = None, opener=None) -> dict[str, Any]:
    url = metadata_url or endpoint.rstrip("/") + "/.well-known/oauth-authorization-server"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with (opener or urllib.request).urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode())
        if not isinstance(payload, dict) or not payload.get("authorization_endpoint") or not payload.get("token_endpoint"):
            raise RuntimeError("OAuth metadata is incomplete")
        return payload
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"OAuth metadata unavailable (HTTP {exc.code}); authorization URL must be configured") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise RuntimeError("OAuth metadata unavailable; authorization URL must be configured") from None


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def begin_oauth(secret_key: str | bytes, provider: str, *, session_id: str, redirect_uri: str, metadata: dict[str, Any] | None = None, authorization_url: str | None = None) -> str:
    if provider not in PROVIDERS: raise KeyError(provider)
    record = get_provider(secret_key, provider); endpoint = OFFICIAL_MCP_ENDPOINTS[provider]["url"]
    metadata = metadata or {}
    verifier, challenge = _pkce(); state = secrets.token_urlsafe(32)
    client_id = record.get("client_id") or metadata.get("client_id")
    if not client_id: raise RuntimeError("OAuth client registration is required but no registration endpoint was provided")
    auth = authorization_url or metadata.get("authorization_endpoint")
    if not auth: raise RuntimeError("provider authorization URL is not configured")
    data = _load(secret_key); data.setdefault("oauth", {})[state] = {"provider": provider, "session_id": session_id, "code_verifier": verifier, "redirect_uri": redirect_uri, "client_id": client_id, "created_at": time.time()}
    _save(secret_key, data)
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    scopes = metadata.get("scopes_supported") or metadata.get("scope")
    if scopes: query["scope"] = " ".join(scopes) if isinstance(scopes, list) else str(scopes)
    return auth + ("&" if "?" in auth else "?") + urllib.parse.urlencode(query)


def _consume_state(secret_key: str | bytes, state: str, provider: str, session_id: str) -> dict[str, Any]:
    data = _load(secret_key); item = data.get("oauth", {}).pop(state, None); _save(secret_key, data)
    if not item or item.get("provider") != provider or item.get("session_id") != session_id or time.time() - float(item.get("created_at", 0)) > 600:
        raise ValueError("invalid or expired OAuth state")
    return item


def complete_oauth(secret_key: str | bytes, provider: str, *, state: str, code: str, session_id: str, token_endpoint: str) -> dict[str, Any]:
    item = _consume_state(secret_key, state, provider, session_id)
    payload = _post(token_endpoint, {"grant_type": "authorization_code", "code": code, "redirect_uri": item["redirect_uri"], "client_id": item["client_id"], "code_verifier": item["code_verifier"]})
    return _store_tokens(secret_key, provider, payload, session_id=session_id)


def _store_tokens(secret_key: str | bytes, provider: str, payload: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    data = _load(secret_key); record = data.setdefault("providers", {}).setdefault(provider, {})
    if not payload.get("access_token"): raise ValueError("provider OAuth response did not contain an access token")
    for key in ("access_token", "refresh_token", "token_type"):
        if payload.get(key): record[key] = payload[key]
    record.update({"expires_at": _expiry(payload.get("expires_in")), "last_authorized_at": _now(), "oauth_session": session_id, "mcp_verified": False, "mcp_tools": []})
    _save(secret_key, data); return status(secret_key, provider)


def access_token(secret_key: str | bytes, provider: str, *, session_id: str, refresh: Callable[[str], dict[str, Any]] | None = None) -> str | None:
    data = _load(secret_key); record = data.setdefault("providers", {}).setdefault(provider, {})
    if record.get("oauth_session") != session_id: return None
    token = record.get("access_token"); expires = record.get("expires_at")
    needs = not token
    if expires:
        try: needs = datetime.fromisoformat(expires) <= datetime.now(timezone.utc) + timedelta(seconds=60)
        except ValueError: needs = True
    if needs and record.get("refresh_token") and refresh:
        payload = refresh(record["refresh_token"]); _store_tokens(secret_key, provider, payload, session_id=session_id); record = _load(secret_key)["providers"][provider]
    return record.get("access_token")


def mark_mcp_verified(secret_key: str | bytes, provider: str, *, session_id: str, tools: list[str]) -> None:
    data = _load(secret_key); record = data.setdefault("providers", {}).setdefault(provider, {})
    if record.get("oauth_session") != session_id: return
    record.update({"mcp_verified": True, "mcp_tools": sorted(tools)}); _save(secret_key, data)


def redact(value: Any) -> Any:
    if isinstance(value, dict): return {k: ("[redacted]" if any(x in k.lower() for x in ("token", "secret", "password", "verifier")) else redact(v)) for k, v in value.items()}
    if isinstance(value, list): return [redact(v) for v in value]
    if isinstance(value, str): return value.replace("Bearer ", "Bearer [redacted]")
    return value
