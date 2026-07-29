# SAR-992 API-Key Retirement Readiness Evidence

## 2026-07-29 Post-Smoke-Repair Update

SAR-1276 is deployed and healthy on `k3s-lab/palace-sarvent`: chart
`0.1.528+b191ea6bbc11`, app `745ca017`, Helm revision 277, and Flux revision
`231725bf99460f5e256de79bba9a3cbf652779b0`. The repaired rollout hook is
terminally `Ready=True` / `UpgradeSucceeded`; MCP is 2/2 Ready and the
compatibility fallback remains enabled.

The fresh secret-safe 30-day readiness report still blocks retirement:

```json
{
  "ready_for_oauth_only_mcp": false,
  "active_key_count": 2,
  "recent_api_key_use_detected": true,
  "active_oauth_client_count": 10,
  "recent_oauth_activity_detected": false
}
```

Read-only audit correlation identified a current legacy caller without exposing
key material. Barbara's `primary-options-trade-finder` cron `7a27de7b6312`
used the remote MCP API-key path at 2026-07-29 13:06 UTC. The server audit
recorded `http_client_id=api-key` with `agent_scope_key=barbara`; ingress source
IP remained the proxy loopback and was not treated as caller attribution.

The report also had correctness gaps that made the negative OAuth result
ambiguous: it loaded only the newest 50 tenant audit rows before filtering, did
not require a specific client/resource/scopes/version, and did not surface the
legacy MCP caller events already present in audit metadata. The SAR-992
readiness implementation now requires explicit runtime expectations, uses
purpose-specific lookback queries, reports recent legacy MCP activity, and
stamps the chart app version into MCP adapter audit rows. The same Helm value
now controls both the mounted outbound fallback credential and acceptance of
caller-supplied `X-API-Key` credentials at the public MCP middleware.

Do not disable fallback from this state. First deploy the repaired report with
fallback retained, migrate the reviewed Barbara caller to OAuth, capture a
passing explicit-runtime report for the chosen lookback, and obtain approval
for the exact Flux value change. Keep both tenant keys unchanged as
human-controlled break-glass.

## Original 2026-07-07 Evidence

Captured: 2026-07-07 02:18 UTC

## Summary

SAR-992 is past the MCP OAuth runtime staging gate, but it is not yet ready for
live MCP API-key fallback disablement from a DOTODO implementation session.

The deployed `palace-sarvent` release is healthy on chart `0.1.446` and app
version `103f701a`. The MCP runtime now has OAuth client-credentials
configuration, the default MCP write scope is `agent/andrew`, and the built-in
memory rollout smoke passed with `auth_mode=mcp_oauth` for tenant `default` and
client `helm-mcp`.

Legacy API-key fallback is still enabled on purpose. The remaining gate is a
human-controlled readiness check with the admin secret, followed by a separate
deployment change to set `mcp.legacyApiKeyAuthEnabled=false` for the target
runtime if the readiness report passes. This evidence is read-only and
secret-safe; it does not print secret values, inspect secret data, revoke keys,
rotate keys, delete keys, or mutate deployment state.

## Read-Only Evidence

Commands were run against `k3s-lab` namespace `palace-sarvent`.

### Deployed Version And GitOps State

```text
kubectl --context k3s-lab -n palace-sarvent get helmrelease palace-sarvent ...
0.1.446    103f701a    True    UpgradeSucceeded

kubectl --context k3s-lab -n flux-system get gitrepository flux-control ...
main@sha1:7d4ef20e713033bc0f783680dd90599562a392a2

kubectl --context k3s-lab -n flux-system get kustomization k3s-lab-apps ...
main@sha1:7d4ef20e713033bc0f783680dd90599562a392a2    True
```

Public application checks:

```text
GET https://api.palace.sarvent.cloud/api/v1/version -> {"version":"103f701a"}
GET https://api.palace.sarvent.cloud/api/v1/ready -> status ok, database ok, queue ok
```

### Helm Values Shape

The live HelmRelease has OAuth runtime values staged while keeping legacy
fallback enabled:

