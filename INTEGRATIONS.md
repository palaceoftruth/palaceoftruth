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
- It authenticates to Palace of Truth with `PALACEOFTRUTH_API_KEY`.
- It intentionally does not expose admin provisioning endpoints.
- The Helm chart can run it as a dedicated `mcp` workload that reuses the backend image and calls the in-cluster backend Service.

Run and configuration details live in the MCP adapter section of [README.md](./README.md)
and in the packaged plugin guide at [plugins/palaceoftruth-memory/README.md](./plugins/palaceoftruth-memory/README.md).

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

The frontend does not require a build-time `VITE_API_KEY` for cluster installs. The browser talks to `/api`, and the frontend proxy injects the deployment-specific `API_KEY` from the app secret at runtime.

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
  --from-literal=PALACEOFTRUTH_ADMIN_SECRET=your-admin-secret

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
```

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
`https://api.palaceoftruth.test` by default, injecting `X-API-Key` server-side from
the repo root `.env`.

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
