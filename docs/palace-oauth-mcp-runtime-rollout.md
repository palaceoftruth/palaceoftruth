# Palace OAuth MCP Runtime Rollout

Use this runbook when moving deployed Palace MCP runtimes from broad tenant
`API_KEY` fallback to OAuth-first service-client authentication.

## OAuth-First Values

Keep legacy API-key fallback enabled while adding OAuth values. When bearer or
OAuth client credentials are configured, MCP runtimes and the rollout smoke use
OAuth first so the staged smoke verifies the replacement path instead of the
broad API key path:

These are deployment-owned values. Keep the environment-specific overlay in the
deployment/config repo, not in this portable chart repo.

Set `mcp.apiBaseUrl` to the same API origin used by `oauthResource`; otherwise
the runtime may mint a token for one resource and call a different backend host.

```yaml
externalSecrets:
  mcpOauthClientSecretProperty: mcp-oauth-client-secret

mcp:
  apiBaseUrl: https://api.palace.sarvent.cloud
  legacyApiKeyAuthEnabled: true
  oauthClientSecretKey: MCP_CLIENT_SECRET
  oauthTokenUrl: https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token
  oauthResource: https://api.palace.sarvent.cloud/api/v1
  oauthAudience: https://api.palace.sarvent.cloud/api/v1
  # Set per runtime, for example agent/karen or agent/andrew. Explicit MCP tool
  # scope_type/scope_key arguments still override these defaults.
  defaultScopeType: agent
  defaultScopeKey: <agent-name>

memoryRolloutSmoke:
  requestTimeoutSeconds: 60
  expectedAuthMode: mcp_oauth
  expectedTenantId: default
  expectedClientKey: helm-mcp
  expectedScopes:
    - read
    - write
```

After smoke verification, capture the tenant readiness report before disabling
fallback:

```bash
curl -fsS \
  -H "X-Admin-Secret: $PALACEOFTRUTH_ADMIN_SECRET" \
  "https://api.palace.sarvent.cloud/api/v1/admin/tenants/default/api-key-retirement-readiness?lookback_days=30"
```

The report is read-only and secret-safe. It must show:

* `ready_for_oauth_only_mcp: true`
* at least one active OAuth MCP client
* recent MCP OAuth client activity from MCP runtime audit events
* no active tenant API key use inside the chosen lookback window
* any retained active API keys marked only as human-controlled break-glass

Current `palace-sarvent` evidence captured for SAR-992 is recorded in
`docs/research/sar-992-api-key-retirement-readiness-evidence.md`. As of
2026-07-07 02:18 UTC, live MCP pods have OAuth client-credentials values, default
scope `agent/andrew`, and a passing `auth_mode=mcp_oauth` rollout smoke while
legacy API-key fallback remains enabled. The next gate is the read-only readiness
report with the human-held admin secret; only disable fallback after that report
passes for the target tenant and lookback window.

Only then set this to stop mounting the broad `API_KEY` into MCP runtime pods
and smoke jobs:

```yaml
mcp:
  legacyApiKeyAuthEnabled: false
```

In OAuth-only mode the MCP Deployment and rollout smoke Job do not mount the
broad `API_KEY` secret. In staged mode they may still mount the fallback secret,
but they authenticate with either
`PALACEOFTRUTH_MCP_BEARER_TOKEN` or client-credentials OAuth using the configured
client key, client secret, token URL, resource, and scopes whenever those values
are present.

## Raw REST Scripts

Raw REST callers must request the API resource, not the MCP resource. Discover
the exact resource identifier instead of guessing an `audience` value:

```bash
PALACE_API_BASE=https://api.palace.sarvent.cloud
PALACE_API_RESOURCE=$(curl -fsS \
  "$PALACE_API_BASE/.well-known/oauth-protected-resource/api/v1" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["resource"])')

read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET; echo
PALACE_API_BEARER_TOKEN=$(curl -fsS -X POST \
  "$PALACE_API_BASE/api/v1/memory/mcp/oauth/token" \
  -d grant_type=client_credentials \
  --data-urlencode client_id=helm-mcp \
  "--data-urlencode client_secret=${PALACEOFTRUTH_MCP_CLIENT_SECRET}" \
  --data-urlencode scope=read \
  --data-urlencode "resource=${PALACE_API_RESOURCE}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -fsS \
  -H "Authorization: Bearer ${PALACE_API_BEARER_TOKEN}" \
  "$PALACE_API_BASE/api/v1/memory/scopes"
```

