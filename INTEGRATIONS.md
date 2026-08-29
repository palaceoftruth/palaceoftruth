# Integrations Guide

This document describes how to integrate the Palace of Truth Helm chart into external systems, how to configure required external dependencies, and how to wire in optional services.

For the current project snapshot, use [PROJECT_STATUS.md](./PROJECT_STATUS.md). This file documents portable integration patterns. Operator-specific deployment values, ArgoCD applications, secret-manager IDs, DNS targets, and private runbooks belong in a separate private deployment repository.

---

## Hermes Plugin Source Of Truth

The Hermes-compatible `palaceoftruth` memory plugin lives in this repository at:

`third_party_plugins/hermes/memory/palaceoftruth/`

Current ownership model:

- This repository is the canonical source for Hermes-compatible plugin logic.
- Deployment repos should consume the plugin as a pinned artifact in their custom runtime image.
- Maintainers publish container artifacts from this repo to GHCR.
- Changes should land here first, then deployment consumers should update their pinned artifact or digest.

This keeps the Hermes memory contract owned by Palace of Truth while letting deployment repos consume a single pinned build artifact instead of copying plugin source trees.

---

## MCP Adapter For Generic Clients

This repository now also ships a standalone MCP adapter for non-Hermes runtimes:

`backend/app/mcp_server.py`

Use it when a client already speaks MCP and you want it to connect to Palace of Truth's existing memory/search API without writing a custom REST wrapper first.

Key points:

- It is a thin adapter over the existing REST contract, not a second memory implementation.
- It supports both `stdio` and streamable HTTP transport.
- It prefers OAuth client credentials or a static bearer token when configured,
  with `PALACEOFTRUTH_API_KEY` retained only as an explicit compatibility
  fallback during migration.
- It intentionally does not expose admin provisioning endpoints.
- The Helm chart can run it as a dedicated `mcp` workload that reuses the backend image and calls the in-cluster backend Service.

Run and configuration details live in the MCP adapter section of [README.md](./README.md)
and in the packaged plugin guide at [third_party_plugins/agent_clients/palaceoftruth-memory/README.md](./third_party_plugins/agent_clients/palaceoftruth-memory/README.md).
The OAuth runtime, raw REST resource, and credential-lifecycle contract is
documented in [docs/palace-oauth-mcp-runtime-rollout.md](./docs/palace-oauth-mcp-runtime-rollout.md).

---

## Helm Chart

The public, source-first install path uses the checked-out chart. Before running
it, build and publish backend/frontend images to your own registry, create the
runtime app secret, and disable or configure any secret-manager integration that
does not exist in your cluster:

```bash
export PALACEOFTRUTH_IMAGE_TAG="2026-05-24-example"

helm install palaceoftruth ./chart \
  --namespace palaceoftruth \
  --create-namespace \
  --set image.registry=ghcr.io \
  --set image.backendRepository=palaceoftruth/palaceoftruth/backend \
  --set image.frontendRepository=palaceoftruth/palaceoftruth/frontend \
  --set image.tag="$PALACEOFTRUTH_IMAGE_TAG" \
  --set externalSecrets.enabled=false \
  --set existingSecret=palaceoftruth-app-secrets \
  -f my-values.yaml
```

If you publish the chart as an OCI artifact, install from your own chart registry:

```
oci://ghcr.io/palaceoftruth/palaceoftruth/palaceoftruth
```

External operators can also use the local chart path above without publishing an
OCI chart first.

### Pod Hardening

Every pod the chart renders satisfies the Kubernetes `restricted` Pod Security
Standard by default. Workloads inherit this; there is no per-workload opt-in to
forget.

| Level | Setting |
| --- | --- |
| Pod | `runAsNonRoot: true`, `runAsUser`/`runAsGroup`/`fsGroup` 10001, `seccompProfile: RuntimeDefault` |
| Container | `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]` |
| Pod | `automountServiceAccountToken: false` |

Because the root filesystem is read-only, application containers mount two
`emptyDir` volumes: `/tmp` for temp files and staged media, and
`podSecurity.homeDir` (default `/home/palace`) for the MCP credential cache.
`HOME` is set to that path.

The only pod that mounts a ServiceAccount token is the memory rollout smoke
Job, which reads pods and pod logs through a minimal namespaced Role to verify
a rollout.

