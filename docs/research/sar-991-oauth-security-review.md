# SAR-991 OAuth Security Review Before API-Key Retirement

Date: 2026-07-06

## Executive Decision

Recommendation: no-go for broad API-key retirement until the P1 blockers below
are fixed or explicitly accepted.

The current OAuth migration is safe to keep staged with legacy API-key fallback
enabled. I found no obvious P0 auth bypass in the reviewed code, and focused
auth tests pass, but API-key retirement would currently remove the fallback
before resource-boundary, revocation, and audit gaps are closed.

Required blockers filed from this review:

- SAR-1002: Tighten OAuth resource enforcement before API-key retirement.
- SAR-1003: Add OAuth token lifecycle audit before API-key retirement.
- SAR-1004: Scope OAuth revocation to the caller tenant.

## Scope Reviewed

This review covered the Palace OAuth service-client migration surfaces needed
before disabling broad tenant API keys:

- Token generation, hashing, expiry, revocation, and introspection.
- Resource and audience handling for MCP and REST API routes.
- Scope enforcement across MCP tools, memory routes, capture routes, Palace
  operations, and non-memory REST APIs.
- Browser/admin token posture and CORS implications for future PKCE work.
- Audit coverage for token mint, use, denial, revoke, fallback, and rollout
  smoke auth mode.
- Helm/runtime controls for OAuth-first MCP deployment.

The review was read-only except for creating central follow-up tasks and this
documentation artifact. No production data deletion or destructive data mutation
was required.

## Current Migration State

The stale blockers named in SAR-991 are mostly resolved by the landed OAuth
series:

- SAR-984 added tenant-safe, resource-bound token issuance.
- SAR-985 added the shared scope catalog.
- SAR-986 added `AuthContext` and capability helpers.
- SAR-987 was decomposed and completed through SAR-999, SAR-1001, and SAR-1000.
- SAR-988 added protected-resource metadata, authorization-server metadata,
  introspection, and `WWW-Authenticate` discovery hints.
- SAR-990 added Helm/runtime controls for OAuth-first MCP and smoke behavior.

The remaining question is not whether OAuth exists; it is whether the fallback
can be disabled safely.

## Findings

### P1: REST OAuth Resource Boundary Is Too Permissive

`backend/app/auth.py` still accepts null-resource bearer tokens and permits MCP
resource tokens on `/api/v1` REST routes. The code comment says legacy
null-resource tokens should remain valid only for MCP, but `_resource_matches_token`
returns true for `token_resource is None`. For API paths, `_expected_token_resources`
adds both the MCP resource and the API resource, so a token minted for MCP can
also satisfy non-memory API dependencies.

Evidence:

- `backend/app/auth.py:111` builds expected resources.
- `backend/app/auth.py:121` accepts null-resource tokens.
- `backend/tests/test_route_capability_auth.py:169` proves wrong arbitrary
  resource is rejected, but the default accepted test token resource is MCP.

Blocker: SAR-1002.

### P1: OAuth Revocation Is Not Tenant Scoped

`POST /api/v1/memory/mcp/oauth/revoke` authenticates the caller, then updates
`mcp_oauth_access_tokens` by `token_hash` only. Introspection correctly filters
by caller tenant, so revoke should follow the same tenant/client boundary while
remaining idempotent and non-disclosing.

Evidence:

- `backend/app/api/mcp_oauth.py:221` defines revoke.
- `backend/app/api/mcp_oauth.py:236` updates only by `token_hash`.
- `backend/app/api/mcp_oauth.py:273` scopes introspection by tenant.

Blocker: SAR-1004.

### P1: Token Lifecycle Audit Is Incomplete For Retirement

MCP tool success, error, and denial audit exists, and browser-extension token
issue is audited. OAuth token mint, token revoke, introspection denial, and
fallback/auth-mode decisions do not have complete secret-safe audit events.
That is not a P0 for staged OAuth, but it is a P1 before retiring broad API
keys because operators need to prove which credential path is actually being
used and denied.

Evidence:

- `backend/app/mcp_server.py:1522` records MCP tool audit events.
- `backend/app/api/palace.py:416` audits browser-extension token issue.
- `backend/app/api/mcp_oauth.py:161` token mint does not write a token-lifecycle
  audit event.
- `backend/app/api/mcp_oauth.py:221` revoke does not write a token-lifecycle
  audit event.

Blocker: SAR-1003.

### Conditional P1: Browser/Admin Still Uses Broad API Key Storage

