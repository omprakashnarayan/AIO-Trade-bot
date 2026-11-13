"""Provider MCP bridge contracts with explicit discovery/schema verification."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from .brokers.base import BrokerAdapterError

@dataclass(frozen=True)
class DiscoveredTool:
    name: str
    input_schema: Mapping[str, Any]

class VerifiedMCPTransport:
    """Injectable provider transport; never assumes a provider tool name."""
    def __init__(self, discover, call):
        self._discover = discover; self._call_fn = call; self.tools: dict[str, DiscoveredTool] = {}
        self.verified = False
    def discover_and_verify(self, required: tuple[str, ...] = ("snapshot",)) -> dict[str, Any]:
        raw = self._discover()
        if not isinstance(raw, (list, tuple)): raise BrokerAdapterError("discovery_invalid", "provider tool discovery was not a list", provider="mcp")
        self.tools = {}
        for item in raw:
            if isinstance(item, DiscoveredTool): tool=item
            elif isinstance(item, Mapping) and isinstance(item.get("name"), str) and isinstance(item.get("inputSchema", {}), Mapping): tool=DiscoveredTool(item["name"], item.get("inputSchema", {}))
            else: continue
            self.tools[tool.name] = tool
        missing=[name for name in required if name not in self.tools]
        self.verified=not missing
        return {"verified":self.verified,"tools":sorted(self.tools),"missing":missing}
    def __call__(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if not self.verified or operation not in self.tools: raise BrokerAdapterError("tool_unverified", "provider MCP tool was not discovered and verified", provider="mcp")
        result=self._call_fn(operation, dict(payload))
        if not isinstance(result, Mapping): raise BrokerAdapterError("response_invalid", "provider MCP response was not an object", provider="mcp")
        return result
