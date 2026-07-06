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

After smoke verification, set this to stop mounting the broad `API_KEY` into MCP
runtime pods and smoke jobs:

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
