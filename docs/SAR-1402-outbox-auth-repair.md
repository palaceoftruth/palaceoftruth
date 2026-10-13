# PR556 R1/R2 source repair — independent rereview required

Reviewed predecessor: 9646dc0cbcafb48d3b6c0caef00e0b7f3d8b4691. No production change or Hermes core edit.

## Correction to predecessor claims

API-key authentication creates AuthContext.subject_id from the stored API-key row ID. Its agent_scope_key is null. The predecessor's requirement that an API-key principal equal an agent name was unusable against the actual backend. The successful old TLS fixture fabricated that agent name and is withdrawn as auth-contract evidence. Source authentication is now tested through actual token issuance/auth/whoami/POST/receipt routes, not a fabricated identity endpoint.

The prior source was API-key-only. That did not support the actual OAuth-only profile. The repaired credential path implements the existing plugin client_credentials protocol and ephemeral expiry-based reacquisition. No OAuth-to-API-key fallback, broader grant, token file or refresh-token database is introduced.

## Identity versus intent

Binding schema now separates:
- origin: exact intended HTTPS backend authority;
- tenant_id: literal expected tenant UUID;
- credential: mode plus server-owned credential/client UUID; OAuth also binds registered client_key;
- scope: exact intended agent/workspace target or submitted-session policy.

API-key identity comes from the additive whoami.credential_id field, derived ONLY from authenticated AuthContext.subject_id. It is not derived from X-Palace-Agent-Scope-Key, caller-supplied names or tenant alone. OAuth checks server-owned client ID/key; existing whoami.mcp_client_id remains a valid server-issued ID when the additive field is absent. OAuth agent scope is additionally matched to server agent_scope_key. Actual granted read/write/write:<type> scopes must cover the intended action; caller scope headers remain narrowing intent, never identity or authority.

The nullable additive backend field is omitted when legacy/test contexts have no authenticated subject, preserving those old response shapes. API-key recovery fails closed against an old backend that does not supply its server credential ID. OAuth can use existing registered client ID metadata. This is source compatibility, not a claim the backend change has been deployed.

Old v1 bindings with principal='agent-name' are rejected without automatic migration or reattribution. Existing records are not deleted or retagged; an explicit offline owner migration decision would be needed if such records existed. The reviewed production profile had outbox disabled; no production record is inspected or modified here.

## Existing supported credential path

The shared client_credentials_form helper is used by the normal provider's existing minting method AND the finite recovery credential provider. Existing grant_type/client_id/client_secret/scope/resource protocol is unchanged. Normal provider caching remains its existing path; finite recovery caches bearer tokens only in process memory and reacquires from the same bound client after expiry. It does not automatically retry an expired/revoked request with a different auth mode or credential identity.

Standalone source command takes already injected current PALACEOFTRUTH_MCP_OAUTH_* / PALACEOFTRUTH_MCP_CLIENT_* values (legacy SECONDBRAIN aliases supported). It never reads a profile dotenv. The documented provider cli.py extension adds:
    hermes palaceoftruth recover-outbox --directory <profile-leaf> --binding-file <non-secret-binding> --timeout 10 --max-jobs 10

That path uses public hermes_constants.get_hermes_home and agent.secret_scope.get_secret, so an active routed profile cannot silently use another profile's process-environment secret. Discovery/argparse/handler execution is tested with the actual official discover_plugin_cli_commands API and fixture profile. It is finite, not a daemon, scheduler or service installer. No actual profile command is executed.

OAuth token endpoint/resource must match the bound origin/API. Exact legacy same-origin /mcp resource intent follows the existing provider rule into /api/v1; arbitrary issuer/resource retargets fail. Requested recovery scopes are narrowed to the required read/write/scope-type set, not expanded to admin/tenant-wide authority. API-key mode can use the current selected API-key path but is never a fallback for missing OAuth credentials.

Token acquisition uses a bounded HTTPS child with verified TLS, no proxy, no redirects/downgrade, capped response and one common deadline. Form/headers travel only through process memory/stdin, not argv/logs/outbox. A newly acquired/refreshed authorization header forces fresh whoami validation BEFORE POST or canonical receipt read. Credential/client rebinding after refresh is rejected before writing. Credential denial at token/identity/POST/receipt phases preserves pending FIFO records and stops the successor.

## Submission-time freeze

Validated non-secret policy is frozen at initialize (or the first supported callback snapshot in direct-call fixtures) under the existing lifecycle lock: origin, tenant, credential mode/ID/key, scope intent, profile home and local leaf. Each callback carries a deep copy of this policy with its existing immutable author/session/context snapshot. Delayed workers do not inspect a newly changed configuration to choose their target. Old accepted work retains the old binding; future mismatched submissions fail before native admission. No secret/token is included in that policy.

## Actual exercised contracts

New integration fixtures use real backend verify_api_key/verify_memory_auth, registered-client token issuance, require_mcp_scope/admission, whoami response, canonical memory acceptance and receipt service. Only SQL/queue persistence is mocked, using the actual models and committed-only readback. JSONB fixture results are decoded into lists like the driver, not used to bypass auth validation. Real loopback TLS bridges those actual ASGI routes to actual recovery CLI/HTTP child execution.

Coverage includes API-key null agent scope with server credential ID; OAuth-only success; expiry, token revocation, client revocation, tenant/credential mismatch, stored-grant denial, server agent-scope denial, denial after POST before receipt, pending FIFO preservation, ephemeral refresh with unchanged client ID and refreshed client-ID mismatch rejection. Current authorization is not inferred from config presence. No live tenant/client/grant/token validity or deployed Postgres proof is claimed.

Historical strict script/workflow/N1 tests and native queue/drain/generation/flush/shutdown remain unchanged. Atomic snapshot gains only non-secret outbox policy. Quotas, commit/readback, FIFO and direct bounded remote checkpoint are retained. Full host-global acceptance remains MANDATORY BLOCKED. Source review/actual test receipts are not merge, deployment, rollout, live canary or Done approval.
