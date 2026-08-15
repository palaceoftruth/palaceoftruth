#!/usr/bin/env bash
set -euo pipefail

namespace="${1:?namespace is required}"
name="${2:?secret name is required}"

if kubectl -n "$namespace" get secret "$name" >/dev/null 2>&1; then
  kubectl -n "$namespace" get secret "$name" -o yaml |
    yq 'del(.metadata.creationTimestamp, .metadata.resourceVersion, .metadata.uid, .metadata.managedFields)'
  exit 0
fi

random_hex() {
  openssl rand -hex "$1"
}

api_key="$(random_hex 32)"
admin_secret="$(random_hex 32)"
credential_pepper="$(random_hex 32)"
valkey_password="$(random_hex 24)"

printf '%s\n' \
  'apiVersion: v1' \
  'kind: Secret' \
  "metadata:" \
  "  name: $name" \
  "  namespace: $namespace" \
  'type: Opaque' \
  'stringData:' \
  "  API_KEY: $api_key" \
  "  PALACEOFTRUTH_ADMIN_SECRET: $admin_secret" \
  "  CREDENTIAL_PEPPER: $credential_pepper" \
  "  VALKEY_PASSWORD: $valkey_password"
