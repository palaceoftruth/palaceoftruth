# Security Policy

Palace of Truth stores user content, embeddings, retrieval metadata, tenant API keys, and optional integration credentials. Treat security reports as high priority.

## Supported Versions

Security fixes target the current `main` branch until formal release branches exist.

## Reporting A Vulnerability

Until the public repository cutover is complete, use the current repository's
Security tab or the maintainer-provided private reporting channel:

1. Open the current repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include affected versions, reproduction steps, expected impact, and any safe proof-of-concept details.

Do not open a public issue for secrets, authentication bypasses, tenant isolation failures, data exposure, SSRF, arbitrary file reads, prompt-injection exploit chains, or deployment credential leaks.

## Security Boundaries

- Tenant API keys must not access other tenants' private content.
- Admin endpoints under `/api/v1/admin/*` are control-plane-only and should be protected by deployment-specific ingress policy where exposed.
- MCP transports must use the same tenant and scope rules as the REST memory facade.
- Webhook and repo/source sync inputs must not expose internal services, local files, or cluster credentials.
- Documentation, examples, PRs, and benchmark artifacts must not include raw secrets, bearer tokens, private transcript text, or production data dumps.

## Cluster Hardening Defaults

The Helm chart ships hardened defaults rather than leaving them to each
deployment:

- Every pod satisfies the Kubernetes `restricted` Pod Security Standard: non-root
  uid, `RuntimeDefault` seccomp, all capabilities dropped, no privilege
  escalation, and a read-only root filesystem.
- The namespace carries `pod-security.kubernetes.io/{enforce,audit,warn}`
  labels so a workload that drops its securityContext is rejected rather than
  admitted quietly.
- ServiceAccount tokens are not auto-mounted. The one exception is the memory
  rollout smoke Job, which holds a minimal read-only Role for pods and pod
  logs.
- Ingress NetworkPolicies restrict Postgres, Valkey, the API, and the MCP
  server to the pods that legitimately reach them.
- Postgres carries explicit resource requests, so the database is never the
  first pod evicted under node memory pressure.

Backups cannot be on by default, because the destination and credentials are
environment-owned. Set `postgres.backup.requireBackup: true` in production
values so a release without backups fails to render.

See [INTEGRATIONS.md](INTEGRATIONS.md) for the values that control these and
for the supported ways to relax one workload.

## Local Development

Use `.env.example` as a template and keep `.env` out of git. Generate strong local values for:

- `API_KEY`
- `PALACEOFTRUTH_ADMIN_SECRET`
- `DB_PASSWORD`
- provider API keys and optional integration tokens

## Public Documentation Hygiene

- Keep `LICENSE`, `NOTICE`, and `TRADEMARKS.md` aligned with the repository that is currently accepting releases.
- Use placeholders instead of real hosts, tokens, tenant ids, item ids, or private infrastructure paths unless they are explicitly public fixtures.
- Keep private deployment details in clearly labeled maintainer examples, not generic install paths.
