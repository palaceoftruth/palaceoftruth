"""Server-derived provenance for durable memory writes.

``created_by_role`` used to be copied straight out of the request body, so any
caller could stamp ``system`` on content it authored. Provenance is now derived
from the authenticated principal at the HTTP boundary. In-process writers
(rollups, imports, dream/brief generators) still build request objects directly
and keep their own role, because their principal is the server itself.
"""

from __future__ import annotations

from typing import Any

from app.services.mcp_containment import is_contained_agent_client

# Roles the server will ever stamp on a request that arrived over HTTP. "system"
# is deliberately absent: it means "written by the server itself".
ROLE_AGENT = "agent"
ROLE_USER = "user"
ROLE_OPERATOR = "operator"

_AUTH_MODE_ROLES = {
    "mcp_oauth": ROLE_AGENT,
    "browser_extension": ROLE_USER,
    "api_key": ROLE_OPERATOR,
}

# Roles an operator API key may still choose for content it relays on behalf of
# a known author. Anything else, including "system", is clamped.
_OPERATOR_SELECTABLE_ROLES = frozenset({ROLE_AGENT, ROLE_USER, ROLE_OPERATOR, "assistant"})


def derive_created_by_role(request: Any, *, requested_role: str | None = None) -> str:
    """The provenance role for a memory write, from the authenticated principal."""
    state = getattr(request, "state", None)
    auth_mode = getattr(state, "auth_mode", None)
    if is_contained_agent_client(getattr(state, "mcp_containment_mode", None)):
        return ROLE_AGENT
    principal_role = _AUTH_MODE_ROLES.get(auth_mode if isinstance(auth_mode, str) else "")
    if principal_role is None:
        # Unknown principal: the least-privileged role, never "system".
        return ROLE_USER
    if principal_role == ROLE_OPERATOR and requested_role in _OPERATOR_SELECTABLE_ROLES:
        return requested_role
    return principal_role
