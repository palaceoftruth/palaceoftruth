# SAR-984 OAuth Client Resource Migration

Date: 2026-07-05

## Summary

Palace MCP OAuth client-credentials token issuance is now tenant-safe and
resource-bound.

New token requests must include the protected MCP resource:

```bash
curl -fsS -X POST https://api.palace.sarvent.cloud/api/v1/memory/mcp/oauth/token \
  -d grant_type=client_credentials \
  --data-urlencode client_id='codex-remote' \
  --data-urlencode client_secret="${PALACEOFTRUTH_MCP_CLIENT_SECRET}" \
  --data-urlencode scope='read write' \
  --data-urlencode resource='https://api.palace.sarvent.cloud/mcp'
```

Use the value advertised by `/.well-known/oauth-protected-resource` for the
target environment. Tokens minted for a different resource are rejected by MCP
bearer validation.

## Tenant-Safe Client IDs

Existing clients with a globally unambiguous `client_key` can keep using that
bare key during migration. If two tenants register the same `client_key`, token
issuance fails closed for the bare key. Use a tenant-qualified client id in that
case:

```text
<tenant_id>:<client_key>
```

For example:

```bash
--data-urlencode client_id='tenant-a:codex-remote'
```

## Legacy Tokens

Bearer tokens minted before this migration do not have persisted resource
metadata. They remain valid only through the existing MCP bearer validation path
until they expire or are revoked. Newly minted tokens persist `resource` and
return it in the token response.

`/api/v1/memory/whoami` now returns non-secret OAuth metadata for validation and
debugging: client id/key, granted scopes, resource/audience, and a short
non-reversible token-hash prefix.
