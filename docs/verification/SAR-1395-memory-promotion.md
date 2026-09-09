# SAR-1395: agent memory promotion

## Contract

An explicitly authorized, contained OAuth agent can copy an existing memory from
its canonical scope to `tenant_shared`. The source stays unchanged. Promotion
requires `memory:promote_shared`, `write`, and `write:agent`; it does not add these
grants to any client by default. Delegated grants, unbound clients, sibling-agent
sources, cross-tenant sources, and invalid or unavailable sources are denied.

The server selects the source content, destination, idempotency key, and
provenance. It applies memory privacy admission before storage. A shared copy is
still agent-authored; promotion does not make it a verified fact. A job records
the promotion time and admission audit. The copy expires at the earlier of the
source validity deadline and its governance verification deadline. Copied metadata records the source's
update time, not a fabricated promotion time. Changes to the source cause a
repeat request to conflict rather than silently return a different payload.

The worker uses a promotion-specific deduplication key for the shared copy.
The body stays unchanged, while the tenant-wide content index cannot collapse
the copy into its private source. Only the server-owned admission record enables
this behavior; caller metadata does not bypass normal content deduplication.

This change adds no database migration. Hermes package version: **1.0.38**.

## Verification matrix

| Check | Evidence | Result |
| --- | --- | --- |
| Explicit grant, binding, and delegated-grant denials | `test_memory_promotion.py` | Automated |
| Source state and privacy admission | `test_memory_promotion.py` | Automated |
| REST auth context and queue contract | `test_memory_promotion.py` | Automated |
| MCP permission gate and fixed endpoint | `test_mcp_server.py` | Automated |
| Hermes schema, invalid IDs, disabled writes, transient failure, repeat calls | `test_hermes_memory_plugin.py` | Automated |
| Admin activation/deactivation, prerequisites and tenant boundary | `test_memory_promotion_activation.py` | Automated |
| Real PostgreSQL concurrent promotion, replay, source preservation, tenant isolation, changed-source conflict, worker completion and stored embeddings | `test_memory_promotion_database.py` | Passed locally; CI database lane |
| Real PostgreSQL reversible activation, preserved credential and read grants | `test_memory_promotion_database.py` | Passed locally; CI database lane |
| Backend regression suite and workflow checks | Existing pytest suites | Required before PR handoff |
| Frontend type compatibility | `npm ci && npm run build` | Passed locally |
| Installed fleet plugin, token grants, worker completion, cross-agent shared recall | Deployment sequence below | Requires deployed candidate and explicit fleet activation |

The local PostgreSQL tests use a disposable migrated database and unique fixture
tenants. They do not delete data. No deployed fleet data or grants were changed.
There is no visual UI change; the frontend change only extends an API type.

## Deployment and rollback

1. Merge the reviewed application PR, then wait for the existing release workflow
   to publish the backend/MCP/worker images and Hermes plugin 1.0.38. The workflow
   builds release images after merge; PR checks do not publish them.
2. Promote the approved image coordinates through the existing Flux deployment
   repo. Confirm the approved environment, release, and installed versions
   before changing deployment.
3. Deploy the backend/MCP/worker before installing plugin 1.0.38 on intended agents.
4. With operator authentication, call
   `PATCH /api/v1/admin/tenants/{tenant_id}/mcp-clients/{client_id}/shared-memory-promotion`
   with `{"enabled": true}` for each intended client. The endpoint requires an
   existing contained agent binding plus `write` and `write:agent`; it does not
   rotate the credential or change read policies.
5. Obtain a fresh OAuth token. If the agent has an explicit requested scope list,
   include `memory:promote_shared`; preserve its existing scopes. Restart the
   agent as needed to refresh its plugin and cached token.
6. Use an approved non-sensitive fixture in the calling agent's canonical scope.
   Call `palace_promote_to_shared` with its memory `entry_id`, poll the returned
   job to completion, and verify shared recall from a second authorized agent.
   Verify source preservation and repeat promotion returning the same job.
   Do not use real private memory as a smoke-test fixture.
7. Disable with the same operator endpoint and `{"enabled": false}`. Current
   token validation checks stored grants on every request, so a token that still
   requests the removed scope is denied. Obtain a fresh reduced-scope token to
   resume the client's other operations. Verify promotion denial.

Rollback removes the new grant and restores the previous plugin and image
coordinates. Shared copies already created remain stored; rollback does not
remove data. No merge, deployment, or fleet activation is performed by this PR
preparation task.