The frontend still reads a broad API key from local storage and sends it as
`X-API-Key`. This is acceptable only if the next retirement slice is explicitly
MCP-runtime-only. Broad Palace API-key retirement for browser/admin flows needs
PKCE or another browser-safe auth path first.

Evidence:

- `frontend/src/api/client.ts:43` sends `X-API-Key`.
- `frontend/src/pages/Settings.tsx:37` uses local storage.
- `backend/app/main.py:158` rejects wildcard CORS origins, which helps but
  does not make browser local storage a retirement-ready auth model.

## Positive Controls

- Client secrets and bearer tokens use `secrets.token_urlsafe(48)` and are
  hash-stored.
- Token expiry and revocation are checked on use.
- MCP HTTP auth validates bearer credentials through `/memory/whoami` with
  expected MCP resource and denies tenant mismatch.
- MCP operations check both adapter credentials and inbound caller scopes.
- Helm can render OAuth-only MCP/smoke pods without mounting the legacy
  `PALACEOFTRUTH_API_KEY` environment variable.

## Verification Evidence

Commands run:

```bash
python3 /Users/asarver/.codex/project-manager/task_pool_ops.py claim-next --automation-id dotodo-palace-of-truth --run-thread-id dotodo-palace-of-truth-2026-07-06T03-18-31Z
python3 /Users/asarver/.codex/project-manager/task_pool_ops.py list --automation-id dotodo-palace-of-truth --status in_progress
python3 /Users/asarver/.codex/project-manager/task_pool_ops.py list --automation-id dotodo-palace-of-truth --status in_review
gh pr list --state open --json number,title,headRefName,baseRefName,isDraft,mergeStateStatus,updatedAt,url
uv pip install pytest pytest-asyncio
.venv/bin/python -m pytest tests/test_mcp_oauth.py tests/test_auth.py tests/test_route_capability_auth.py -q
helm template palace-test chart --set mcp.oauthClientSecretKey=MCP_CLIENT_SECRET --set mcp.oauthTokenUrl=https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token --set mcp.oauthResource=https://mcp.palace.sarvent.cloud/mcp --set memoryRolloutSmoke.expectedAuthMode=mcp_oauth --set mcp.legacyApiKeyAuthEnabled=false | rg -n "PALACEOFTRUTH_API_KEY|PALACEOFTRUTH_MCP_OAUTH|expected-auth-mode|mcp_oauth"
helm template palace-test chart --set mcp.oauthClientSecretKey=MCP_CLIENT_SECRET --set mcp.oauthTokenUrl=https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token --set mcp.oauthResource=https://mcp.palace.sarvent.cloud/mcp --set memoryRolloutSmoke.expectedAuthMode=mcp_oauth --set mcp.legacyApiKeyAuthEnabled=true | rg -n "PALACEOFTRUTH_API_KEY|PALACEOFTRUTH_MCP_OAUTH|expected-auth-mode|mcp_oauth"
```

Results:

- Focused backend auth/OAuth route tests: 45 passed.
- OAuth-only Helm render: `PALACEOFTRUTH_API_KEY` omitted; OAuth envs present.
- Legacy Helm render: `PALACEOFTRUTH_API_KEY` present; OAuth envs present.
- Parallel safety check: only SAR-991 was active; no open PRs were present.

Initial validation caveat: the first `uv run pytest` used the contaminated
global Python 3.14 pytest stack and failed during collection with a pydantic-core
mismatch. The passing command above used the repo-local Python 3.12 venv.

## Residual Risks

- Expired OAuth access tokens are rejected on use, but there is no token-retention
  cleanup job in the reviewed paths. This is hygiene, not a retirement blocker.
- MCP non-write operations currently map broadly to `read`. That may be the
  intended service-client model, but a future finer-grained scope split should
  be explicit rather than inferred.
- Browser/admin API-key retirement remains out of scope until user-delegated
  auth or an equivalent browser-safe model exists.

## Go/No-Go Recommendation

Do not disable broad tenant API keys yet. Complete SAR-1002, SAR-1003, and
SAR-1004 first, then re-run API-key retirement checks. After those land, SAR-992
can proceed as an MCP/runtime-only retirement gate unless browser/admin
retirement is explicitly included with a separate auth plan.

Rollback remains non-destructive: keep or re-enable `mcp.legacyApiKeyAuthEnabled`
and avoid revoking live API keys until OAuth-only runtime and smoke checks pass.
