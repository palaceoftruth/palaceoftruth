# SAR-983 Palace OAuth Protected Resource Contract

Date: 2026-07-05

## TLDR

Palace should model its HTTP MCP server and REST API as OAuth protected
resources, not as an MCP-operation-only auth layer. The near-term contract is:

- Publish resource metadata for the MCP resource and API resource.
- Mint bearer tokens for one canonical resource/audience and reject tokens
  presented to a different resource.
- Keep legacy API-key access during migration, but normalize it into the same
  capability model used by OAuth bearer tokens.
- Split implementation into the existing follow-ups: SAR-984 for tenant-safe
  client identity and resource binding, SAR-985 for the shared scope catalog,
  and SAR-986 for the shared `AuthContext`/capability dependency.

## Source Inventory

Standards and protocol references:

- RFC 9728, OAuth 2.0 Protected Resource Metadata:
  <https://www.rfc-editor.org/rfc/rfc9728.html>
- RFC 8707, Resource Indicators for OAuth 2.0:
  <https://www.rfc-editor.org/rfc/rfc8707.html>
- RFC 7662, OAuth 2.0 Token Introspection:
  <https://www.rfc-editor.org/rfc/rfc7662.html>
- RFC 8414, OAuth 2.0 Authorization Server Metadata:
  <https://www.rfc-editor.org/rfc/rfc8414.html>
- RFC 9700, Best Current Practice for OAuth 2.0 Security:
  <https://www.rfc-editor.org/rfc/rfc9700.html>
- RFC 7636, Proof Key for Code Exchange by OAuth Public Clients:
  <https://www.rfc-editor.org/rfc/rfc7636.html>
- RFC 8628, OAuth 2.0 Device Authorization Grant:
  <https://www.rfc-editor.org/rfc/rfc8628.html>
- RFC 7591, OAuth 2.0 Dynamic Client Registration Protocol:
  <https://www.rfc-editor.org/rfc/rfc7591.html>
- MCP draft authorization spec:
  <https://modelcontextprotocol.io/specification/draft/basic/authorization>

Current Palace code reviewed:

- `backend/app/api/mcp_oauth.py`
- `backend/app/auth.py`
- `backend/app/mcp_server.py`
- `backend/app/api/memory.py`
- `backend/app/api/palace.py`
- `backend/app/schemas/memory.py`
- `frontend/src/api/types.ts`
- `frontend/src/pages/PalaceControlTower.tsx`

## Current State

Palace already has the useful pieces of a first-party OAuth bridge:

- `POST /api/v1/memory/mcp/oauth/token` issues client-credentials bearer
  tokens for registered MCP clients.
- `GET /.well-known/oauth-protected-resource` publishes protected-resource
  metadata and advertises the MCP server resource.
- Memory routes accept API keys and MCP bearer tokens through shared helpers
  such as `verify_memory_auth` and `require_mcp_scope`.
- Capture routes use scoped bearer checks for `capture:write` and
  `capture:job:read`.
- Control Tower can register MCP clients and return secret-safe config snippets.

The contract is still incomplete in three important ways:

- Client identity is not tenant-safe at token issuance. `mcp_clients` is unique
  by `(tenant_id, client_key)`, but token issuance selects by `client_key` alone.
- Tokens do not persist or validate resource/audience metadata. They store
  scopes and expiry, but not the protected resource they were minted for.
- Scope definitions are split. Backend schemas support read/write,
  write-specific memory scopes, admin/local/destructive flags, and capture
  scopes, while the frontend registration type and UI expose only a subset.

## Client Classes