The frontend runs `nginx-unprivileged`, which cannot bind a port below 1024 as
a non-root user. It listens on `frontend.containerPort` (default 8080); the
Service still publishes 80, so ingress configuration is unchanged.

When `namespace.create=true`, the chart labels the namespace for Pod Security
Admission:

```yaml
podSecurity:
  admission:
    enabled: true
    enforce: restricted
    audit: restricted
    warn: restricted
    version: latest
```

Enforcing `restricted` rejects any future workload that drops its
securityContext. Adding these labels to a namespace that already holds pods
created by other tooling can block those pods on their next restart; start
with `enforce: baseline` and keep `audit`/`warn` at `restricted`, then tighten
once the audit annotations are clean.

Relax a single workload through `podSecurity.overrides.<workload>`, which is
merged last:

```yaml
podSecurity:
  overrides:
    localEmbedding:
      container:
        readOnlyRootFilesystem: false
```

Recognized keys: `backend`, `frontend`, `worker`, `mediaWorker`,
`palaceWorker`, `mcp`, `migration`, `memoryRolloutSmoke`, `valkey`,
`valkeySentinel`, `localEmbedding`. The Valkey metrics exporter keeps its own
`valkey.metrics.securityContext`. `podSecurity.enabled: false` removes all
securityContexts; that is a debugging escape hatch, not a supported
configuration.

### Install

```bash
export CHART_VERSION="0.1.345"

helm install palaceoftruth oci://ghcr.io/palaceoftruth/palaceoftruth/palaceoftruth \
  --version "$CHART_VERSION" \
  --namespace palaceoftruth \
  --create-namespace \
  -f my-values.yaml
```

### Upgrade

```bash
export CHART_VERSION="0.1.345"

helm upgrade palaceoftruth oci://ghcr.io/palaceoftruth/palaceoftruth/palaceoftruth \
  --version "$CHART_VERSION" \
  -f my-values.yaml
```

Upgrading an install that predates the pod hardening defaults changes two
things at the pod level:

- Pods now run as uid 10001 with `fsGroup: 10001`. Kubernetes recursively
  changes group ownership of every mounted volume on the first mount after the
  upgrade, so the first Valkey and shared-runtime pod start can be slow on a
  large volume.
- If `namespace.create=true`, the namespace gains
  `pod-security.kubernetes.io/enforce: restricted`. Pods in that namespace
  created by other tooling are then blocked on their next restart. Set
  `podSecurity.admission.enforce: baseline` for the first upgrade, confirm the
  audit annotations are clean, then tighten.

### Minimal `values.yaml` Override

```yaml
image:
  registry: ghcr.io
  backendRepository: palaceoftruth/palaceoftruth/backend
  frontendRepository: palaceoftruth/palaceoftruth/frontend
  tag: "2026-05-24-example"

config:
  openrouterDefaultModel: minimax/minimax-m2.7
  openrouterFallbackModels: nvidia/nemotron-3-super-120b-a12b
  palaceSyncAllowedRoots: ""   # leave empty in cluster installs unless corpus paths are mounted
  palaceDefaultS3SourceName: ""
  palaceDefaultS3Bucket: ""
  palaceDefaultS3Prefix: ""
  palaceDefaultS3EndpointUrl: ""
  palaceDefaultS3Region: ""

ingress:
  baseDomain: palaceoftruth.example.com

externalSecrets:
  enabled: false   # disable if not using ESO; provide secrets manually instead

existingSecret: palaceoftruth-app-secrets
existingRegistrySecret: ""   # set when your registry requires imagePullSecrets
```

By default the chart deploys backend and frontend images tagged with the chart `appVersion`. Set `image.tag` only when you need to override that default.

The frontend does not support a build-time browser API key for cluster installs. The browser talks to `/api` through the same-origin proxy, but the proxy must not inject the deployment-specific `API_KEY`; agent and service integrations should authenticate through MCP OAuth or server-side credentials.

