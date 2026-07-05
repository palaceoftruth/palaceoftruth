# Palace OAuth Scope Catalog

Date: 2026-07-05

Palace OAuth and MCP client registration use the shared backend catalog in
`backend/app/mcp_scopes.py`. Control Tower reads that catalog from
`GET /api/v1/palace/mcp-clients`, and protected-resource metadata exposes the
same values from `GET /.well-known/oauth-protected-resource`.

## Supported Scope Combinations

Use the narrowest combination that matches the client:

| Client | Scopes |
| --- | --- |
| Read-only MCP client | `read destructive_prohibited` |
| General memory writer | `read write destructive_prohibited` |
| Agent memory writer | `read write write:agent destructive_prohibited` |
| Workspace memory writer | `read write write:workspace destructive_prohibited` |
| Session checkpoint writer | `read write write:session destructive_prohibited` |
| Local admin maintenance client | `read write admin local_only destructive_prohibited` |
| Browser capture extension | `capture:write capture:job:read destructive_prohibited` |

`local_only` and `destructive_prohibited` are guardrail flags, not ordinary data
permissions. They should constrain how a client acts even when broader read,
write, or admin scopes are present.

## Token Snippet Shape

Generated Control Tower snippets must keep secrets one-time only. The token
command can include selected scopes, but it must prompt for the client secret
instead of persisting it:

```bash
read -rsp 'Palace MCP client secret: ' PALACEOFTRUTH_MCP_CLIENT_SECRET; echo
curl -fsS -X POST "$PALACE_TOKEN_URL" \
  -d grant_type=client_credentials \
  --data-urlencode client_id='tenant-a:codex-remote' \
  "--data-urlencode client_secret=${PALACEOFTRUTH_MCP_CLIENT_SECRET}" \
  --data-urlencode scope='read write write:workspace destructive_prohibited' \
  --data-urlencode resource='https://api.palace.sarvent.cloud/mcp'
```

Unsupported scopes must fail closed during client registration, token issuance,
bearer validation, and API-key MCP scope-header validation.
