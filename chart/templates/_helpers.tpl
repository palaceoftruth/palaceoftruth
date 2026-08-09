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
Memory rollout smoke is a Job, and Job pod templates are immutable. Include the
app image tag in the Job name so Helm replaces it on app/chart upgrades.
*/}}
{{- define "palaceoftruth.memoryRolloutSmokeJobName" -}}
{{- printf "%s-memory-smoke-%s" ((include "palaceoftruth.fullname" .) | trunc 38 | trimSuffix "-") ((include "palaceoftruth.imageTag" .) | trunc 10 | trimSuffix "-") | trunc 63 | trimSuffix "-" -}}
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
Writable Sentinel configuration content. Keep this in one helper so the pod
template checksum changes only when the Sentinel runtime configuration changes,
not when chart metadata changes.
*/}}
{{- define "palaceoftruth.valkeySentinelConfig" -}}
# Valkey Sentinel requires this when the monitored primary is configured
# with Kubernetes service DNS instead of a literal IP address.
sentinel resolve-hostnames yes

# Monitor the primary. Quorum = minimum sentinels that must agree the
# primary is down before triggering a failover.
sentinel monitor {{ .Values.valkey.sentinel.masterName }} {{ include "palaceoftruth.valkeyPrimaryName" . }} 6379 {{ .Values.valkey.sentinel.quorum }}

# How long (ms) a primary can be unreachable before it's considered down.
sentinel down-after-milliseconds {{ .Values.valkey.sentinel.masterName }} {{ .Values.valkey.sentinel.downAfterMilliseconds }}

# Max time (ms) allowed for a failover to complete.
sentinel failover-timeout {{ .Values.valkey.sentinel.masterName }} {{ .Values.valkey.sentinel.failoverTimeout }}

# How many replicas to reconfigure in parallel after a failover.
sentinel parallel-syncs {{ .Values.valkey.sentinel.masterName }} 1

# Sentinel listens on this port.
port 26379
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
Whether the bundled Valkey requires a password on its data port.
*/}}
{{- define "palaceoftruth.valkeyAuthEnabled" -}}
{{- if and .Values.valkey.enabled .Values.valkey.auth.enabled }}true{{ else }}false{{ end -}}
{{- end }}

{{/*
Secret holding the Valkey password. Either operator-supplied (existingSecret,
typically produced by ExternalSecrets) or chart-managed.
*/}}
{{- define "palaceoftruth.valkeyAuthSecretName" -}}
{{- if .Values.valkey.auth.existingSecret }}
{{- .Values.valkey.auth.existingSecret }}
{{- else }}
{{- printf "%s-valkey-auth" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "palaceoftruth.valkeyAuthSecretKey" -}}
{{- .Values.valkey.auth.existingSecretKey | default "valkey-password" }}
{{- end }}

