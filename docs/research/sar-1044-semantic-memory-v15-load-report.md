# SAR-1044 Semantic Memory v1.5 Load Report

Generated: `2026-07-09T22:24:49.157722+00:00`

## Fixture

- Scope: `agent/iris`
- Entries: `10000`
- Semantic candidate limit: `200`
- Production data used: `false`
- Service path: `semantic_recall_memory` with 10k total considered rows and the API-bounded candidate window
- Service path: `RetentionService.retain` extracted write with deterministic LLM output and canonical memory write

## Results

- Recall p50: `0.003111` seconds
- Recall p95: `0.00377` seconds
- Retain extraction p50: `0.000511` seconds
- Retain extraction p95: `0.00067` seconds
- Queue/backpressure: covered by `palace_arq_queue_depth`, `palace_arq_worker_queue_depth`, and `palace_arq_recent_latency_seconds`.
- Passed: `true`

## Regenerate

```bash
python3 scripts/semantic_memory_v15_load_report.py --output-json /tmp/sar-1044-load.json --output-md /tmp/sar-1044-load.md
```