Operators sign in to the UI from **Settings**: paste a tenant API key once, and the backend exchanges it for a short-lived session. The key is never stored in the browser. The session lives in an `HttpOnly` `palace_session` cookie that page scripts cannot read, plus a readable `palace_session_csrf` companion the SPA echoes in the `X-Palace-CSRF` header on every unsafe request. Sessions expire after `BROWSER_SESSION_TTL_SECONDS` (12 hours by default), can be revoked from the UI, and stop working as soon as the API key behind them is revoked.

A session is always narrower than its key. By default it excludes the `admin` scope, so ordinary browsing cannot register MCP clients or mint extension pairing keys. Select **Enable administration tools for this session** at sign-in when you need those, and sign out afterwards.

### Optional S3 Credentials for Palace Sync

If you want Palace to sync from MinIO, R2, or another S3-compatible object store, provide AWS-style credentials to the backend and worker:

```yaml
externalSecrets:
  enabled: true
  s3CredentialsItemId: "<secret-manager-s3-creds-item-id>"
  s3AccessKeyProperty: access-key-id
  s3SecretKeyProperty: secret-access-key
  s3SessionTokenProperty: ""   # set only if your provider requires it
```

Or, without ESO:

```bash
kubectl create secret generic palaceoftruth-app-secrets \
  --namespace palaceoftruth \
  --from-literal=AWS_ACCESS_KEY_ID=... \
  --from-literal=AWS_SECRET_ACCESS_KEY=...
```

If you also want the app to auto-register a default Palace S3 source on startup, set the non-secret metadata in chart values:

```yaml
config:
  palaceDefaultS3SourceName: "Example markdown corpus"
  palaceDefaultS3Bucket: "palaceoftruth-corpus"
  palaceDefaultS3Prefix: "docs"
  palaceDefaultS3EndpointUrl: "https://<cloudflare-account-id>.r2.cloudflarestorage.com"
  palaceDefaultS3Region: "auto"
  palaceDefaultS3AllowedExtensions: ".md"
  palaceDefaultS3ForcePathStyle: "false"
```

### Optional Repo Credentials for Palace Sync

If you want Palace repo sync sources to support private GitHub repositories or stored repo credentials, wire these app secrets:

```yaml
externalSecrets:
  enabled: true
  githubPatProperty: github-pat
  syncSourceCredentialKeyProperty: palaceoftruth-sync-source-credential-key
```

Or, without ESO:

```bash
kubectl create secret generic palaceoftruth-app-secrets \
  --namespace palaceoftruth \
  --from-literal=GITHUB_PAT=ghp_... \
  --from-literal=PALACEOFTRUTH_SYNC_SOURCE_CREDENTIAL_KEY=... \
  --dry-run=client -o yaml | kubectl apply -f -
```

Notes:

- `GITHUB_PAT` is only required if you want the `deployment_github_pat` repo credential mode in Palace.
- `PALACEOFTRUTH_SYNC_SOURCE_CREDENTIAL_KEY` is required only when you want stored PAT or SSH-key credentials encrypted at rest in the database.
- Leave `externalSecrets.githubPatProperty` and `externalSecrets.syncSourceCredentialKeyProperty` empty when an environment does not need repo sync secrets. The chart treats both as opt-in.
- Leave these fields empty unless your deployment needs repo sync credentials.

---

## ArgoCD (GitOps)

This repository does not carry an environment-specific ArgoCD Application. Keep ArgoCD resources and private Helm values in your deployment repository.

Create an Application manifest referencing the chart with your own values:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: palaceoftruth
  namespace: argocd
spec:
  project: default
  source:
    repoURL: ghcr.io/palaceoftruth/palaceoftruth
    chart: palaceoftruth
    targetRevision: "<chart-version>"
    helm:
      values: |
        config:
          openrouterDefaultModel: minimax/minimax-m2.7
          openrouterFallbackModels: nvidia/nemotron-3-super-120b-a12b
        ingress:
          baseDomain: palaceoftruth.myorg.com
  destination:
    server: https://kubernetes.default.svc
    namespace: palaceoftruth
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

---

## Secrets Management

### External Secrets Operator (ESO)

The chart can render ExternalSecret resources for clusters that use ESO. To use this:

