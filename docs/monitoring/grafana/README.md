# Palace Grafana Dashboards

This directory contains importable Grafana dashboards for Palace operations.

## Palace Operations

`palace-operations.json` is built for an operations Grafana with a Mimir
datasource. It defaults to example label values:

- cluster: `example-cluster`
- namespace: `palaceoftruth`
- backend job: `palaceoftruth-backend`

The dashboard covers:

- Palace API scrape health, request rate, average latency, and 5xx rate.
- ARQ worker availability/heartbeat freshness, queue depth, deferred depth,
  oldest queued job age, and recent failures.
- dirty Palace backlog and indexed corpus size.
- memory and webhook job health.
- item source/status mix.
- Kubernetes deployment availability and container restarts.

Import through Grafana UI or API using the dashboard JSON as the canonical
source. Keep queries on low-cardinality labels exported by
`backend/app/services/prometheus_metrics.py`.

## Retrieval latency percentiles

Retrieval and embedding latency use fixed Prometheus histogram buckets, so
replicas can be aggregated safely. Calculate percentiles from `rate()` of the
bucket series; do not average per-replica quantiles:

```promql
histogram_quantile(0.50, sum by (le, endpoint, stage) (rate(palace_retrieval_stage_duration_seconds_bucket[5m])))
histogram_quantile(0.95, sum by (le, endpoint, stage) (rate(palace_retrieval_stage_duration_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le, endpoint, stage) (rate(palace_retrieval_stage_duration_seconds_bucket[5m])))
```

Retrieval request labels are limited to endpoint and outcome. Intent,
route-confidence, fallback, abstain, empty-result, and budget-truncation are
separate bounded classification series so their combinations cannot multiply
request cardinality. Result labels use fixed endpoint, rank-band, freshness,
trust-class, and source-support classifications. Embedding labels are fixed
provider, input-type, status, and failure-kind classifications. Metrics never
use tenant, query, URL, item, job, correlation, or fingerprint values as labels.

Durable database gauges expose oldest job age and source refresh age/due state.
They intentionally report aggregate bounded classes rather than individual job
or source identifiers.

## Worker alerting

Worker process liveness, dependency startup, and queue heartbeat freshness are
separate signals. Kubernetes restarts an exited ARQ process. The startup wrapper
waits for database and Valkey/Sentinel dependencies. The readiness probe uses
an ARQ heartbeat keyed to the current pod, so a sibling replica cannot make a
replacement pod ready. Metrics aggregate those expiring heartbeats by queue;
an idle queue remains healthy while a missing worker is explicit.

Alert when any scraped worker group has no fresh heartbeat for five minutes:

```promql
max_over_time(palace_arq_worker_available[5m]) == 0
```

Use `palace_arq_worker_heartbeat_age_seconds` for dashboards and early warning.
Use `palace_arq_worker_instances` to compare fresh consumers with the desired
worker replica count.
Do not alert on `palace_arq_worker_queue_depth == 0`; zero is the normal idle
state. The `key` label names a bounded logical worker group and `queue` names
the ARQ queue. Multiple logical groups may intentionally share one queue.
