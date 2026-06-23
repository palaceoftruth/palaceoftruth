# Palace Grafana Dashboards

This directory contains importable Grafana dashboards for Palace operations.

## Palace Operations

`palace-operations.json` is built for the central operations Grafana with the
Mimir datasource. It defaults to:

- cluster: `k3s-lab`
- namespace: `palace-sarvent`
- backend job: `palace-sarvent-backend`

The dashboard covers:

- Palace API scrape health, request rate, average latency, and 5xx rate.
- ARQ queue depth, deferred depth, oldest queued job age, and recent failures.
- dirty Palace backlog and indexed corpus size.
- memory and webhook job health.
- item source/status mix.
- Kubernetes deployment availability and container restarts.

Import through Grafana UI or API using the dashboard JSON as the canonical
source. Keep queries on low-cardinality labels exported by
`backend/app/services/prometheus_metrics.py`.
