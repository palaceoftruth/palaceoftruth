# Generic remote Tilt development

This Tilt setup targets a remote Kubernetes development workspace. It has no
default cluster, registry, namespace, domain, or secret-manager identity.

A private launcher must set these variables before Tilt starts:

- `VCDEV_LAUNCHED=1`
- `VCDEV_PROJECT_NAMESPACE`
- `VCDEV_PROJECT_SLUG`
- `VCDEV_WORKSPACE_SLUG`
- `VCDEV_BASE_DOMAIN`
- `VCDEV_REGISTRY`
- `VCDEV_REGISTRY_PROJECT`
- `VCDEV_SHARED_PROVIDER_SECRET`
- `VCDEV_REGISTRY_PULL_SECRET`
- `BUILDKIT_HOST`

The selected Kubernetes context must be the intended development cluster. The
Tiltfile fails before loading resources when the launcher marker or a required
value is absent.

The shared provider and registry Secrets must already exist in the project
namespace. Tilt creates the project namespace, structural quota, disposable
application Secret, database, Valkey, workloads, storage, Services, and
Ingresses. `tilt down` removes those project resources.

Builds use remote BuildKit and target `linux/amd64`. The local Docker daemon is
not used.
