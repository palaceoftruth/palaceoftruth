from datetime import datetime, timezone

from app.schemas.memory import MemoryEntryRequest
from app.services.memory_admission import evaluate_memory_write_admission


def _entry(scope_type: str, scope_key: str | None = None) -> MemoryEntryRequest:
    return MemoryEntryRequest.model_validate(
        {
            "tenant_id": "default",
            "title": "Scope admission",
            "body": "Verify the requested scope is authorized before durable storage.",
            "source": "test",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": {"type": scope_type, "key": scope_key},
        }
    )


def test_bound_hermes_client_writes_only_its_canonical_agent_scope() -> None:
    allowed = evaluate_memory_write_admission(
        body=_entry("agent", "iris"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "write:agent"],
        mcp_client_key="hermes-iris",
        mcp_agent_scope_key="iris",
    )
    sibling = evaluate_memory_write_admission(
        body=_entry("agent", "vera"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "write:agent"],
        mcp_client_key="hermes-iris",
        mcp_agent_scope_key="iris",
    )

    assert allowed.accepted is True
    assert sibling.reason_code == "hermes_agent_write_requires_canonical_scope"


def test_unbound_hermes_client_cannot_write_tenant_shared_memory() -> None:
    decision = evaluate_memory_write_admission(
        body=_entry("tenant_shared"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "write:agent"],
        mcp_client_key="hermes-iris",
        mcp_agent_scope_key=None,
    )

    assert decision.reason_code == "hermes_agent_write_requires_agent_scope"
