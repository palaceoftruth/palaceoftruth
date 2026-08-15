#!/usr/bin/env bash
set -euo pipefail

: "${VCDEV_PROJECT_NAMESPACE:?VCDEV_PROJECT_NAMESPACE is required}"
: "${VCDEV_PROJECT_SLUG:?VCDEV_PROJECT_SLUG is required}"
: "${VCDEV_WORKSPACE_SLUG:?VCDEV_WORKSPACE_SLUG is required}"
: "${VCDEV_BASE_DOMAIN:?VCDEV_BASE_DOMAIN is required}"
: "${VCDEV_REGISTRY:?VCDEV_REGISTRY is required}"
: "${VCDEV_REGISTRY_PROJECT:?VCDEV_REGISTRY_PROJECT is required}"
: "${VCDEV_SHARED_PROVIDER_SECRET:?VCDEV_SHARED_PROVIDER_SECRET is required}"
: "${VCDEV_REGISTRY_PULL_SECRET:?VCDEV_REGISTRY_PULL_SECRET is required}"

frontend_host="${VCDEV_PROJECT_SLUG}.${VCDEV_WORKSPACE_SLUG}.${VCDEV_BASE_DOMAIN}"

helm template "$VCDEV_PROJECT_SLUG" ./chart \
  --namespace "$VCDEV_PROJECT_NAMESPACE" \
  --values ./tilt/values.dev.yaml \
  --set-string "image.registry=$VCDEV_REGISTRY" \
  --set-string "image.backendRepository=$VCDEV_REGISTRY_PROJECT/$VCDEV_PROJECT_SLUG-backend" \
  --set-string "image.frontendRepository=$VCDEV_REGISTRY_PROJECT/$VCDEV_PROJECT_SLUG-frontend" \
  --set-string image.tag=dev \
  --set-string "existingRegistrySecret=$VCDEV_REGISTRY_PULL_SECRET" \
  --set-string "additionalEnvFromSecrets[0]=$VCDEV_SHARED_PROVIDER_SECRET" \
  --set-string "frontend.viteApiProxyTarget=http://$VCDEV_PROJECT_SLUG-palaceoftruth-backend:8000" \
  --set-string "ingress.baseDomain=$frontend_host" \
  --set-string "ingress.frontendHost=$frontend_host" \
  --set-string "ingress.apiHost=api.$frontend_host" \
  --set-string "ingress.mcpHost=mcp.$frontend_host" \
  --set-string "ingress.tlsSecretName=$VCDEV_PROJECT_SLUG-tls" |
  yq '
    del(.spec.template.spec.containers[]?.resources) |
    del(.spec.template.spec.initContainers[]?.resources) |
    del(.spec.jobTemplate.spec.template.spec.containers[]?.resources) |
    del(.spec.jobTemplate.spec.template.spec.initContainers[]?.resources) |
    del(select(.apiVersion == "postgresql.cnpg.io/v1" and .kind == "Cluster").spec.resources)
  '