| Client class | Phase-one auth shape | Notes |
| --- | --- | --- |
| Codex MCP stdio | Legacy API key by default; OAuth bearer optional for remote HTTP | Keep stdio simple and secret-manager driven. Do not force remote OAuth for local repo-owned adapter setup. |
| Codex MCP HTTP | First-party OAuth client credentials | Requires tenant-safe client id and resource-bound token. |
| Hermes plugin | First-party OAuth client credentials or API key during migration | Must preserve tenant isolation and avoid accepting tokens minted for another Palace resource. |
| Rollout smoke/jobs | API key during migration; OAuth client credentials after SAR-986 | Needs non-interactive service identity and structured audit metadata. |
| CLI/scripts | API key during migration; device flow or client credentials later | Device flow is useful only when scripts act for a human user. |
| Browser/admin UI | Current API key localStorage path during migration; future auth code + PKCE | Public browser clients must use PKCE and must not receive long-lived client secrets. |
| Browser extension | Current scoped bearer route for capture; future authorization-code or device flow | Keep `capture:write` and `capture:job:read` narrowly scoped. |
| Future third-party MCP clients | OAuth protected-resource discovery plus client metadata document or pre-registration | Dynamic client registration can remain a compatibility option, not the default. |

## Canonical Resources

Phase one should define two stable Palace resource identifiers:

| Resource | Canonical identifier | Surface |
| --- | --- | --- |
| Palace MCP HTTP resource | `https://mcp.palace.sarvent.cloud/mcp` for production, `https://mcp.palaceoftruth.test/mcp` for local dev | Streamable HTTP MCP endpoint and MCP tool calls. |
| Palace REST API resource | `https://api.palace.sarvent.cloud/api/v1` for production, `https://api.palaceoftruth.test/api/v1` for local dev | REST route families used by agents, browser extension, UI, and operational clients. |

The resource identifier should be an `https` URL with no fragment. Tokens should
store the canonical resource at issuance and validation should compare the
presented token's audience/resource against the route family being accessed.

The MCP protected-resource metadata endpoint should advertise the MCP resource
and its authorization server. A later API metadata endpoint can advertise the
REST API resource when API OAuth is enabled beyond memory/capture routes.

## Scope And Capability Matrix

Palace should keep the current broad MCP scopes for migration compatibility and
add resource-action scopes only where they remove ambiguity.

| Capability | Compatibility scope | Future resource-action scope | Route families |
| --- | --- | --- | --- |
| Read memory and context | `read` | `memory:read` | `/memory/whoami`, `/memory/entries`, `/memory/scopes`, `/memory/retrieve`, `/memory/retrieve-agent`, `/memory/trajectory`, `/memory/source-trust`, `/memory/wakeup-brief` |
| Write general memory | `write` | `memory:write` | `/memory/entries`, `/memory/entries/batch`, legacy artifact writes |
| Write agent memory | `write`, `write:agent` | `memory:write:agent` | `/memory/entries` with `scope.type=agent` |
| Write workspace memory | `write`, `write:workspace` | `memory:write:workspace` | `/memory/entries` with `scope.type=workspace` |
| Write session memory | `write`, `write:session` | `memory:write:session` | `/memory/entries` with `scope.type=session` |
| Read jobs | `read`, `capture:job:read` | `jobs:read`, `capture:job:read` | `/memory/jobs`, `/jobs` |
| Capture browser/page content | `capture:write` | `capture:write` | `/capture` |
| Register/revoke MCP clients | `admin` | `palace:admin:mcp-clients` | `/palace/mcp-clients/*` |
| Read Palace operations views | API-key-only today | `palace:read` | `/palace`, `/palace/control-tower`, source/fact/audit reads |
| Mutate Palace rooms/sources/runs | API-key-only today | `palace:write` or narrower admin scopes | sync sources, room updates, pins, runs, claim review |
| Local-only adapter guard | `local_only` | token/client policy flag | Adapter runtime policy, not a route permission by itself. |
| Destructive-operation guard | `destructive_prohibited` | token/client policy flag | Deny deletion/revoke/destructive operations even if broader scopes are present. |

Implementation note: `admin` can continue to imply narrower Palace operations in
phase one, but the capability helper should make that implication explicit.
`local_only` and `destructive_prohibited` should be modeled as token/client
policy flags because they constrain where or how a client can act; they are not
ordinary route permissions.

