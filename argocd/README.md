# ArgoCD

This public repository does not carry environment-specific ArgoCD Application
resources. Keep cluster names, private registry coordinates, secret-manager item
IDs, DNS targets, and release-promotion runbooks in a private deployment
repository.

Use the reusable Helm chart in `../chart` from your own ArgoCD Application or
ApplicationSet.
