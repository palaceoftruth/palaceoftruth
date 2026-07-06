# Palace OAuth MCP Runtime Rollout

Use this runbook when moving deployed Palace MCP runtimes from broad tenant
`API_KEY` fallback to OAuth-first service-client authentication.

## OAuth-First Values

Keep legacy API-key fallback enabled while adding OAuth values. When bearer or
OAuth client credentials are configured, MCP runtimes and the rollout smoke use
OAuth first so the staged smoke verifies the replacement path instead of the
broad API key path:

```yaml
mcp:
  legacyApiKeyAuthEnabled: true
  oauthClientSecretKey: MCP_CLIENT_SECRET
  oauthTokenUrl: https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token
  oauthResource: https://api.palace.sarvent.cloud/api/v1
  oauthAudience: https://api.palace.sarvent.cloud/api/v1

memoryRolloutSmoke:
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
