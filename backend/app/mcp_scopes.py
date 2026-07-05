from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


McpOperationScope = Literal[
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "admin",
    "local_only",
    "destructive_prohibited",
    "capture:write",
    "capture:job:read",
]


@dataclass(frozen=True)
class McpScopeDefinition:
    value: McpOperationScope
    label: str
    description: str
    category: str


MCP_SCOPE_CATALOG: tuple[McpScopeDefinition, ...] = (
    McpScopeDefinition("read", "Read memory", "Read memory, graph, claim, wakeup, and audit surfaces.", "memory"),
    McpScopeDefinition("write", "Write memory", "Create tenant-shared memory entries and run write-capable MCP tools.", "memory"),
    McpScopeDefinition("write:agent", "Write agent scope", "Create memory entries in explicitly requested agent scopes.", "memory"),
    McpScopeDefinition("write:workspace", "Write workspace scope", "Create memory entries in explicitly requested workspace scopes.", "memory"),
    McpScopeDefinition("write:session", "Write session scope", "Create memory entries in explicitly requested session scopes.", "memory"),
    McpScopeDefinition("admin", "Admin tools", "Call administrative MCP operations such as maintenance backfills.", "admin"),
    McpScopeDefinition("local_only", "Local-only client", "Marks a client as intended for local runtime use only.", "guardrail"),
    McpScopeDefinition(
        "destructive_prohibited",
        "No destructive tools",
        "Marks a client as prohibited from destructive operations.",
        "guardrail",
    ),
    McpScopeDefinition("capture:write", "Capture writes", "Allow browser extension or capture clients to create captures.", "capture"),
    McpScopeDefinition("capture:job:read", "Capture job reads", "Allow capture clients to poll their capture jobs.", "capture"),
)

ALL_MCP_OPERATION_SCOPES: tuple[McpOperationScope, ...] = tuple(scope.value for scope in MCP_SCOPE_CATALOG)
VALID_MCP_OPERATION_SCOPES = frozenset(ALL_MCP_OPERATION_SCOPES)
DEFAULT_MCP_CLIENT_SCOPES: tuple[McpOperationScope, ...] = (
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "admin",
    "local_only",
    "destructive_prohibited",
)


def serialize_mcp_scope_catalog() -> list[dict[str, str]]:
    return [
        {
            "value": scope.value,
            "label": scope.label,
            "description": scope.description,
            "category": scope.category,
        }
        for scope in MCP_SCOPE_CATALOG
    ]
