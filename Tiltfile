def required_env(name):
    value = os.getenv(name, '')
    if not value:
        fail('%s is required; launch Tilt through the configured vcdev shim' % name)
    return value

if os.getenv('VCDEV_LAUNCHED', '') != '1':
    fail('VCDEV_LAUNCHED is required; launch Tilt through the configured vcdev shim')

project_namespace = required_env('VCDEV_PROJECT_NAMESPACE')
project_slug = required_env('VCDEV_PROJECT_SLUG')
workspace_slug = required_env('VCDEV_WORKSPACE_SLUG')
base_domain = required_env('VCDEV_BASE_DOMAIN')
registry = required_env('VCDEV_REGISTRY')
registry_project = required_env('VCDEV_REGISTRY_PROJECT')
shared_provider_secret = required_env('VCDEV_SHARED_PROVIDER_SECRET')
registry_pull_secret = required_env('VCDEV_REGISTRY_PULL_SECRET')
required_env('BUILDKIT_HOST')

allow_k8s_contexts(k8s_context())

backend_image = '%s/%s/%s-backend' % (registry, registry_project, project_slug)
frontend_image = '%s/%s/%s-frontend' % (registry, registry_project, project_slug)

custom_build(
    backend_image,
    './tilt/build-with-buildkit.sh backend backend',
    deps=['backend'],
    skips_local_docker=True,
    live_update=[
        sync('backend/app', '/app/app'),
        sync('backend/scripts', '/app/scripts'),
        # All backend-image processes run as PID 1 under the same non-root UID.
        # A successful signal lets Kubernetes restart workers and MCP cleanly.
        run('kill 1'),
    ],
)
custom_build(
    frontend_image,
    './tilt/build-with-buildkit.sh frontend frontend development',
    deps=['frontend'],
    skips_local_docker=True,
    live_update=[
        sync('frontend/src', '/app/src'),
        sync('frontend/public', '/app/public'),
    ],
)

frontend_host = '%s.%s.%s' % (project_slug, workspace_slug, base_domain)
api_host = 'api.%s' % frontend_host
mcp_host = 'mcp.%s' % frontend_host

project_objects = encode_yaml_stream([
    {
        'apiVersion': 'v1',
        'kind': 'Namespace',
        'metadata': {
            'name': project_namespace,
            'labels': {
                'app.kubernetes.io/part-of': project_slug,
                'pod-security.kubernetes.io/enforce': 'restricted',
                'pod-security.kubernetes.io/audit': 'restricted',
                'pod-security.kubernetes.io/warn': 'restricted',
            },
        },
    },
    {
        'apiVersion': 'v1',
        'kind': 'ResourceQuota',
        'metadata': {'name': 'project-structure', 'namespace': project_namespace},
        'spec': {'hard': {
            'count/pods': '50',
            'count/persistentvolumeclaims': '12',
            'requests.storage': '80Gi',
            'services.nodeports': '0',
            'services.loadbalancers': '0',
        }},
    },
])
k8s_yaml(project_objects)

secret_yaml = local(
    './tilt/generate-project-secret.sh %s palace-dev-secrets' % project_namespace,
    quiet=True,
)
k8s_yaml(secret_yaml)

rendered = local('./tilt/render-dev.sh', quiet=True)
k8s_yaml(rendered)

resource_prefix = '%s-palaceoftruth' % project_slug
k8s_resource('%s-frontend' % resource_prefix, links=['https://%s' % frontend_host])
k8s_resource('%s-backend' % resource_prefix, links=['https://%s' % api_host])
k8s_resource('%s-mcp' % resource_prefix, links=['https://%s' % mcp_host])
