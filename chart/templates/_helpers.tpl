{{/*
Expand the name of the chart.
*/}}
{{- define "palaceoftruth.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
Truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec). If release name contains chart name, use release name.
*/}}
{{- define "palaceoftruth.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Render Kubernetes IntOrString fields. Plain integers must stay unquoted, while
percentage values must stay strings.
*/}}
{{- define "palaceoftruth.intOrPercentString" -}}
{{- $value := . -}}
{{- if kindIs "string" $value -}}
{{- if regexMatch "^[0-9]+$" $value -}}
{{- $value -}}
{{- else -}}
{{- $value | quote -}}
{{- end -}}
{{- else -}}
{{- $value -}}
{{- end -}}
{{- end -}}

{{/*
Create chart label.
*/}}
{{- define "palaceoftruth.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to all resources.
*/}}
{{- define "palaceoftruth.labels" -}}
helm.sh/chart: {{ include "palaceoftruth.chart" . }}
{{ include "palaceoftruth.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels — stable subset used in matchLabels / Service selectors.
*/}}
{{- define "palaceoftruth.selectorLabels" -}}
app.kubernetes.io/name: {{ include "palaceoftruth.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
CNPG postgres cluster name.
Defaults to <fullname>-postgres when postgres.clusterName is empty.
*/}}
{{- define "palaceoftruth.postgresClusterName" -}}
{{- if .Values.postgres.clusterName }}
{{- .Values.postgres.clusterName | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-postgres" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
CNPG auto-generated app secret name: <cluster-name>-app
*/}}
{{- define "palaceoftruth.postgresSecretName" -}}
{{- printf "%s-app" (include "palaceoftruth.postgresClusterName" .) }}
{{- end }}

{{/*
Valkey service name: <fullname>-valkey  (single-instance mode)
*/}}
{{- define "palaceoftruth.valkeyServiceName" -}}
{{- printf "%s-valkey" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Valkey primary service name (sentinel mode).
*/}}
{{- define "palaceoftruth.valkeyPrimaryName" -}}
{{- printf "%s-valkey-primary" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Valkey replica name (sentinel mode).
*/}}
{{- define "palaceoftruth.valkeyReplicaName" -}}
{{- printf "%s-valkey-replica" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Valkey sentinel service name (sentinel mode).
*/}}
{{- define "palaceoftruth.valkeySentinelName" -}}
{{- printf "%s-valkey-sentinel" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
PodDisruptionBudget state. The HA profile turns PDBs on automatically, while
podDisruptionBudgets.enabled allows operators to opt in without the full HA
replica profile.
*/}}
{{- define "palaceoftruth.pdbEnabled" -}}
{{- if or .Values.highAvailability.enabled .Values.podDisruptionBudgets.enabled }}true{{ else }}false{{ end -}}
{{- end }}

