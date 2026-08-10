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
        containment_mode="hermes_agent",
        mcp_agent_scope_key="iris",
    )
    sibling = evaluate_memory_write_admission(
        body=_entry("agent", "vera"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "write:agent"],
        mcp_client_key="hermes-iris",
        containment_mode="hermes_agent",
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
        containment_mode="hermes_agent",
        mcp_agent_scope_key=None,
    )

    assert decision.reason_code == "hermes_agent_write_requires_agent_scope"


def test_hermes_admin_scope_does_not_bypass_canonical_agent_write_binding() -> None:
    decision = evaluate_memory_write_admission(
        body=_entry("tenant_shared"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "admin"],
        mcp_client_key="hermes-iris",
        containment_mode="hermes_agent",
        mcp_agent_scope_key="iris",
    )

    assert decision.reason_code == "hermes_agent_write_requires_agent_scope"


def test_renamed_hermes_client_stays_contained_by_stored_mode() -> None:
    """A client key that dodges the old "hermes-" prefix no longer escapes."""
    decision = evaluate_memory_write_admission(
        body=_entry("tenant_shared"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "admin"],
        mcp_client_key="hermes_prod",
        mcp_agent_scope_key="iris",
        containment_mode="hermes_agent",
    )

    assert decision.reason_code == "hermes_agent_write_requires_agent_scope"


def test_tenant_shared_write_requires_an_explicit_write_grant() -> None:
    denied = evaluate_memory_write_admission(
        body=_entry("tenant_shared"),
        auth_mode="mcp_oauth",
        allowed_scopes=["read", "write:agent"],
        mcp_client_key="analytics",
        mcp_agent_scope_key=None,
    )
    granted = evaluate_memory_write_admission(
        body=_entry("tenant_shared"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write"],
        mcp_client_key="analytics",
        mcp_agent_scope_key=None,
    )

    assert denied.reason_code == "missing_write"
    assert granted.accepted is True


def test_bound_client_cannot_write_another_agents_scope() -> None:
    decision = evaluate_memory_write_admission(
        body=_entry("agent", "vera"),
        auth_mode="mcp_oauth",
        allowed_scopes=["write", "write:agent"],
        mcp_client_key="analytics",
        mcp_agent_scope_key="iris",
    )

    assert decision.reason_code == "agent_write_outside_bound_scope"


def test_browser_extension_writes_are_scope_checked() -> None:
    decision = evaluate_memory_write_admission(
        body=_entry("tenant_shared"),
        auth_mode="browser_extension",
        allowed_scopes=["capture:write"],
        mcp_client_key=None,
    )

    assert decision.reason_code == "missing_write"