The token endpoint form field is named `resource`. The runtime setting
`oauthAudience` is only a compatibility fallback that the official adapter
normalizes into that `resource` field. A `400 invalid_resource` response means
the client already authenticated but the exact resource URI was missing or not
accepted; regenerating the client secret will not repair that mismatch. A bad
client id or secret instead returns `401 invalid_client`.

MCP client registration is create-only. Repeating registration for an existing
`tenant_id` and `client_key` returns `409` without changing the stored secret
hash. Never use registration as an implicit rotation mechanism; credential
rotation must be a separately reviewed workflow that updates the external
secret, rolls the consumers, verifies both REST and MCP canaries, and only then
retires the old credential.

## Verification

Render both modes before rollout:

```bash
helm lint chart
helm template palaceoftruth chart
helm template palaceoftruth chart \
  --set mcp.legacyApiKeyAuthEnabled=false \
  --set mcp.oauthClientSecretKey=MCP_CLIENT_SECRET \
  --set mcp.oauthTokenUrl=https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token \
  --set mcp.oauthResource=https://api.palace.sarvent.cloud/api/v1 \
  --set memoryRolloutSmoke.expectedAuthMode=mcp_oauth \
  --set memoryRolloutSmoke.expectedClientKey=helm-mcp \
  --set memoryRolloutSmoke.expectedScopes[0]=read \
  --set memoryRolloutSmoke.expectedScopes[1]=write
```

The rollout smoke checks `/memory/whoami` and fails when the observed
`auth_mode`, tenant, MCP client key, or required scopes do not match the
expected values.

Set `memoryRolloutSmoke.requestTimeoutSeconds: 60` for OAuth staging. The
SAR-1007 landing proved the OAuth path with a manual 60-second smoke after the
default 10-second request timeout was too short for the write/job path.

## Per-Tenant Retirement Checklist

Run this checklist for each tenant/runtime before changing deployment values:

* Codex MCP uses the repo-owned stdio Palace MCP adapter or an OAuth-capable
  remote MCP profile.
* Hermes and other agent plugins report `auth_mode=mcp_oauth` or another
  approved OAuth mode in `/memory/whoami` and MCP request audit events.
* MCP HTTP smoke uses the expected tenant, client key, and scopes.
* Rollout smoke is configured with `expectedAuthMode=mcp_oauth`,
  `expectedClientKey`, and `expectedScopes`.
* CLI/scripts that still require `PALACEOFTRUTH_API_KEY` are either out of the
  MCP runtime path or explicitly documented as break-glass/manual tools.
* Browser/admin API-key retirement remains out of scope unless a separate
  browser/admin auth plan is approved.
* The readiness endpoint shows recent MCP OAuth client activity and no recent
  active tenant API-key use for the tenant and lookback window.
* Rollback is documented as re-enabling `mcp.legacyApiKeyAuthEnabled=true`.
* Production API keys are not rotated, revoked, or deleted without explicit
  human approval for that tenant/runtime.

## Rollback

Rollback does not require deleting production data, rotating tenant API keys, or
revoking OAuth clients. Re-enable the explicit compatibility fallback and remove
or blank the OAuth credential values for the affected release:

```yaml
mcp:
  legacyApiKeyAuthEnabled: true
  oauthClientSecretKey: ""
  bearerTokenSecretKey: ""
```

If the OAuth token endpoint or client secret is suspected to be bad, leave the
OAuth values in place for later diagnosis and restore the last known-good chart
release or Flux value commit. Rotate or revoke live secrets only after a human
has verified the replacement auth path and approved the secret operation.