{{/*
Password value for the chart-managed Valkey auth Secret. Rendered in exactly
one place (valkey-auth-secret.yaml); every other manifest references the Secret
by key so the generated value never differs between manifests within a render.
*/}}
{{- define "palaceoftruth.valkeyAuthPassword" -}}
{{- if .Values.valkey.auth.password }}
{{- .Values.valkey.auth.password }}
{{- else }}
{{- $name := printf "%s-valkey-auth" (include "palaceoftruth.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- $existing := lookup "v1" "Secret" .Release.Namespace $name }}
{{- $current := "" }}
{{- if $existing }}
{{- $current = index (default dict $existing.data) (include "palaceoftruth.valkeyAuthSecretKey" .) | default "" }}
{{- end }}
{{- if $current }}
{{- $current | b64dec }}
{{- else }}
{{- randAlphaNum 32 }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Environment block giving application pods the Valkey credentials. REDIS_URL
stays in the ConfigMap without credentials; the password is injected separately
so it is never written to a ConfigMap. The app applies these to both the direct
DSN connection and the Sentinel-discovered primary.
*/}}
{{- define "palaceoftruth.redisAuthEnvVars" -}}
{{- if eq (include "palaceoftruth.valkeyAuthEnabled" .) "true" -}}
- name: REDIS_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.valkeyAuthSecretName" . }}
      key: {{ include "palaceoftruth.valkeyAuthSecretKey" . }}
{{- end }}
{{- end }}

{{/*
Environment block for a Valkey data-node container: the password used to render
the server config, plus REDISCLI_AUTH so exec probes can authenticate without
placing the password in argv. Never apply this to a Sentinel container --
Sentinel is not password-protected and valkey-cli treats an unwanted AUTH as a
connection failure.
*/}}
{{- define "palaceoftruth.valkeyServerAuthEnvVars" -}}
{{- if eq (include "palaceoftruth.valkeyAuthEnabled" .) "true" -}}
- name: VALKEY_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.valkeyAuthSecretName" . }}
      key: {{ include "palaceoftruth.valkeyAuthSecretKey" . }}
- name: REDISCLI_AUTH
  valueFrom:
    secretKeyRef:
      name: {{ include "palaceoftruth.valkeyAuthSecretName" . }}
      key: {{ include "palaceoftruth.valkeyAuthSecretKey" . }}
{{- end }}
{{- end }}

{{/*
Shell fragment that (re)writes the auth directives of a Valkey config file from
$VALKEY_PASSWORD. Stale directives are stripped first so a rotated password is
picked up, and so a Sentinel CONFIG REWRITE cannot leave two conflicting lines.
Takes the config file path as the context.
*/}}
{{- define "palaceoftruth.valkeyAuthConfigScript" -}}
sed -i '/^requirepass /d;/^masterauth /d' {{ . }}
printf 'requirepass %s\n' "$VALKEY_PASSWORD" >> {{ . }}
printf 'masterauth %s\n' "$VALKEY_PASSWORD" >> {{ . }}
# requirepass alone is not enough. Valkey applies `user` directives in a second
# pass after the rest of the config, so a `user default ... nopass` line -- which
# CONFIG REWRITE writes into the data directory on its own -- silently overrides
# requirepass. The server then accepts unauthenticated clients while rejecting
# authenticated ones with "AUTH called without any password configured for the
# default user". Rewrite only the password token, so the rest of the rule
# (sanitize-payload, key and channel patterns) survives untouched.
awk '
  BEGIN { pw = ENVIRON["VALKEY_PASSWORD"] }
  /^user default / {
    line = ""
    set = 0
    for (i = 1; i <= NF; i++) {
      token = $i
      # nopass, >plaintext, #sha256 and <plaintext are the ACL password tokens.
      if (token == "nopass" || token ~ /^[><#]/) {
        if (set) { continue }
        token = ">" pw
        set = 1
      }
      line = (line == "" ? token : line " " token)
    }
    if (set == 0) { line = line " >" pw }
    print line
    next
  }
  { print }
' {{ . }} > {{ . }}.tmp && mv {{ . }}.tmp {{ . }}
chmod 600 {{ . }}
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
Baseline workload hardening.

Every chart-owned pod renders the same pod- and container-level
securityContext so a new workload inherits the hardened defaults instead of
opting into them. Both helpers take a two-element list: the root context and
the override key under podSecurity.overrides (use "" for no override).

Precedence is podSecurity.<pod|container> over the computed baseline, then
podSecurity.overrides.<key>.<pod|container> over that, so a deployment can
relax exactly one workload without restating the baseline.
*/}}
{{- define "palaceoftruth.podSecurityContext" -}}
{{- $root := index . 0 -}}
{{- $key := index . 1 -}}
{{- $security := $root.Values.podSecurity -}}
{{- if $security.enabled -}}
{{- $base := dict
      "runAsNonRoot" true
      "runAsUser" $security.runAsUser
      "runAsGroup" $security.runAsGroup
      "fsGroup" $security.fsGroup
      "seccompProfile" $security.seccompProfile
-}}
{{- $merged := mergeOverwrite $base (deepCopy (default (dict) $security.pod)) -}}
{{- if $key -}}
{{- $override := default (dict) (index (default (dict) $security.overrides) $key) -}}
{{- $merged = mergeOverwrite $merged (deepCopy (default (dict) $override.pod)) -}}
{{- end -}}
securityContext:
  {{- toYaml $merged | nindent 2 }}
{{- end -}}
{{- end }}

{{- define "palaceoftruth.containerSecurityContext" -}}
{{- $root := index . 0 -}}
{{- $key := index . 1 -}}
{{- $security := $root.Values.podSecurity -}}
{{- if $security.enabled -}}
{{- $base := dict
      "allowPrivilegeEscalation" false
      "readOnlyRootFilesystem" true
      "capabilities" (dict "drop" (list "ALL"))
-}}
{{- $merged := mergeOverwrite $base (deepCopy (default (dict) $security.container)) -}}
{{- if $key -}}
{{- $override := default (dict) (index (default (dict) $security.overrides) $key) -}}
{{- $merged = mergeOverwrite $merged (deepCopy (default (dict) $override.container)) -}}
{{- end -}}
securityContext:
  {{- toYaml $merged | nindent 2 }}
{{- end -}}
{{- end }}

{{/*
Writable scratch for a read-only root filesystem.

/tmp is the only path the application writes outside its declared volumes:
uploads stream through tempfile, exports build a zip there, and yt-dlp and
ffmpeg stage media there. HOME is separate because the MCP client writes
~/.hermes/palaceoftruth.json, and a read-only / would fail that at import time.
*/}}
{{- define "palaceoftruth.scratchVolumeMounts" -}}
- name: scratch-tmp
  mountPath: /tmp
- name: scratch-home
  mountPath: {{ .Values.podSecurity.homeDir | quote }}
{{- end }}

{{- define "palaceoftruth.scratchVolumes" -}}
- name: scratch-tmp
  emptyDir: {}
- name: scratch-home
  emptyDir: {}
{{- end }}

{{/*
HOME for the backend image. Kept in one place so the chart, the Dockerfile and
the compose stack cannot drift apart.
*/}}
{{- define "palaceoftruth.homeEnv" -}}
- name: HOME
  value: {{ .Values.podSecurity.homeDir | quote }}
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
