# Kubernetes Manifests

Environment-specific Kubernetes manifests have moved out of this repository.

Use the Helm chart in `../chart` for portable installs. Keep raw manifests,
ExternalSecrets mappings, ingress hostnames, DNS targets, and registry pull
secret wiring in a private deployment repository for each operator environment.
