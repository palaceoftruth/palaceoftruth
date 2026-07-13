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

## Valkey and Sentinel telemetry

The chart can add `oliver006/redis_exporter` sidecars to bundled Valkey pods.
Telemetry is disabled by default. Set `valkey.metrics.enabled=true` to cover the
standalone Valkey pod or every primary, replica, and Sentinel pod; also set
`valkey.metrics.serviceMonitor.enabled=true` when the Prometheus Operator CRD is
installed. The generated ServiceMonitor selects only Services labeled
`palaceoftruth.io/valkey-metrics=true` and scrapes their named `metrics` port.

Unauthenticated Valkey deployments need no credential values. When ACL auth is
enabled, configure `valkey.metrics.existingSecret` and
`valkey.metrics.passwordFileKey` together. That Secret key must contain
redis_exporter's JSON address-to-password map, not a raw password. The Secret
directory is mounted read-only without `subPath`, allowing Kubernetes Secret
projection updates to reach the container after rotation. The exporter reads
the password file at startup, so roll the affected pods after rotating the
Secret. Credential values are not included in exporter arguments, environment
values, labels, or monitoring resources.

Use the exact localhost target keys rendered by the chart. A standalone
deployment needs the Valkey key; Sentinel mode needs both keys because its
exporters scrape Valkey and Sentinel processes separately:

```json
{
  "redis://127.0.0.1:6379": "<valkey-password>",
  "redis://127.0.0.1:26379": "<sentinel-password>"
}
```

Alert on exporter scrape failure or `redis_up == 0`, and use the exported role,
replication, and Sentinel families to diagnose quorum or replica health. These
signals are observational: dashboards and alerts must never issue `SENTINEL
RESET`, prune peer records, or trigger failover automatically. A stale Sentinel
peer can remain visible after pod replacement while the live quorum is healthy,
so pair peer-count anomalies with quorum and current-peer health before paging.
Set `valkey.metrics.prometheusRule.enabled=true` to render release- and
namespace-bounded alerts for missing/down exporter targets, `redis_up == 0`, and
failed Sentinel CKQUORUM telemetry when that Sentinel metric family exists.
