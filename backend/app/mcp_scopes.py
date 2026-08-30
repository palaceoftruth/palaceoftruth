from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


McpOperationScope = Literal[
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "audit:write",
    "admin",
    "local_only",
    "destructive_prohibited",
    "capture:write",
    "capture:job:read",
    "curation:approve",
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
    McpScopeDefinition("audit:write", "Write audit events", "Append MCP request audit events without changing client grants.", "audit"),
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
    McpScopeDefinition("curation:approve", "Approve curation", "Approve or reject candidate curation artifacts.", "curation"),
)

ALL_MCP_OPERATION_SCOPES: tuple[McpOperationScope, ...] = tuple(scope.value for scope in MCP_SCOPE_CATALOG)
VALID_MCP_OPERATION_SCOPES = frozenset(ALL_MCP_OPERATION_SCOPES)
DEFAULT_MCP_CLIENT_SCOPES: tuple[McpOperationScope, ...] = (
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "audit:write",
    "admin",
    "local_only",
    "destructive_prohibited",
)


# Scope grant persisted for tenant API keys that existed before per-key scopes
# were stored (migration 055). It reproduces the privilege those keys already
# had: every REST capability gate passed unconditionally, and the MCP scope
# gate accepted any self-declared header. Narrowing an individual key is an
# operator action, not something the migration can infer.
LEGACY_API_KEY_SCOPES: tuple[McpOperationScope, ...] = (
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "audit:write",
    "admin",
    "capture:write",
    "capture:job:read",
    "curation:approve",
)

# Scope grant given to an API key created after migration 055 when the caller
# does not ask for a specific set. It covers every routine memory and capture
# surface but withholds "admin", which now has to be requested on purpose.
DEFAULT_API_KEY_SCOPES: tuple[McpOperationScope, ...] = (
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "audit:write",
    "capture:write",
    "capture:job:read",
)

# Required scope for every MCP operation that reaches _run_mcp_operation.
# Dispatch fails closed on any operation missing an entry, so a new tool cannot
# ship without an explicit authorization decision. Tool aliases (palace_search,
# palace_remember, ...) delegate to the base tool and are covered by its entry.
MCP_OPERATION_SCOPES: dict[str, McpOperationScope] = {
    # read surfaces
    "connection_info": "read",
    "whoami": "read",
    "get_memory_job": "read",
    "list_memory_entries": "read",
    "list_memory_scopes": "read",
    "list_memory_jobs": "read",
    "get_graph": "read",
    "get_item_relationships": "read",
    "list_temporal_facts": "read",
    "get_claim_support": "read",
    "get_answer_audit": "read",
    "get_palace_room": "read",
    "get_wakeup_brief": "read",
    "get_wakeup_context": "read",
    "palace_context": "read",
    "get_retrieval_doctor": "read",
    "retrieve_memory": "read",
    "retrieve_agent_memory": "read",
    "retrieve_memory_trajectory": "read",
    "palace_semantic_recall": "read",
    "palace_fact_recall": "read",
    "search_items": "read",
    "list_tags": "read",
    "list_items": "read",
    # write surfaces
    "create_memory_entry": "write",
    "create_memory_entries_batch": "write",
    "capture_checkpoint": "write",
    # maintenance surfaces
    "backfill_deferred_relationships": "admin",
}

# Additional scope demanded by a write whose destination is an explicitly
# requested scope. This is what makes write:agent / write:workspace /
# write:session real rather than decorative: holding plain "write" grants
# tenant_shared writes only.
MCP_DESTINATION_SCOPES: dict[str, McpOperationScope] = {
    "agent": "write:agent",
    "workspace": "write:workspace",
    "session": "write:session",
    "tenant_shared": "write",
}


class UnmappedMcpOperationError(LookupError):
    """Raised when an operation has no entry in MCP_OPERATION_SCOPES."""


def required_scope_for_operation(operation: str) -> McpOperationScope:
    """Return the scope an operation needs, failing closed when unmapped."""
    try:
        return MCP_OPERATION_SCOPES[operation]
    except KeyError as exc:
        raise UnmappedMcpOperationError(
            f"MCP operation {operation!r} has no required scope; refusing to dispatch"
        ) from exc


def destination_scope_for(scope_type: str | None) -> McpOperationScope | None:
    """Return the extra scope a write to ``scope_type`` needs, if any."""
    if scope_type is None:
        return None
    return MCP_DESTINATION_SCOPES.get(scope_type)


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
