# SAR-989 Palace OAuth User-Delegated Auth Design

Date: 2026-07-05

## TLDR

Palace should keep the immediate OAuth migration focused on service clients and
resource-bound bearer tokens, then add user-delegated OAuth as a later phase.
For browser-capable Codex, CLI, and admin UI clients, the default should be
Authorization Code with PKCE. Device Authorization Grant should be an explicit
fallback for no-browser or no-loopback environments such as SSH sessions,
containers, remote development hosts, and appliance-style clients.

Do not implement user-delegated auth by extending the current
client-credentials MCP client registry in place. Add a separate public-client
registration model with redirect URI, consent, refresh-token, session, and
audit semantics after SAR-984, SAR-985, and SAR-986 finish the service-client
trust boundary.

## Source Inventory

Standards and protocol references:

- RFC 7636, Proof Key for Code Exchange by OAuth Public Clients:
  <https://www.rfc-editor.org/rfc/rfc7636>
- RFC 8252, OAuth 2.0 for Native Apps:
  <https://www.rfc-editor.org/rfc/rfc8252>
- RFC 8628, OAuth 2.0 Device Authorization Grant:
  <https://www.rfc-editor.org/rfc/rfc8628>
- RFC 8707, Resource Indicators for OAuth 2.0:
  <https://www.rfc-editor.org/rfc/rfc8707>
- RFC 8414, OAuth 2.0 Authorization Server Metadata:
  <https://www.rfc-editor.org/rfc/rfc8414>
- RFC 9728, OAuth 2.0 Protected Resource Metadata:
  <https://www.rfc-editor.org/rfc/rfc9728>
- RFC 9700, Best Current Practice for OAuth 2.0 Security:
  <https://www.rfc-editor.org/rfc/rfc9700>
- MCP draft authorization spec:
  <https://modelcontextprotocol.io/specification/draft/basic/authorization>
- SAR-983 Palace OAuth Protected Resource Contract:
  `docs/research/sar-983-oauth-protected-resource-contract.md`

Current Palace code and docs reviewed:

- `backend/app/api/mcp_oauth.py`
- `backend/app/auth.py`
- `backend/app/api/memory.py`
- `backend/app/api/palace.py`
- `backend/app/schemas/memory.py`
- `backend/app/mcp_server.py`
- `frontend/src/pages/PalaceControlTower.tsx`
- `frontend/src/api/types.ts`
- `.env.example`
- `README.md`

## Current State

Palace currently supports first-party service-client OAuth for MCP-style clients:

- Registered MCP clients can exchange `client_credentials` for bearer tokens.
- HTTP MCP exposes protected-resource metadata.
- Memory and capture routes have scoped bearer-token handling.
- Control Tower registers MCP clients and shows secret-safe configuration
  snippets.

Palace does not yet have the primitives needed for user-delegated public-client
auth:

- No authorization endpoint, consent screen, or user session model.
- No redirect URI registry or loopback callback policy.
- No PKCE code verifier/challenge storage.
- No device authorization endpoint or device-code polling state.
- No refresh-token rotation or user grant revocation surface.
- No distinction between service clients and public/native/browser clients in
  the stored MCP client registry.

That means user-delegated OAuth should remain a design and backlog phase until
the service-client migration lands the tenant-safe/resource-bound base.

## Client Flow Decision

| Client shape | Default flow | Fallback | Rationale |
| --- | --- | --- | --- |
| Browser/admin UI | Authorization Code + PKCE | None initially | Browser clients are public clients and must not hold client secrets. They also need first-class user sessions and consent. |
| Codex desktop or local CLI with browser and loopback | Authorization Code + PKCE | Device flow with explicit operator opt-in | PKCE avoids user-code phishing and binds the authorization response to the same local process through the code verifier and redirect. |
| CLI in SSH, container, remote VM, or cloud IDE | Device Authorization Grant | Manual token import only for break-glass | These environments often cannot open a local browser or bind a useful loopback callback. |
| MCP HTTP clients acting for a service/runtime | Client Credentials | Legacy API key during migration | Service identity should not pretend to be a human-delegated user grant. |
| Browser extension capture | Authorization Code + PKCE | Device flow only if extension runtime blocks redirect flow | Capture scopes stay narrow: `capture:write` and `capture:job:read`. |
| CI and unattended jobs | Client Credentials or workload identity | Legacy API key during migration | No human in the loop means PKCE/device flow is the wrong trust model. |

## PKCE Contract

Palace should implement PKCE for public user-delegated clients with these
minimum requirements:

- Public clients register allowed redirect URIs, including loopback redirect
  patterns for native clients and exact HTTPS redirects for browser/admin UI.
- Authorization requests require `code_challenge` and
  `code_challenge_method=S256`.
- Authorization codes are one-time use, short-lived, tenant-bound,
  user-bound, resource-bound, and tied to the original challenge, client,
  redirect URI, state, issuer, and requested scopes.
- Token exchange requires the original `code_verifier`.
- The `resource` parameter follows the SAR-983 resource contract and is stored
  in the resulting grant/token metadata.
- Refresh tokens, when issued, rotate on use and can be revoked by user,
  client, tenant, or suspicious grant lineage.
- Consent screens name the Palace resource, client display name, tenant, user,
  scopes, and whether offline/refresh access is being requested.

PKCE should be the only first-class user-delegated flow for the admin UI and the
default flow for local Codex/CLI clients.

## Device Flow Contract

Device Authorization Grant is useful, but Palace should treat it as a fallback
with stricter policy because the user transcribes a code and can complete the
authorization on a different device.

Minimum requirements:

- Device flow is disabled by default for clients unless the client registration
  explicitly allows it.
- User codes are short-lived, single-use, high entropy for the displayed length,
  and shown only with clear client/resource/tenant context.
