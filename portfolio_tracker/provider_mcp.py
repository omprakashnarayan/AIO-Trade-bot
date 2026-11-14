"""MCP-only provider bridge.

The bridge intentionally has no broker REST implementation.  It speaks the
MCP Streamable HTTP JSON-RPC transport and treats discovery as a prerequisite
for every provider read.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
import ssl
import threading
import urllib.error
import urllib.request
from typing import Any, Mapping

from .brokers.base import AdapterCapability, BrokerAdapterError, BrokerMetadata, LiveExecutionDisabled

_SECRET = re.compile(r"(?i)(bearer\s+|token[=:]\s*|api[_-]?key[=:]\s*|secret[=:]\s*|password[=:]\s*)[^\s,;]+|sk-[A-Za-z0-9_-]+|ghp_[A-Za-z0-9]+")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _SECRET.sub(lambda m: (m.group(1) if m.group(1) else "") + "[REDACTED]", value)
    if isinstance(value, Mapping):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class DiscoveredTool:
    name: str
    input_schema: Mapping[str, Any]
    description: str = ""


class MCPAuthorizationRequired(BrokerAdapterError):
    def __init__(self, provider: str, authorization_url: str):
        self.authorization_url = authorization_url
        super().__init__("mcp_authorization_required", "provider MCP authorization is required in a browser", provider=provider, retryable=False)


class MCPClient:
    """Small standards-compliant Streamable HTTP JSON-RPC MCP client."""
    def __init__(self, url: str, *, provider: str, timeout: float = 30.0, connect_timeout: float = 5.0,
                 authorization_url: str | None = None, access_token: str | None = None,
                 token_type: str = "Bearer", token_provider=None, opener=None):
        if not url.startswith("https://") and not url.startswith("http://"):
            raise ValueError("MCP endpoint must be an HTTP(S) URL")
        self.url, self.provider = url, provider
        self.timeout, self.connect_timeout = float(timeout), float(connect_timeout)
        self.authorization_url = authorization_url or url
        self.access_token, self.token_type, self.token_provider = access_token, token_type, token_provider
        self._opener = opener or urllib.request.build_opener()
        self._next_id = 0
        self._session_id: str | None = None
        self.tools: dict[str, DiscoveredTool] = {}
        self.initialized = False
        self.verified = False
        self._lock = threading.Lock()

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None, *, notification: bool = False) -> Any:
        with self._lock:
            if not notification:
                self._next_id += 1
                request_id = self._next_id
            else:
                request_id = None
        payload = {"jsonrpc": "2.0", "method": method}
        if request_id is not None: payload["id"] = request_id
        if params is not None: payload["params"] = dict(params)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        token = self.token_provider() if self.token_provider else self.access_token
        if token:
            headers["Authorization"] = f"{self.token_type} {token}"
        if self._session_id: headers["MCP-Session-Id"] = self._session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                session_id = response.headers.get("MCP-Session-Id")
                if session_id: self._session_id = session_id
                if notification: return None
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise MCPAuthorizationRequired(self.provider, self.authorization_url) from None
            raise BrokerAdapterError("mcp_http_error", f"provider MCP HTTP {exc.code}", provider=self.provider, retryable=exc.code >= 500) from None
        except (TimeoutError, urllib.error.URLError, OSError):
            raise BrokerAdapterError("mcp_timeout", "provider MCP request timed out or failed", provider=self.provider, retryable=True) from None
        try:
            # Streamable HTTP may return a single JSON object or SSE data lines.
            if raw.lstrip().startswith("data:"):
                raw = next(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
            result = json.loads(raw)
        except (TypeError, ValueError, StopIteration):
            raise BrokerAdapterError("mcp_invalid_json", "provider MCP returned invalid JSON-RPC", provider=self.provider) from None
        if "error" in result:
            error = result.get("error") or {}
            message = str(error.get("message") or "provider MCP request failed")
            if any(word in message.lower() for word in ("unauthor", "consent", "login", "oauth")):
                raise MCPAuthorizationRequired(self.provider, self.authorization_url)
            raise BrokerAdapterError("mcp_rpc_error", str(redact(message)), provider=self.provider)
        if "result" not in result:
            raise BrokerAdapterError("mcp_invalid_response", "provider MCP response has no result", provider=self.provider)
        return result["result"]

    def initialize(self) -> Mapping[str, Any]:
        result = self._rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "portfolio-tracker", "version": "mcp-only"}})
        self._rpc("notifications/initialized", notification=True)
        self.initialized = True
        return result

    def discover(self) -> dict[str, DiscoveredTool]:
        if not self.initialized: self.initialize()
        result = self._rpc("tools/list", {})
        raw = result.get("tools") if isinstance(result, Mapping) else None
        if not isinstance(raw, list):
            raise BrokerAdapterError("mcp_discovery_invalid", "provider MCP tools/list did not return tools", provider=self.provider)
        self.tools = {}
        for item in raw:
            if not isinstance(item, Mapping) or not isinstance(item.get("name"), str): continue
            schema = item.get("inputSchema", {})
            if not isinstance(schema, Mapping) or schema.get("type", "object") != "object": continue
            name = item["name"]
            # Provider-hosted servers may advertise write tools. Never retain or
            # expose destructive tools in this app's MCP-only read policy.
            if any(word in name.lower() for word in ("place_order", "submit_order", "modify_order", "cancel_order", "execute_order", "transfer", "withdraw")):
                continue
            self.tools[name] = DiscoveredTool(name, dict(schema), str(item.get("description", "")))
        self.verified = bool(self.tools)
        if not self.verified:
            raise BrokerAdapterError("mcp_no_read_tools", "provider MCP exposed no verifiable tools", provider=self.provider)
        return dict(self.tools)

    def discover_and_verify(self, required: tuple[str, ...] = ()) -> dict[str, Any]:
        tools = self.discover()
        missing = [name for name in required if name not in tools]
        return {"verified": not missing and bool(tools), "tools": sorted(tools), "missing": missing}

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if not self.verified or name not in self.tools:
            raise BrokerAdapterError("tool_unverified", "provider MCP tool was not discovered and verified", provider=self.provider)
        result = self._rpc("tools/call", {"name": name, "arguments": dict(arguments or {})})
        if isinstance(result, Mapping) and result.get("isError"):
            raise BrokerAdapterError("mcp_tool_error", "provider MCP read tool returned an error", provider=self.provider)
        # MCP content commonly wraps JSON in a text content block.
        content = result.get("content") if isinstance(result, Mapping) else None
        if isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping) and block.get("type") == "text":
                    try: parsed = json.loads(block.get("text", ""))
                    except (TypeError, ValueError): continue
                    if isinstance(parsed, Mapping): return parsed
        if isinstance(result, Mapping): return result
        raise BrokerAdapterError("mcp_tool_response_invalid", "provider MCP tool response was not an object", provider=self.provider)


class MCPProviderAdapter:
    """Read-only adapter used by ProviderSyncService; writes always reject."""
    def __init__(self, *, provider: str, broker_id: str, account_id: str, client: MCPClient):
        self.provider_name = provider
        self.client = client
        self.metadata = BrokerMetadata(provider, account_id, broker_id, "mcp_only", frozenset({AdapterCapability.ACCOUNT, AdapterCapability.POSITIONS, AdapterCapability.STATUS, AdapterCapability.RECONCILE}))
        self._account_id = account_id

    def discover(self) -> dict[str, Any]:
        return self.client.discover_and_verify()

    def snapshot(self) -> Mapping[str, Any]:
        if not self.client.verified: self.discover()
        aliases = ("snapshot", "portfolio_snapshot", "get_portfolio_snapshot", "holdings", "get_holdings", "positions", "get_positions")
        for alias in aliases:
            if alias in self.client.tools:
                return self.client.call(alias, {"account_id": self._account_id})
        raise BrokerAdapterError("mcp_snapshot_tool_unverified", "no verified provider portfolio read tool was discovered", provider=self.provider_name)

    def submit_order(self, intent):
        raise LiveExecutionDisabled(self.provider_name)


class VerifiedMCPTransport:
    """Compatibility injectable transport for tests and local adapters."""
    def __init__(self, discover, call):
        self._discover, self._call_fn, self.tools, self.verified = discover, call, {}, False
    def discover_and_verify(self, required: tuple[str, ...] = ("snapshot",)) -> dict[str, Any]:
        raw = self._discover()
        if not isinstance(raw, (list, tuple)): raise BrokerAdapterError("discovery_invalid", "provider tool discovery was not a list", provider="mcp")
        self.tools = {}
        for item in raw:
            if isinstance(item, DiscoveredTool):
                self.tools[item.name] = item
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str) and isinstance(item.get("inputSchema", {}), Mapping):
                self.tools[item["name"]] = DiscoveredTool(item["name"], item.get("inputSchema", {}))
        self.verified = not [name for name in required if name not in self.tools]
        return {"verified": self.verified, "tools": sorted(self.tools), "missing": [name for name in required if name not in self.tools]}
    def __call__(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.verified or operation not in self.tools: raise BrokerAdapterError("tool_unverified", "provider MCP tool was not discovered and verified", provider="mcp")
        result = self._call_fn(operation, dict(payload))
        if not isinstance(result, Mapping): raise BrokerAdapterError("response_invalid", "provider MCP response was not an object", provider="mcp")
        return result
