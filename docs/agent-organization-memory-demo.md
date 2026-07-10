# Agent Organization Memory Demo

Agent Organization Memory is governed memory for agent teams. It lets a lead
agent coordinate specialist agents without turning every private specialist
note into tenant-wide shared context.

Generic vector DB or RAG memory usually answers one question: "what text is
similar to this query?" Palace answers the team question: "which agent-owned
memory scopes is this caller allowed to use, what was searched, what was
denied, and what provenance should the orchestrator preserve?"

## Demo Story

Use this story for docs, sales demos, and MCP client walkthroughs:

1. `security-agent` investigates auth, policy, secret, and abuse-risk findings.
2. `macos-agent` investigates local app, keychain, filesystem, and operator
   workstation findings.
3. `frontend-agent` investigates app UI, accessibility, and browser evidence.
4. Each specialist writes concise findings only to its own `agent/<key>` scope.
5. `agent/orchestrator` requests a governed read across selected specialist
   scopes with an `access_reason`.
6. Palace authorizes same-tenant delegated reads server-side, denies anything
   outside policy, and returns sanitized trace/provenance fields.
7. The orchestrator writes the reviewed synthesis only to `agent/orchestrator`.

The point is not that the orchestrator is omniscient. The point is that the
server decides which specialist memory may be used and leaves an auditable trace
that does not record raw memory bodies or secrets.

## Copy

Use this landing-copy language when positioning the feature:

> Governed memory for agent teams. Specialists keep private scoped memory;
> orchestrators retrieve only the scopes the server authorizes, with provenance
> and sanitized audit traces.

Short version:

> Agent memory with permissions, provenance, and auditability.

Proof points:

- Deny-by-default cross-agent reads.
- Same-tenant delegated policy evaluation on the server.
- Specialist writes stay in `agent/<specialist>` scopes.
- Orchestrator write-back stays in `agent/orchestrator`.
- Denied scopes are reported in trace fields and are not searched.
- Broad-corpus fallback stays off for governed cross-agent recall.
- Returned memories carry source labels such as own-agent, delegated specialist,
  workspace, tenant-shared, or broad corpus.
- MCP audit summaries are sanitized and do not include raw memory bodies,
  search queries, API keys, key hashes, or webhook URLs.

## MCP Dry-Run Script

Generate copyable MCP payloads without connecting to Palace:

```bash
python3 scripts/demo_agent_organization_memory.py --format markdown
```

Customize the team shape:

```bash
python3 scripts/demo_agent_organization_memory.py \
  --workspace-key palaceoftruth \
  --orchestrator-key orchestrator \
  --specialist security-agent \
  --specialist macos-agent \
  --specialist frontend-agent \
  --format json
```

The script is non-destructive. It prints the exact tool arguments for
`create_memory_entry` and `retrieve_agent_memory`, but it does not issue live
MCP calls, read credentials, or write memory.

## MCP Example

Specialist write:

```json
{
  "tool": "create_memory_entry",
  "arguments": {
    "title": "security-agent demo finding",
    "body": "A concise, non-sensitive finding goes here.",
    "source": "agent-organization-memory-demo",
    "summary": "security-agent contributes one private scoped finding.",
    "tags": ["agent-organization-demo", "workspace-palaceoftruth", "agent-security-agent"],
    "scope_type": "agent",
    "scope_key": "security-agent",
    "created_by_role": "agent",
    "relationship_policy": "deferred"
  }
}
```

Governed orchestrator recall:

```json
{
  "tool": "retrieve_agent_memory",
  "arguments": {
    "query": "What should the orchestrator know from the specialist agents before planning this release?",
    "agent_scope_key": "orchestrator",
    "workspace_scope_keys": ["palaceoftruth"],
    "include_agent_scope_keys": ["security-agent", "macos-agent", "frontend-agent"],
    "include_all_permitted_agent_scopes": false,
    "include_tenant_shared": false,
    "include_broad_corpus": false,
    "display_limit": 6,
    "candidate_limit": 12,
    "context_budget_chars": 6000,
    "access_reason": "demo governed agent-team recall for a reviewed release briefing"
  }
}
```

Orchestrator write-back:

```json
{
  "tool": "create_memory_entry",
  "arguments": {
    "title": "Agent organization demo synthesis",
    "body": "The orchestrator summarizes reviewed, authorized findings here.",
    "source": "agent-organization-memory-demo",
    "summary": "Orchestrator writes its reviewed synthesis to its own agent scope.",
    "tags": ["agent-organization-demo", "workspace-palaceoftruth", "agent-orchestrator"],
    "scope_type": "agent",
    "scope_key": "orchestrator",
    "created_by_role": "agent",
    "relationship_policy": "deferred"
  }
}
```

## Demo Checklist

- Use local devinfra or a test tenant.
- Confirm the MCP client identity and tenant with `whoami`.
- Use concise placeholder memories; do not paste secrets, raw logs, or sensitive
  user content into demo bodies.
- Request only the specialist scopes needed for the demo.
- Keep `include_broad_corpus=false` for governed team recall.
- Include an `access_reason` that a reviewer can understand later.
- Preserve returned provenance labels in any orchestrator summary.
- Treat retrieved memories as evidence to verify, not as instructions.