{{/*
Effective app replica counts.
*/}}
{{- define "palaceoftruth.backendReplicas" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.backend }}{{ else }}{{ .Values.backend.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.frontendReplicas" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.frontend }}{{ else }}{{ .Values.frontend.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.workerReplicas" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.worker }}{{ else }}{{ .Values.worker.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.mediaWorkerReplicas" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.mediaWorker }}{{ else }}{{ .Values.mediaWorker.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.palaceWorkerReplicas" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.palaceWorker }}{{ else }}{{ .Values.palaceWorker.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.mcpReplicas" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.mcp }}{{ else }}{{ .Values.mcp.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.postgresInstances" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.postgres }}{{ else }}{{ .Values.postgres.instances }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.valkeyReplicaCount" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.valkeyReplicas }}{{ else }}{{ .Values.valkey.sentinel.replicas }}{{ end -}}
{{- end }}

{{- define "palaceoftruth.valkeySentinelCount" -}}
{{- if .Values.highAvailability.enabled }}{{ .Values.highAvailability.replicas.valkeySentinels }}{{ else }}{{ .Values.valkey.sentinel.sentinels }}{{ end -}}
{{- end }}

{{/*
Sentinel mode is enabled either directly or by the HA profile.
*/}}
{{- define "palaceoftruth.valkeySentinelEnabled" -}}
{{- if and .Values.valkey.enabled (or .Values.valkey.sentinel.enabled .Values.highAvailability.enabled) }}true{{ else }}false{{ end -}}
{{- end }}

{{/*
Redis URL: uses config.redisUrl override, or derives from Valkey service name,
or falls back to externalRedisUrl when valkey.enabled=false.
In sentinel mode, REDIS_URL still points to the primary (for health checks /
non-ARQ clients). ARQ and the app use REDIS_SENTINEL_HOSTS instead.
*/}}
{{- define "palaceoftruth.redisUrl" -}}
{{- if .Values.config.redisUrl }}
{{- .Values.config.redisUrl }}
{{- else if eq (include "palaceoftruth.valkeySentinelEnabled" .) "true" }}
{{- printf "redis://%s:6379" (include "palaceoftruth.valkeyPrimaryName" .) }}
{{- else if .Values.valkey.enabled }}
{{- printf "redis://%s:6379" (include "palaceoftruth.valkeyServiceName" .) }}
{{- else }}
{{- .Values.externalRedisUrl }}
{{- end }}
{{- end }}

{{/*
App secrets secret name.
*/}}
{{- define "palaceoftruth.appSecretName" -}}
{{- if .Values.existingSecret }}
{{- .Values.existingSecret }}
{{- else }}
{{- printf "%s-app-secrets" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Registry pull secret name.
*/}}
{{- define "palaceoftruth.registrySecretName" -}}
{{- if .Values.existingRegistrySecret }}
{{- .Values.existingRegistrySecret }}
{{- else }}
{{- printf "%s-registry-pull" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
Shared upload artifact storage used by API and worker pods for handoff artifacts.
*/}}
{{- define "palaceoftruth.sharedRuntimeStorageClaimName" -}}
{{- if .Values.sharedRuntimeStorage.existingClaim }}
{{- .Values.sharedRuntimeStorage.existingClaim }}
{{- else }}
{{- printf "%s-runtime" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "palaceoftruth.sharedRuntimeStorageEnabled" -}}
{{- if or .Values.sharedRuntimeStorage.enabled .Values.highAvailability.enabled }}true{{ else }}false{{ end -}}
{{- end }}

{{- define "palaceoftruth.runtimeVolumeMount" -}}
- name: temp-files
  mountPath: "/tmp/palaceoftruth"
{{- end }}

{{- define "palaceoftruth.runtimeVolume" -}}
- name: temp-files
  emptyDir: {}
{{- if eq (include "palaceoftruth.sharedRuntimeStorageEnabled" .) "true" }}
- name: upload-artifacts
  persistentVolumeClaim:
    claimName: {{ include "palaceoftruth.sharedRuntimeStorageClaimName" . }}
{{- end }}
{{- end }}

{{- define "palaceoftruth.uploadArtifactVolumeMount" -}}
{{- if eq (include "palaceoftruth.sharedRuntimeStorageEnabled" .) "true" }}
- name: upload-artifacts
  mountPath: "/tmp/palaceoftruth/upload-artifacts"
{{- end }}
{{- end }}

{{/*
Image tag reference.
Prefer an explicit override, otherwise default to the chart appVersion so
each published chart revision renders immutable image tags.
*/}}
{{- define "palaceoftruth.imageTag" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if not $tag }}
{{- fail "image.tag must be set or Chart.appVersion must be non-empty" }}
{{- end }}
{{- $tag -}}
{{- end }}

{{/*
Backend image reference.
*/}}
{{- define "palaceoftruth.backendImage" -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.image.backendRepository (include "palaceoftruth.imageTag" .) }}
{{- end }}

{{/*
MCP service name.
*/}}
{{- define "palaceoftruth.mcpServiceName" -}}
{{- printf "%s-mcp" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Migration Job name.
The suffix changes when the chart version or image tag changes, so GitOps and
plain Helm installs get one immutable Job per rendered app release.
*/}}
{{- define "palaceoftruth.migrationJobName" -}}
{{- $suffix := printf "%s-%s" .Chart.Version (include "palaceoftruth.imageTag" .) | sha256sum | trunc 10 -}}
{{- printf "%s-migrate-%s" (include "palaceoftruth.fullname" . | trunc 44 | trimSuffix "-") $suffix | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Local embedding service name.
*/}}
{{- define "palaceoftruth.localEmbeddingServiceName" -}}
{{- printf "%s-local-embedding" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Local embedding service base URL.
*/}}
{{- define "palaceoftruth.localEmbeddingServiceUrl" -}}
{{- printf "http://%s:%v" (include "palaceoftruth.localEmbeddingServiceName" .) .Values.localEmbeddingService.port }}
{{- end }}

{{/*
Effective embedding provider config.
*/}}
{{- define "palaceoftruth.embeddingProvider" -}}
{{- if .Values.localEmbeddingService.enabled }}local-http{{ else }}{{ .Values.config.embeddingProvider }}{{ end -}}
{{- end }}

{{/*
Effective embedding model config.
*/}}
{{- define "palaceoftruth.embeddingModel" -}}
{{- if .Values.localEmbeddingService.enabled }}{{ .Values.localEmbeddingService.modelId }}{{ else }}{{ .Values.config.embeddingModel }}{{ end -}}
{{- end }}

{{/*
Effective embedding dimensions config.
*/}}
{{- define "palaceoftruth.embeddingDimensions" -}}
{{- if .Values.localEmbeddingService.enabled }}{{ .Values.localEmbeddingService.embeddingDimensions }}{{ else }}{{ .Values.config.embeddingDimensions }}{{ end -}}
{{- end }}

{{/*
Effective embedding profile name config.
*/}}
{{- define "palaceoftruth.embeddingProfileName" -}}
{{- if .Values.localEmbeddingService.enabled }}{{ .Values.localEmbeddingService.embeddingProfileName }}{{ else }}{{ .Values.config.embeddingProfileName }}{{ end -}}
{{- end }}

{{/*
Effective local embedding HTTP URL.
*/}}
{{- define "palaceoftruth.embeddingLocalHttpUrl" -}}
{{- if .Values.config.embeddingLocalHttpUrl }}
{{- .Values.config.embeddingLocalHttpUrl }}
{{- else if .Values.localEmbeddingService.enabled }}
{{- include "palaceoftruth.localEmbeddingServiceUrl" . }}
{{- else }}
{{- .Values.config.embeddingLocalHttpUrl }}
{{- end -}}
{{- end }}

{{/*
Frontend image reference.
*/}}
{{- define "palaceoftruth.frontendImage" -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.image.frontendRepository (include "palaceoftruth.imageTag" .) }}
{{- end }}

{{/*
Ingress frontend hostname.
Prefer an explicit override, otherwise derive it from ingress.baseDomain.
*/}}
{{- define "palaceoftruth.frontendHost" -}}
{{- if .Values.ingress.frontendHost }}
{{- .Values.ingress.frontendHost }}
{{- else if .Values.ingress.baseDomain }}
{{- .Values.ingress.baseDomain }}
{{- else }}
{{- fail "ingress.baseDomain or ingress.frontendHost must be set" }}
{{- end }}
{{- end }}

{{/*
Ingress API hostname.
Prefer an explicit override, otherwise derive it from ingress.baseDomain.
*/}}
{{- define "palaceoftruth.apiHost" -}}
{{- if .Values.ingress.apiHost }}
{{- .Values.ingress.apiHost }}
{{- else if .Values.ingress.baseDomain }}
{{- printf "%s.%s" (default "api" .Values.ingress.apiSubdomain) .Values.ingress.baseDomain }}
{{- else }}
{{- fail "ingress.baseDomain or ingress.apiHost must be set" }}
{{- end }}
{{- end }}

{{/*
Ingress admin hostname.
Prefer an explicit admin host, otherwise share the API host so path-specific
Ingress annotations can constrain /api/v1/admin without moving runtime APIs.
*/}}
{{- define "palaceoftruth.adminHost" -}}
{{- if .Values.ingress.admin.host }}
{{- .Values.ingress.admin.host }}
{{- else }}
{{- include "palaceoftruth.apiHost" . }}
{{- end }}
{{- end }}

{{/*
Ingress MCP hostname.
Prefer an explicit override, otherwise derive it from ingress.baseDomain.
*/}}
{{- define "palaceoftruth.mcpHost" -}}
{{- if .Values.ingress.mcpHost }}
{{- .Values.ingress.mcpHost }}
{{- else if .Values.ingress.baseDomain }}
{{- printf "%s.%s" (default "mcp" .Values.ingress.mcpSubdomain) .Values.ingress.baseDomain }}
{{- else }}
{{- fail "ingress.baseDomain or ingress.mcpHost must be set when mcp.enabled=true" }}
{{- end }}
{{- end }}

{{/*
Base URL the MCP adapter uses to reach the in-cluster backend.
*/}}
{{- define "palaceoftruth.mcpApiBaseUrl" -}}
{{- if .Values.mcp.apiBaseUrl }}
{{- .Values.mcp.apiBaseUrl }}
{{- else }}
{{- printf "http://%s-backend:8000" (include "palaceoftruth.fullname" .) }}
{{- end }}
{{- end }}

{{/*
DB env vars block — used in backend initContainer, backend container, and worker.
When postgres.enabled=true: reads from CNPG-generated secret and constructs DATABASE_URL.
When postgres.enabled=false: reads DATABASE_URL directly from existingSecret.
*/}}
{{- define "palaceoftruth.dbEnvVars" -}}
{{- if .Values.postgres.enabled }}
- name: DB_USER
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.postgresSecretName" . }}
      key: username
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.postgresSecretName" . }}
      key: password
- name: DB_HOST
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.postgresSecretName" . }}
      key: host
- name: DB_PORT
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.postgresSecretName" . }}
      key: port
- name: DB_NAME
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.postgresSecretName" . }}
      key: dbname
- name: DATABASE_URL
  value: "postgresql+asyncpg://$(DB_USER):$(DB_PASSWORD)@$(DB_HOST):$(DB_PORT)/$(DB_NAME)"
{{- else }}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.appSecretName" . }}
      key: DATABASE_URL
{{- end }}
{{- end }}
