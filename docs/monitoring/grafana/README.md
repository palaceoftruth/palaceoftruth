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
- retrieval p95 latency, outcomes, fallback use, and stale-result rate.
- embedding failures, durable job age, and watched-source refresh health.
- k3s-lab remote-write failures and pending-sample backlog.

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

The backend ServiceMonitor sets `honorLabels: true`. This preserves the
application's bounded `endpoint` label instead of rewriting it to
`exported_endpoint` when Prometheus attaches its own scrape-target label.
After a chart rollout, verify the rendered resource and live label shape:

```bash
helm template palaceoftruth chart --set metrics.serviceMonitor.enabled=true \
  | yq 'select(.kind == "ServiceMonitor" and .metadata.name == "palaceoftruth-backend") | .spec.endpoints[0].honorLabels'

# Run against the Prometheus HTTP API for the deployed Palace environment.
curl -G "$PROMETHEUS_URL/api/v1/query" \
  --data-urlencode 'query=count by (endpoint, exported_endpoint, stage) (palace_retrieval_stage_duration_seconds_count)'
```

The successful query returns the bounded application endpoint values
(`retrieve`, `retrieve_agent`, `semantic_recall`, or `other`) in `endpoint`.
It must not return `exported_endpoint` in place of that application label.
After that label-shape check, use the `sum by (le, endpoint, stage)` grouping
for replica-aggregated p50, p95, and p99 queries above.

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

## Dashboard ownership and reconciliation

This repository owns the canonical dashboard JSON. Environment repositories own
Prometheus discovery and alert-rule resources. Central Grafana receives this
dashboard through a controlled UI or API import; it is not provisioned from the
`k3s-lab` Flux application path. After importing an updated JSON file, verify the
dashboard UID is `palace-operations`, select the Mimir datasource, and set the
cluster, namespace, and backend-job variables for the target environment.

The central endpoint is `https://grafana.lgtm.sarvent.cloud`. The SarvEnt
platform operator owns the import and supplies a short-lived Grafana service
account token through the approved secret manager; never store or print that
token. From this directory, create the Grafana API envelope and import it:

```bash
jq -n --slurpfile dashboard palace-operations.json \
  '{dashboard: $dashboard[0], overwrite: true, message: "Update Palace operations dashboard"}' \
  > /tmp/palace-dashboard-import.json

curl -fsS -X POST "${GRAFANA_URL:-https://grafana.lgtm.sarvent.cloud}/api/dashboards/db" \
  -H "Authorization: Bearer ${GRAFANA_SERVICE_ACCOUNT_TOKEN:?required}" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/palace-dashboard-import.json
```

Verify the UID and export the reviewed dashboard without logging the token:

```bash
curl -fsS "${GRAFANA_URL:-https://grafana.lgtm.sarvent.cloud}/api/dashboards/uid/palace-operations" \
  -H "Authorization: Bearer ${GRAFANA_SERVICE_ACCOUNT_TOKEN:?required}" \
  | jq '.dashboard | del(.id, .version, .iteration)' \
  > /tmp/palace-dashboard-export.json

jq 'del(.id, .version, .iteration)' palace-operations.json \
  > /tmp/palace-dashboard-canonical.json
diff -u /tmp/palace-dashboard-canonical.json /tmp/palace-dashboard-export.json
```

Treat the checked-in JSON as authoritative. Before accepting a central Grafana
edit, export it and compare it with this file so UI-only drift does not silently
replace the reviewed queries.

## Retrieval and freshness alert runbook

Start with read-only checks:

1. Confirm the Palace ServiceMonitor target is up and metric scrape errors are
   zero.
2. Compare retrieval error, latency, stale-result, and fallback panels across
   both backend replicas before changing routing or thresholds.
3. For job or source-refresh alerts, inspect aggregate queue age, worker
   heartbeat, oldest durable job age, and refresh outcomes. Do not retry or
   delete production jobs from an alert response.
4. For remote-write alerts, inspect
   `prometheus_remote_storage_samples_failed_total`,
   `prometheus_remote_storage_samples_pending`, Prometheus logs, and Mimir
   ingestion errors. Fix the rejected sample source before tuning queue
   capacity or relabeling.

Alert rules are selected only when their `release` label matches the live
Prometheus `ruleSelector`, and the namespace is permitted by
`ruleNamespaceSelector`. Verify both selectors after every monitoring-stack
change.

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