1. Install [External Secrets Operator](https://external-secrets.io/)
2. Configure a `ClusterSecretStore` backed by your secret provider
3. Set the following in your values:

```yaml
externalSecrets:
  enabled: true
  secretStoreName: "<cluster-secret-store-name>"
  secretStoreKind: ClusterSecretStore
  appSecretItemId: "<app-secret-item-id>"
  registrySecretItemId: "<registry-secret-item-id>"
```

If you want one secret-provider item to back both the app secrets and Palace S3 credentials, point both fields at the same item ID:

```yaml
externalSecrets:
  enabled: true
  appSecretItemId: "<palaceoftruth-secret-item-id>"
  s3CredentialsItemId: "<palaceoftruth-secret-item-id>"
  registrySecretItemId: "<registry-secret-item-id>"
```

### Manual Secrets (without ESO)

If you are not using ESO, disable it and create the secrets manually before installing the chart:

```bash
# Application secrets
kubectl create secret generic palaceoftruth-app-secrets \
  --namespace palaceoftruth \
  --from-literal=OPENAI_API_KEY=sk-... \
  --from-literal=OPENROUTER_API_KEY=sk-or-... \
  --from-literal=API_KEY=your-api-key \
  --from-literal=PALACEOFTRUTH_ADMIN_SECRET=your-admin-secret \
  --from-literal=CREDENTIAL_PEPPER=your-credential-pepper

# Registry pull secret (if using private GHCR packages)
kubectl create secret docker-registry palaceoftruth-registry \
  --namespace palaceoftruth \
  --docker-server=ghcr.io \
  --docker-username=<user> \
  --docker-password=<password>
```

Then set in values:

```yaml
existingRegistrySecret: palaceoftruth-registry

externalSecrets:
  enabled: false
```

Model selection and Palace sync policy belong in chart config, not secrets:

```yaml
config:
  openrouterDefaultModel: minimax/minimax-m2.7
  openrouterFallbackModels: nvidia/nemotron-3-super-120b-a12b
  palaceSyncAllowedRoots: ""
```

Palace S3 sync sources do not use `PALACE_SYNC_ALLOWED_ROOTS`; that policy only applies to local folder/repo mounts.

---

## Palace Sync Sources

Palace now supports three sync source kinds:

- `folder`
- `repo`
- `s3`

Use `s3` for MinIO, R2, or another S3-compatible store. Use `repo` for curated Git or GitHub-backed corpora. The backend stores non-secret source metadata per sync source and reads deployment-managed secrets from the app secret at runtime.

### Repo Sync Credential Modes

Repo sync sources currently support four credential modes:

- public repo / no credential
- stored GitHub PAT
- deployment-managed GitHub PAT
- stored SSH credential

Stored PATs and SSH credentials are encrypted at rest with `PALACEOFTRUTH_SYNC_SOURCE_CREDENTIAL_KEY`. Deployment-managed PAT mode reads `GITHUB_PAT` at runtime and does not store the token in the database.

### Example Palace S3 Source

```json
{
  "name": "Example Markdown Corpus",
  "source_kind": "s3",
  "bucket": "palaceoftruth-corpus",
  "prefix": "docs",
  "endpoint_url": "http://minio.minio.svc.cluster.local:9000",
  "region": "us-east-1",
  "force_path_style": true,
  "allowed_extensions": [".md"],
  "scan_interval_seconds": 900
}
```

Notes:

- Use `force_path_style: true` for MinIO.
- Leave `force_path_style: false` for R2 unless your endpoint requires otherwise.
- Use `allowed_extensions: [".md"]` if you only want markdown imported.
- The stored source locator becomes `s3://<bucket>/<prefix>`, while individual item `source_url` values become `s3://<bucket>/<object-key>`.
- Palace sync sources can now be edited and deleted through the control plane as well as created and manually synced.

---

## PostgreSQL — CloudNative-PG

The chart deploys a [CloudNative-PG](https://cloudnative-pg.io/) cluster. Requires the CNPG operator to be installed in the cluster.

Fresh installs bootstrap `pgvector` in the application database during `initdb`, so the app does not require a manual `CREATE EXTENSION vector` step.

### Install the Operator

```bash
helm repo add cnpg https://cloudnative-pg.github.io/charts
helm install cnpg cnpg/cloudnative-pg --namespace cnpg-system --create-namespace
```

### Chart Values

```yaml
postgres:
  instances: 1                  # increase for HA
  storage:
    size: 5Gi
    storageClass: ""            # use cluster default
  parameters:
    shared_buffers: "128MB"
    max_connections: "100"
  # Shipped defaults, not a sizing recommendation. They exist so the database
  # is never BestEffort and therefore never the first pod evicted under node
  # memory pressure. Raise them from observed load. CPU deliberately has no
  # limit: throttling a primary during checkpoint or vacuum is worse than
  # letting it burst.
  resources:
    requests:
      cpu: 250m
      memory: 1Gi
    limits:
      memory: 1Gi
```

### CNPG-I Backups with Barman Cloud

The chart can render a Barman Cloud `ObjectStore`, attach it as the cluster WAL
archiver, and create a CNPG `ScheduledBackup`. Install a compatible
[Barman Cloud plugin](https://cloudnative-pg.io/plugin-barman-cloud/docs/intro/)
and its CRDs before enabling this feature. Keep bucket endpoints, credential
secret references, schedules, and retention policy in environment-owned values.

Backups are off by default because they need a `destinationPath` and
credentials that only the operator of a given environment holds; a default-on
chart would fail to render everywhere. The chart compensates in two ways:

- Every install that runs Postgres without backups prints a warning in the
  Helm NOTES output.
- Setting `postgres.backup.requireBackup: true` turns that warning into a
  render-time failure. Set it in production values so an unprotected database
  can never ship unnoticed.

`objectStore.retentionPolicy` defaults to `30d` so enabling backups cannot
create an unbounded bucket by accident. Set it to `""` only when the object
store's own lifecycle policy owns expiry.

```yaml
postgres:
  backup:
    enabled: true
    objectStore:
      configuration:
        destinationPath: s3://example-palace-backups/palace
        endpointURL: https://object-storage.example.com
        s3Credentials:
          accessKeyId:
            name: palace-backup-credentials
            key: ACCESS_KEY_ID
          secretAccessKey:
            name: palace-backup-credentials
            key: SECRET_ACCESS_KEY
      retentionPolicy: "30d"
    scheduledBackup:
      schedule: "0 0 0 * * *" # six fields: seconds through day-of-week
      immediate: true
```

Backups do not include database credential Secrets. Protect those through the
cluster's normal secret-management path. Recovery must create a separate CNPG
cluster that reads the source `ObjectStore`; never point a restore drill's WAL
archiver at the source backup path.

### Using an External PostgreSQL

If you prefer to bring your own PostgreSQL (with pgvector), disable the in-chart cluster and point the app at your instance:

```yaml
postgres:
  enabled: false
```

Then provide `DATABASE_URL` in your manual secrets (or ESO) in the format:

```
postgresql+asyncpg://<user>:<password>@<host>:<port>/<dbname>
```

The database must have the `pgvector` extension enabled:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

---

## Redis / Valkey (Queue + Cache)

The chart deploys a single-instance Valkey (Redis-compatible) deployment.

### Chart Values

```yaml
valkey:
  storage:
    size: 1Gi
    storageClass: ""
  resources:
    requests:
      memory: "64Mi"
    limits:
      memory: "256Mi"
```

### Using an External Redis

Set `valkey.enabled: false` and provide `REDIS_URL` in secrets:

```
redis://<host>:6379
```

---

## Ingress — Nginx Ingress Controller

The chart creates one main Ingress with host rules for the frontend, API, and optional MCP host. When `ingress.admin.enabled=true`, it also creates a separate admin Ingress for `/api/v1/admin/*` so operators can attach stricter control-plane annotations without changing runtime API traffic.

### Requirements

- [Nginx Ingress Controller](https://kubernetes.github.io/ingress-nginx/) installed in the cluster
- cert-manager for TLS (see below)

### Chart Values

```yaml
ingress:
  className: nginx
  frontendHost: palaceoftruth.example.com
  apiHost: api.palaceoftruth.example.com
  mcpHost: mcp.palaceoftruth.example.com
  externalDnsTarget: ""         # set if using external-dns
  admin:
    enabled: false              # optional split control-plane ingress
certificate:
  clusterIssuer: letsencrypt-prod   # cert-manager ClusterIssuer name
```

---

## TLS — cert-manager

TLS certificates are issued by cert-manager using the `certificate.yaml` template.

### Requirements

- [cert-manager](https://cert-manager.io/) installed in the cluster
- A `ClusterIssuer` configured (DNS-01 or HTTP-01 challenge)

### Chart Values

```yaml
certificate:
  clusterIssuer: letsencrypt-prod   # name of your ClusterIssuer
```

---

## DNS — external-dns

DNS records are managed automatically by [external-dns](https://github.com/kubernetes-sigs/external-dns) via annotations on Ingress resources. Do not create or modify DNS records manually for Kubernetes-managed hostnames.

### Chart Values

```yaml
ingress:
  externalDnsTarget: "<load-balancer-ip-or-hostname>"   # IP or hostname external-dns should resolve to
```

Setting this value adds the `external-dns.alpha.kubernetes.io/target`
annotation to the Ingress resources. external-dns derives hostnames from the
Ingress `rules.host` entries unless you add a hostname annotation yourself.

Leave blank if you are managing DNS records out-of-band.

---

## AI Services

### OpenAI (Embeddings + Transcription)

Required for:
- Generating vector embeddings (`text-embedding-3-small`, 1536 dimensions by default)
- YouTube/audio transcription (`gpt-4o-transcribe-diarize`)

Provide `OPENAI_API_KEY` in secrets. The embedding model and Whisper model can be overridden:

```yaml
# In configmap values or directly in a ConfigMap override
config:
  embeddingModel: "text-embedding-3-small"
  embeddingDimensions: "1536"
  embeddingProfileName: "openai-text-embedding-3-small-1536"
  whisperModel: "gpt-4o-transcribe-diarize"
```

Changing embedding dimensions requires a planned re-embedding migration. Do not switch an existing 1536-dimensional deployment to a different profile without rebuilding stored embeddings.

### Optional Local Embedding Service

Set `localEmbeddingService.enabled=true` to render an internal Text Embeddings Inference service and point the app at it. This is disabled by default. Use it for fresh installs or planned re-embedding migrations only, because the default local example uses 768-dimensional embeddings while the OpenAI default uses 1536 dimensions.

### Media Worker

Media ingest runs on its own ARQ queue and worker deployment. Scale it with `mediaWorker.replicas` and `mediaWorker.maxJobs`; keep `maxJobs` conservative unless the cluster CPU and transcription provider capacity can handle parallel work.

### High Availability

Set `highAvailability.enabled=true` to raise app, worker, MCP, Postgres, and Valkey guardrails using the replica counts in `highAvailability.replicas`. Use this only when the cluster has enough schedulable capacity and storage/failure-domain support.

### OpenRouter (LLM — Summarization, Tagging, Chat, Relationships)

Required for:
- Summarizing ingested content
- Generating tags and categories
- RAG chat responses
- Extracting relationships between items

Provide `OPENROUTER_API_KEY` in secrets. The default and fallback models can be configured:

```yaml
config:
  openrouterDefaultModel: "minimax/minimax-m2.7"
  openrouterFallbackModels: "nvidia/nemotron-3-super-120b-a12b"
```

Any [OpenRouter-compatible model](https://openrouter.ai/models) can be used.

---

## API Authentication

The Palace of Truth API uses a static API key passed in the `X-API-Key` header.

```bash
curl -H "X-API-Key: ${PALACEOFTRUTH_API_KEY}" https://api.palaceoftruth.example.com/api/v1/health
```

The key is set via the `API_KEY` environment variable (provided through secrets).

Each key carries a scope grant stored on its `api_keys` row. The `X-MCP-Scope`
and `X-MCP-Scopes` headers narrow that grant for one call and cannot add a
scope to it, so give a key the scopes it needs when you register or rotate it
with the `scopes` field. Stored credential verifiers are peppered HMAC-SHA256
when `CREDENTIAL_PEPPER` is set; both hash formats are accepted, so the pepper
can be introduced on a running deployment, but changing it later invalidates
every credential hashed with the previous value.

---

## Item Governance

### Structured Log Keys

Governance transitions on `PATCH /api/v1/items/{id}` emit structured log lines that operators can replay against the audit trail without an ORM round-trip. The `extra` payload never includes raw content, secrets, or tenant identifiers beyond what is needed to find the row.

- `governance.item.update` at INFO on every successful transition, with `extra={"item_id", "tenant_id", "actor_subject", "changed_fields", "verification_state", "risk_class", "verification_deadline"}`.
- `governance.item.denied` at WARNING when a cross-tenant or scope-limited write is refused, with `extra={"tenant_id", "item_id", "actor_subject", "reason"}`.

### Partial Indexes

Migration `070_item_governance` adds three partial Postgres indexes so the columns operators actually filter on stay indexable without bloating the catalog for the untriaged majority:

- `idx_items_governance_tenant_deadline` on `(tenant_id, governance_verification_deadline)` — used by background sweeps that flag expired verification and by the currentness derivation in search ranking.
- `idx_items_governance_tenant_risk` on `(tenant_id, governance_risk_class)` — used by ranking-trace rollups and by any operator query that groups high-risk items by tenant.
- `idx_claims_governance_tenant_deadline` on `(tenant_id, governance_verification_deadline)` — used by claim-level expiry sweeps; claims inherit enough of the item surface to be auditable on their own.

Each index is partial with `WHERE <column> IS NOT NULL`, so it only indexes rows that have been triaged.

### Search Ranking

The search service projects the eight governance columns through to `_SearchCandidate` and computes `governance_currentness_state ∈ {unassigned, current, expired, superseded}`. Ranking adjustments:

- `governance_expired_high_risk = -0.35` is applied to items whose deadline has passed and whose `risk_class` is `high` or `critical`. Lower-risk expired items stay visible without a penalty so the warning reaches the wire.
- Items in `governance_currentness_state = superseded` are excluded from `current`-mode search results. The count is recorded as `excluded_governance_counts["superseded"]` on the ranking trace, alongside `governance_state_counts` for the four-state distribution.

---

## Local Development

Public localhost fallback:

```bash
docker network create traefik 2>/dev/null || true
cp .env.example .env
# Set OPENAI_API_KEY, OPENROUTER_API_KEY, API_KEY, DB_PASSWORD
docker compose -f docker-compose.yml -f docker-compose.localhost.yml up --build -d
open http://localhost:8080
```

Maintainer review standardizes on devinfra-backed HTTPS routes:

```bash
cp .env.example .env
# Set OPENAI_API_KEY, OPENROUTER_API_KEY, API_KEY, DB_PASSWORD
docker compose up -d
di up palaceoftruth
```

| Service | Local URL (via devinfra) |
|---------|--------------------------|
| Frontend | `https://palaceoftruth.test` |
| API | `https://api.palaceoftruth.test` |
| API docs | `https://api.palaceoftruth.test/docs` |

The base `docker-compose.yml` already joins the `traefik` network and carries the
Traefik labels, so no extra devinfra overlay compose file is required.

If you want host-side frontend HMR, run Vite locally:

```bash
cd frontend
npm install
npm run dev
```

Host-run Vite keeps `/api` on the same origin and proxies to
`https://api.palaceoftruth.test` by default without injecting the shared
backend API key into browser-originated requests.

To call the API directly during local development:

```bash
curl -sk -H "X-API-Key: $API_KEY" https://api.palaceoftruth.test/api/v1/health
```

---

## Supported Content Sources

| Source | Endpoint | Notes |
|--------|----------|-------|
| YouTube / video URLs | `POST /api/v1/ingest/media` | Uses yt-dlp for extraction, Whisper-compatible transcription; `/ingest/youtube` remains a compatibility alias |
| Web articles | `POST /api/v1/ingest/webpage` | Uses trafilatura; Playwright as fallback |
| Documents | `POST /api/v1/ingest/doc` | Multipart upload for supported document formats; `/ingest/pdf` remains a compatibility alias |
| Images | `POST /api/v1/ingest/image` | Multipart upload with image extraction/OCR path |
| Plain text notes | `POST /api/v1/ingest/note` | Direct text input |
| RSS feeds | `/api/v1/feeds` | Registers and auto-polls feeds on the configured interval |

---

## Health Check

```bash
GET /api/v1/health
```

Returns `{"status":"ok"}` and is intended for liveness/readiness probes.

The chart's deployments configure liveness and readiness probes against this endpoint by default.

Authenticated operational stats are exposed at `GET /api/v1/stats`.
