{{/*
Shared redis_exporter sidecar and password volume. The exporter reads an
optional password from a mounted Secret file so credentials never appear in
process arguments or rendered environment values.
*/}}
{{- define "palaceoftruth.valkeyExporterContainer" -}}
{{- $root := .root -}}
{{- $secretConfigured := or $root.Values.valkey.metrics.existingSecret $root.Values.valkey.metrics.passwordFileKey -}}
- name: valkey-exporter
  image: {{ $root.Values.valkey.metrics.image }}
  imagePullPolicy: {{ $root.Values.valkey.metrics.imagePullPolicy }}
  env:
    - name: REDIS_ADDR
      value: {{ .address | quote }}
    {{- if $secretConfigured }}
    - name: REDIS_PASSWORD_FILE
      value: /run/secrets/valkey-exporter/password.json
    {{- end }}
  ports:
    - name: metrics
      containerPort: {{ $root.Values.valkey.metrics.port }}
      protocol: TCP
  resources:
    {{- toYaml $root.Values.valkey.metrics.resources | nindent 4 }}
  securityContext:
    {{- toYaml $root.Values.valkey.metrics.securityContext | nindent 4 }}
  readinessProbe:
    httpGet:
      path: /metrics
      port: metrics
    initialDelaySeconds: 5
    periodSeconds: 10
  livenessProbe:
    httpGet:
      path: /metrics
      port: metrics
    initialDelaySeconds: 10
    periodSeconds: 20
  {{- if $secretConfigured }}
  volumeMounts:
    - name: valkey-exporter-password
      # Mount the projected directory rather than a subPath so Kubernetes can
      # update the file when the source Secret rotates.
      mountPath: /run/secrets/valkey-exporter
      readOnly: true
  {{- end }}
{{- end }}

{{- define "palaceoftruth.valkeyExporterPasswordVolume" -}}
{{- $secretConfigured := or .Values.valkey.metrics.existingSecret .Values.valkey.metrics.passwordFileKey -}}
{{- if $secretConfigured }}
- name: valkey-exporter-password
  secret:
    secretName: {{ required "valkey.metrics.existingSecret is required when password-file wiring is configured" .Values.valkey.metrics.existingSecret }}
    items:
      - key: {{ required "valkey.metrics.passwordFileKey is required when password-file wiring is configured" .Values.valkey.metrics.passwordFileKey }}
        path: password.json
{{- end }}
{{- end }}
