# SAR-992 API-Key Retirement Readiness Evidence

Captured: 2026-07-06 20:45 UTC

## Summary

SAR-992 is not ready for live MCP API-key fallback disablement yet.

The deployed `palace-sarvent` release is healthy on chart `0.1.443` and app
version `20b36667`, but the live MCP runtime still has only legacy API-key
authentication wired into its pod environment. No MCP OAuth client secret,
token URL, resource, audience, or rollout-smoke OAuth assertions are currently
configured in the HelmRelease values.

This evidence is intentionally read-only and secret-safe. It records whether
the required replacement OAuth path is configured, but it does not print secret
values, inspect secret data, revoke keys, rotate keys, delete keys, or mutate
deployment state.

## Read-Only Evidence

Commands were run against `k3s-lab` namespace `palace-sarvent`.

### Deployed Version

```text
kubectl --context k3s-lab -n palace-sarvent get helmrelease palace-sarvent ...
0.1.443    20b36667    True
```

Public application checks:

```text
GET https://api.palace.sarvent.cloud/api/v1/version -> {"version":"20b36667"}
GET https://api.palace.sarvent.cloud/api/v1/ready -> status ok, database ok, queue ok
```

### Helm Values Shape

The live HelmRelease has only MCP client scope values in `.spec.values.mcp`:

```json
{"clientScopes":"read,write,write:agent,write:workspace,write:session,admin,local_only,destructive_prohibited"}
```

The live rollout smoke values are disabled:

```json
{"enabled":false}
```

This means the deployed release has not yet staged:

- `mcp.oauthClientSecretKey`
- `mcp.oauthTokenUrl`
- `mcp.oauthResource`
- `mcp.oauthAudience`
- `memoryRolloutSmoke.expectedAuthMode=mcp_oauth`
- `memoryRolloutSmoke.expectedClientKey`
- `memoryRolloutSmoke.expectedScopes`

### MCP Runtime Environment Shape

The live MCP deployment exposes these environment variable names:

```text
PALACEOFTRUTH_API_BASE_URL
PALACEOFTRUTH_API_KEY
PALACEOFTRUTH_MCP_CLIENT_KEY
PALACEOFTRUTH_MCP_CLIENT_NAME
PALACEOFTRUTH_MCP_CLIENT_SCOPES
```

The runtime does not expose these OAuth-only replacement variables:

```text
PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET
PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL
PALACEOFTRUTH_MCP_OAUTH_RESOURCE
PALACEOFTRUTH_MCP_OAUTH_AUDIENCE
PALACEOFTRUTH_MCP_BEARER_TOKEN
```

The MCP pods were running and ready:

```text
palace-sarvent-mcp-5464459fd5-5twk6    Running    true    0
palace-sarvent-mcp-5464459fd5-gxlrk    Running    true    0
```

### Public Fail-Closed Checks

Unauthenticated MCP access fails closed and advertises protected-resource
metadata:

```text
GET https://mcp.palace.sarvent.cloud/mcp -> HTTP 401
WWW-Authenticate: Bearer ... resource_metadata="https://mcp.palace.sarvent.cloud/.well-known/oauth-protected-resource/mcp"
```

The admin readiness endpoint is deployed and fails closed without the admin
secret:

```text
GET https://api.palace.sarvent.cloud/api/v1/admin/tenants/default/api-key-retirement-readiness?lookback_days=30 -> HTTP 403
```

## Decision

Do not set `mcp.legacyApiKeyAuthEnabled=false` for `palace-sarvent` yet.

The replacement MCP OAuth runtime path has not been staged in live Helm values,
the rollout smoke is disabled, and the readiness endpoint cannot be evaluated
without the human-held admin secret. Disabling legacy API-key fallback now would
risk breaking the deployed MCP service before the OAuth client path has proven
itself.

## Next Safe Steps

1. Stage MCP OAuth client credentials and token/resource/audience values in the
   deployment source of truth without removing the API-key fallback.
2. Enable the rollout smoke with `expectedAuthMode=mcp_oauth`, the expected
   MCP client key, tenant, and scopes.
3. Deploy and verify MCP runtime and smoke evidence show OAuth activity.
4. Use the admin readiness endpoint with the human-held admin secret to confirm
   `ready_for_oauth_only_mcp=true` for the target tenant and lookback window.
5. Only after those checks pass, set `mcp.legacyApiKeyAuthEnabled=false` for
   the target runtime.
6. Keep tenant API keys as human-controlled break-glass unless the human
   separately approves rotation, revocation, or deletion for that tenant.

## Non-Goals Preserved

- No production API keys were printed, rotated, revoked, or deleted.
- No Kubernetes, Flux, Helm, or secret resources were mutated.
- Browser/admin API-key retirement remains out of scope for this MCP runtime
  slice.