- The verification page requires explicit confirmation after login; entering a
  code alone is not consent.
- The polling interval, `slow_down`, `authorization_pending`, `access_denied`,
  and `expired_token` states follow RFC 8628 semantics.
- Device-flow grants are audit-highlighted because they are easier to phish
  than PKCE.
- Device-flow access to high-risk scopes such as admin, destructive operations,
  bulk export, or future privileged room/source mutation should be disabled
  until a security review approves it.

Device flow is appropriate for SSH, containers, remote VMs, and no-browser
clients. It should not be the default for a developer laptop or browser UI.

## Data Model Additions

Do not overload the existing MCP client-credentials table without separating
client classes. User-delegated auth needs durable records for:

- Public clients: `client_id`, tenant, display name, client type, allowed
  redirect URIs, allowed grant types, allowed resources, allowed scopes, and
  policy flags.
- Authorization codes: hashed code reference, user, tenant, client, redirect
  URI, resource, scopes, challenge hash, expiry, and consumed timestamp.
- Device grants: hashed device code, displayed user code hash, verification URI
  state, client, resource, scopes, polling interval, expiry, approval user,
  denial reason, and completion timestamp.
- Refresh grants: hashed refresh token family, user, client, tenant, resource,
  scopes, rotation counter, last-used metadata, revoked timestamp, and revoke
  reason.
- User sessions and consent grants: enough to show, revoke, and audit who gave
  which client which delegated access.

Every table should avoid storing raw bearer tokens, raw authorization codes, raw
device codes, or raw refresh tokens. Store hashes or opaque handles only.

## API Surface

Add user-delegated auth as a separate surface from MCP client credentials:

- `GET /.well-known/oauth-authorization-server` or equivalent RFC 8414 metadata.
- `GET /api/v1/oauth/authorize` for browser/PKCE authorization.
- `POST /api/v1/oauth/token` for authorization-code, refresh-token, and device
  polling exchanges.
- `POST /api/v1/oauth/device_authorization` for device-code startup.
- `POST /api/v1/oauth/revoke` for refresh-token and grant revocation.
- `GET /api/v1/oauth/grants` and revoke endpoints for the admin/user grant
  management UI.

The existing `POST /api/v1/memory/mcp/oauth/token` can remain as the
service-client compatibility endpoint while the codebase moves toward a shared
authorization server module. Do not silently change existing MCP client
credential behavior during the user-delegated design phase.

## Scope And Resource Rules

The SAR-983 resource contract remains authoritative:

- Tokens are minted for a canonical Palace resource.
- Tokens presented to another resource fail closed.
- MCP clients send and validate the `resource` parameter.
- Scope challenge behavior should guide clients toward the least scopes needed
  for the current operation.

User-delegated flows should start with a narrow scope set:

- Read-only context and memory retrieval.
- Capture extension write/read-job scopes where the user explicitly installs or
  authorizes the extension.
- Optional future Palace UI scopes only after SAR-986 capability checks exist.

Do not allow user-delegated tokens to inherit broad legacy API-key powers.

## Migration Plan

1. Finish the service-client foundation:
   - SAR-984 tenant-safe client identity and resource-bound tokens.
   - SAR-985 shared scope catalog.
   - SAR-986 shared `AuthContext` and capability checks.
2. Add authorization-server metadata and public-client registration models.
3. Implement PKCE for browser/admin UI and local native clients.
4. Add user session and consent grant management.
5. Add refresh-token rotation and revocation.
6. Add device flow as an explicit fallback for no-browser/no-loopback clients.
7. Move selected browser extension and Codex/CLI clients from API-key or
   service-client credentials to user-delegated auth.
8. Run security review before enabling high-risk scopes or retiring broad
   tenant API-key defaults.

## Verification Expectations For Follow-Ups

Future PKCE implementation should prove:

- Missing PKCE challenge fails.
- `plain` challenge method fails unless a deliberate compatibility decision is
  made.
- Wrong verifier fails.
- Reused authorization code fails.
- Wrong redirect URI fails.
- Wrong client, tenant, issuer, or resource fails.
- Expired code fails.
- Scope escalation at token exchange fails.
- Refresh-token reuse or replay is detected and revokes the affected token
  family.

Future device-flow implementation should prove:

- Expired user/device codes fail.
- Polling before approval returns `authorization_pending`.
- Excessive polling returns `slow_down`.
- Denied grants return `access_denied`.
- User-code reuse fails.
- Device flow is unavailable for clients or scopes that policy disallows.
- Audit entries distinguish PKCE, device, refresh, and service-client grants.

## Follow-Up Task Recommendations

Keep SAR-989 as the design artifact. After the service-client foundation lands,
create or promote implementation tasks in this order:

1. Add public-client registration, redirect URI policy, and authorization-server
   metadata for user-delegated OAuth.
2. Implement Authorization Code + PKCE for local/browser public clients.
3. Add consent, user grant management, refresh-token rotation, and revocation.
4. Add Device Authorization Grant as an explicitly enabled fallback.
5. Migrate browser extension and selected Codex/CLI clients to user-delegated
   auth behind feature flags.

## Caveats

- This document intentionally does not mutate runtime auth behavior.
- The current MCP draft authorization spec is still a draft; concrete
  implementation should anchor on stable RFCs where possible and re-check the
  MCP spec before coding.
- This plan assumes Palace remains its own phase-one authorization server. If
  Palace delegates to an external IdP, the redirect, consent, refresh, and
  revocation model should be revised.
- Firecrawl was available for source discovery, but one broad web query returned
  unrelated scraped content alongside the MCP spec; conclusions above are based
  on the official RFCs, MCP spec, and current Palace repo evidence.
