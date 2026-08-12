"""Server-owned containment mode for MCP clients.

Containment used to be decided by ``client_key.startswith("hermes-")``. The
registrant picks ``client_key``, so the guard was opt-out by renaming. The
containment decision now lives in ``mcp_clients.containment_mode``, written once
by the registration path and read back with the access token.

The registration path still *derives* the mode from the requested name, but it
derives it once, server-side, from a normalized form of the name — so
``hermes_prod``, ``Hermes.prod`` and ``hermes prod`` all land contained instead
of escaping. A caller may ask for containment explicitly; it can never ask to
drop it.
"""

from __future__ import annotations

import re
from typing import Any

CONTAINMENT_STANDARD = "standard"
CONTAINMENT_HERMES_AGENT = "hermes_agent"
VALID_CONTAINMENT_MODES = frozenset({CONTAINMENT_STANDARD, CONTAINMENT_HERMES_AGENT})

# The reserved family name. Any client key that normalizes into this namespace
# is contained, whatever separator or casing the registrant used.
_RESERVED_AGENT_FAMILY = "hermes"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_client_key(client_key: str | None) -> str:
    """Fold separators and casing so name variants cannot be used as a bypass."""
    if not client_key:
        return ""
    return _NON_ALNUM.sub("-", client_key.strip().lower()).strip("-")


def is_reserved_agent_client_key(client_key: str | None) -> bool:
    normalized = normalize_client_key(client_key)
    return normalized == _RESERVED_AGENT_FAMILY or normalized.startswith(f"{_RESERVED_AGENT_FAMILY}-")


def derive_containment_mode(*, client_key: str, requested_mode: str | None = None) -> str:
    """Containment mode for a newly registered client.

    Requesting containment is allowed; requesting *less* containment than the
    reserved name implies is not.
    """
    if is_reserved_agent_client_key(client_key):
        return CONTAINMENT_HERMES_AGENT
    if requested_mode == CONTAINMENT_HERMES_AGENT:
        return CONTAINMENT_HERMES_AGENT
    return CONTAINMENT_STANDARD


def normalize_containment_mode(value: Any) -> str:
    """Coerce a stored or transported value, failing closed on anything unknown."""
    # No MCP identity is the normal REST/API-key path, not a malformed MCP
    # mode. Authenticated MCP requests always attach a concrete value.
    if value is None:
        return CONTAINMENT_STANDARD
    if isinstance(value, str) and value in VALID_CONTAINMENT_MODES:
        return value
    return CONTAINMENT_HERMES_AGENT


def is_contained_agent_client(containment_mode: Any) -> bool:
    return normalize_containment_mode(containment_mode) == CONTAINMENT_HERMES_AGENT


def request_containment_mode(request: Any) -> str:
    """Containment mode attached to the authenticated request, if any."""
    return normalize_containment_mode(getattr(getattr(request, "state", None), "mcp_containment_mode", None))


def request_is_contained_agent_client(request: Any) -> bool:
    return is_contained_agent_client(request_containment_mode(request))