## Tenant Isolation Rules

- Client ids must be globally unique, or token issuance must include an
  unambiguous tenant qualifier. The current `client_key`-only lookup is not a
  safe issuer contract because two tenants can own the same `client_key`.
- Token validation must always bind `tenant_id`, `client_id`, `token_hash`,
  scopes, resource/audience, expiry, and revocation state.
- `/api/v1/memory/whoami` should expose non-secret auth metadata needed by MCP
  validation: `tenant_id`, `auth_mode`, client key/name/id where safe,
  granted scopes/capabilities, resource/audience, and token id/hash reference
  only as a non-reversible prefix or opaque handle.
- Introspection-style inactive responses must not leak whether another tenant's
  token or client exists.

## Break-Glass API-Key Policy

API keys remain valid during migration because they are the existing operator
and automation path. They should be treated as legacy break-glass credentials
with these constraints:

- API-key clients must continue to send explicit `X-MCP-Scope` /
  `X-MCP-Scopes` headers for memory MCP use until they are migrated.
- New route code should not branch on raw API-key versus OAuth mode. It should
  receive an authenticated principal and require capabilities.
- API keys should not gain new ambient permissions by default. When a route
  moves behind capability helpers, legacy API-key mode should be granted only
  the capabilities intentionally preserved for that route family.

## Migration Phases

1. SAR-984: make token issuance tenant-safe and resource-bound.
   - Add a tenant-qualified or globally unique client id contract.
   - Persist token resource/audience.
   - Reject missing or incorrect `resource` where OAuth clients can provide it.
   - Extend `whoami` with non-secret resource/audience metadata.
2. SAR-985: centralize the scope catalog.
   - Move supported scope definitions into one backend source of truth or a
     generated schema.
   - Sync frontend types and Control Tower options.
   - Fail closed for unknown scopes in registration, token issuance, bearer
     validation, and MCP HTTP auth.
3. SAR-986: introduce shared `AuthContext` and capability checks.
   - Populate one principal shape for API key, MCP bearer, browser extension,
     browser/admin UI, and future PKCE tokens.
   - Add `require_capability(...)` or equivalent route dependencies.
   - Move non-memory Palace route families from ambient `verify_api_key` toward
     explicit capabilities.
4. Future browser/public-client phase.
   - Add authorization-code + PKCE for browser/admin UI and third-party clients.
   - Consider device authorization for CLIs that act on behalf of a human.
   - Keep dynamic client registration optional; prefer pre-registration or
     client metadata documents for MCP clients.

## Verification Expectations For Follow-Ups

SAR-984 should prove:

- Same `client_key` in two tenants cannot mint a token for the wrong tenant.
- Invalid tenant/client combinations fail closed.
- Missing or wrong resource is rejected where resource binding is required.
- Valid legacy clients continue to work through the documented compatibility
  path.

SAR-985 should prove:

- Backend and frontend consume the same supported scope set.
- Control Tower can register clients with every supported scope.
- Unsupported scopes fail closed in all validation paths.
- Generated snippets do not persist or repeat raw secrets.

SAR-986 should prove:

- Read-only tokens cannot write.
- Write tokens cannot cross unauthorized memory scope grants.
- Revoked/expired tokens fail closed.
- Legacy API-key mode works only where explicitly allowed.
- Non-memory route families use capabilities rather than implicit API-key
  admission.

## Caveats

- This document defines the contract; it intentionally does not mutate OAuth
  runtime behavior.
- The canonical production resource URLs reflect current Palace hostnames. If
  deployment routing changes, resource identifiers must change through an
  explicit compatibility plan, not silently.
- OAuth 2.1 remains an IETF draft, so this plan cites stable RFCs for concrete
  resource metadata, resource indicators, introspection, authorization-server
  metadata, PKCE, device flow, dynamic registration, and OAuth security BCP
  requirements.