```json
{
  "apiBaseUrl": "https://api.palace.sarvent.cloud",
  "defaultScopeKey": "andrew",
  "defaultScopeType": "agent",
  "legacyApiKeyAuthEnabled": true,
  "oauthAudience": "https://api.palace.sarvent.cloud/api/v1",
  "oauthClientSecretKey": "MCP_CLIENT_SECRET",
  "oauthResource": "https://api.palace.sarvent.cloud/api/v1",
  "oauthTokenUrl": "https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token"
}
```

The live rollout smoke values are enabled and assert OAuth identity:

```json
{
  "enabled": true,
  "expectedAuthMode": "mcp_oauth",
  "expectedClientKey": "helm-mcp",
  "expectedScopes": ["read", "write"],
  "expectedTenantId": "default",
  "requestTimeoutSeconds": 60
}
```

### MCP Runtime Environment Shape

The live MCP deployment has two ready replicas and exposes these environment
variable names:

```text
PALACEOFTRUTH_API_BASE_URL
PALACEOFTRUTH_API_KEY
PALACEOFTRUTH_MCP_OAUTH_CLIENT_SECRET
PALACEOFTRUTH_MCP_OAUTH_TOKEN_URL
PALACEOFTRUTH_MCP_OAUTH_RESOURCE
PALACEOFTRUTH_MCP_OAUTH_AUDIENCE
PALACEOFTRUTH_MCP_CLIENT_KEY
PALACEOFTRUTH_MCP_CLIENT_NAME
PALACEOFTRUTH_MCP_CLIENT_SCOPES
PALACEOFTRUTH_DEFAULT_SCOPE_TYPE
PALACEOFTRUTH_DEFAULT_SCOPE_KEY
```

This confirms OAuth configuration is staged. `PALACEOFTRUTH_API_KEY` is still
present because fallback remains enabled until the readiness report and
deployment flip are explicitly approved.

### Rollout Smoke Evidence

The Helm hook job completed successfully:

```text
palace-sarvent-memory-smoke-103f701a    Complete    1/1    52s
```

The job log reported:

```text
status=passed
tenant_identity_expectations=passed
tenant_id=default
memory_write=passed
memory_job_completion=passed
memory_jobs_listing=passed
sentinel_valkey=passed
mcp_health=passed with HTTP 401
kubernetes_alerts=passed with alert_count=0
```

### Public Fail-Closed Check

Unauthenticated MCP access fails closed and advertises protected-resource
metadata:

```text
GET https://mcp.palace.sarvent.cloud/mcp -> HTTP 401
WWW-Authenticate: Bearer ... resource_metadata="https://mcp.palace.sarvent.cloud/.well-known/oauth-protected-resource/mcp"
```

## Decision

Do not set `mcp.legacyApiKeyAuthEnabled=false` from this implementation PR.

The replacement MCP OAuth runtime path is now staged and smoke-tested, but the
admin readiness endpoint still needs to be evaluated with the human-held admin
secret to prove there is no recent active tenant API-key use for the chosen
lookback window. The actual fallback-disablement change belongs in the
deployment source of truth after that readiness report passes.

## Next Safe Steps

1. Use the human-held admin secret to capture the read-only readiness report for
   tenant `default`.
2. Confirm the report shows `ready_for_oauth_only_mcp=true`, recent MCP OAuth
   activity, and no active tenant API-key use within the chosen lookback window.
3. If the report passes, create the deployment change that sets
   `mcp.legacyApiKeyAuthEnabled=false` for `palace-sarvent` while keeping the
   OAuth client values, default scope, and rollout-smoke assertions enabled.
4. Deploy and verify the MCP deployment and memory-smoke job no longer mount
   `PALACEOFTRUTH_API_KEY`, a caller-supplied retained key receives `401` from
   the public MCP endpoint, and `/memory/whoami` plus smoke evidence still show
   `auth_mode=mcp_oauth`.
5. Keep tenant API keys as human-controlled break-glass unless the human
   separately approves rotation, revocation, or deletion for that tenant.

## Non-Goals Preserved

- No production API keys were printed, rotated, revoked, or deleted.
- No Kubernetes, Flux, Helm, or secret resources were mutated.
- Browser/admin API-key retirement remains out of scope for this MCP runtime
  slice.
