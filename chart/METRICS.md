# Authenticated per-pod metrics scrapes

Enable `metrics.serviceMonitor.enabled` only when the Prometheus Operator CRD
is installed. The monitor discovers individual backend endpoints and uses the
application Secret's `API_KEY` as its bearer credential. It does not scrape the
load-balanced Service address.

The backend's `TrustedHostMiddleware` otherwise rejects the discovered pod IP
with HTTP 400 before metrics authorization runs. With the monitor enabled, the
Deployment injects `PALACE_METRICS_POD_IP` from the Downward API's `status.podIP`
and appends that exact address to the backend's effective `TRUSTED_HOSTS`:

```yaml
- name: PALACE_METRICS_POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
- name: TRUSTED_HOSTS
  value: "$(TRUSTED_HOSTS),$(PALACE_METRICS_POD_IP)"
```

Kubelet loads `envFrom` first, then expands explicit `env` entries in order.
The self-reference therefore preserves the effective ConfigMap/Secret value,
including an existing Secret override, and appends only the current pod's IP.
Replacement pods get their own current address without hard-coded pod IPs.
Workers and installations with the ServiceMonitor disabled are unchanged.

This adds no wildcard, CIDR, `0.0.0.0`, Host-validation bypass, or auth bypass.
Each replica accepts its own address, not the other replica's address. Metrics
still return 401 for absent/invalid bearer credentials. Public/API/service host
entries remain intact. This fixes direct **IPv4** endpoint scrapes; it does not
claim to resolve Starlette's separate IPv6 Host parsing limitations.

## Why not override Host or scrape the Service?

Prometheus reserves `Host` in `http_headers`. Promtool 3.12.0 rejects it with
`setting header "Host" is not allowed`. Kubernetes probes' `httpHeaders` support
does not imply Prometheus scrape support. Replacing every target with a Service
VIP would lose deterministic per-pod scraping. Pod-DNS rewriting and an extra
metrics listener are unnecessary here.

## Verification

From `backend/`, with Helm on PATH and backend test dependencies installed:

```sh
python -m pytest tests/test_helm_*.py tests/test_system_api.py -q
helm lint ../chart
helm lint ../chart --set metrics.serviceMonitor.enabled=true --set backend.replicas=2
```

`test_helm_metrics_hosts.py` renders the chart and models kubelet's ordered env
expansion, then exercises actual Starlette host validation for two different
pod addresses. Its integration cases use the real metrics router, credential
dependency, and renderer with the existing database test double. They verify
200 with a valid test bearer, 401 without valid credentials, and 400 for
untrusted hosts. The monitor's Secret reference and unmodified target discovery
are also asserted. These are local tests, not proof of a live rollout.

## References

- [Kubernetes dependent environment variables](https://kubernetes.io/docs/tasks/inject-data-application/define-interdependent-environment-variables/)
- [Kubelet envFrom/ordered expansion implementation](https://github.com/kubernetes/kubernetes/blob/v1.34.0/pkg/kubelet/kubelet_pods.go)
- [Prometheus reserved HTTP headers](https://github.com/prometheus/common/blob/main/config/headers.go)
