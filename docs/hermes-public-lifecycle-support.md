# Official Hermes lifecycle support (SAR-1403 / SAR-1404)

This candidate is a review checkpoint, NOT a complete release or rollout approval.

| Official host | Context | Provider FIFO/snapshot/quota/drain | Host-global flush | Host-submission scope snapshot |
| --- | --- | --- | --- | --- |
| installed b65cd56cfcdc33cbf6657326b5711a7c23cd1485 | pass | pass | unsupported | unsupported |
| upstream c57331677d7fc46298a7159cf3a650853f0966c5 | pass | pass | unsupported | unsupported |

Provider hooks use only the official MemoryProvider ABC and public spawn_context_thread helper (copy_context fallback for older helper-less hosts). All accepted automatic turn/mirror writes enter one FIFO and retain their submission ContextVars, scope/session metadata and quota bucket. flush_pending is a plugin-local barrier, not an ingestion durability receipt. shutdown closes admission and records bounded timeout/failure state. A timeout does not cancel a live transport; accepted work remains tracked and can finish in the running process. No process-crash durability claim is made.

The official manager invokes sync_turn asynchronously. No public prepare/admission API is present in either tested host. Therefore the provider cannot capture mutable scope at sync_all submission time before dispatch. The official executor flush also cannot see a nested provider worker. check_hermes_public_lifecycle.py imports and exercises the actual manager and returns exit 2 when either strict requirement fails. CI does not skip or xfail these failures. The weekly workflow is offline; it is NOT a live canary.

Reproduce:

    python scripts/check_hermes_public_lifecycle.py --host /path/to/official/hermes-agent --receipt receipt.json

Use an isolated HOME/HERMES_HOME and the official host dependency environment. Network access is denied by an audit hook. Auth/transport substitutions apply to plugin fixtures only; host classes/functions are not replaced.

Release gates:

1. Barbara reviews exact source candidate and evidence.
2. A supported external orchestration integration must prove admission/scope ordering and manager-plus-provider flush before session/scope switch; none is installed by this candidate. A scope decision is required if supported entrypoints cannot provide it. Do not import PR535's unlanded host patch.
3. All strict real-host CI checks must pass, not merely provider tests.
4. Weekly live canary remains manual under SAR-1407 review authorization. Record exact reviewed plugin SHA/hash, official host SHA, explicit canary identifier/scope, durable memory job status, exact-scope readback, health, backup and rollback receipt. Do not count queued acceptance as durability. Do not mutate another profile or restart an active orchestrator.

PR535 refs and candidate549 refs remain preserved. SAR-1405/1406 files and worker clones are excluded.
