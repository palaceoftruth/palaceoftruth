# Official Hermes lifecycle support (SAR-1403 / SAR-1404)

This candidate is a review checkpoint, NOT a complete release or rollout approval.

| Official host | Context/provider-local regressions | Host-global flush | Real resume session/scope attribution | Old host submission/new session quota |
| --- | --- | --- | --- | --- |
| installed b65cd56cfcdc33cbf6657326b5711a7c23cd1485 | pass | unsupported | pass | fails |
| prior upstream c57331677d7fc46298a7159cf3a650853f0966c5 | pass | unsupported | pass | fails |
| current upstream 1f2b9e08644c6de4a5b5c50d0e4676a19c5577c5 | pass | unsupported | pass | fails |

Provider hooks use only the official MemoryProvider ABC and public spawn_context_thread helper (copy_context fallback for older helper-less hosts). All accepted automatic turn/mirror writes enter one FIFO and retain their submission ContextVars, scope/session metadata and quota bucket. flush_pending is a plugin-local barrier, not an ingestion durability receipt. shutdown closes admission and records bounded timeout/failure state. A timeout does not cancel a live transport; accepted work remains tracked and can finish in the running process. No process-crash durability claim is made.

The official manager invokes sync_turn asynchronously. Its flush only sees its executor, not a nested provider worker. The former scope probe directly changed private provider scope configuration after submission. That is not a supported runtime transition and is withdrawn as production scope-defect evidence. Actual official /resume-/branch boundary testing preserves the old payload session and configured session scope. It does expose a provider defect: an old host submission dispatched after resume consumes the new session's quota. Provider-local admission tests did not cover that upstream queue delay.

check_hermes_public_lifecycle.py now calls the actual official CLI boundary with offline application fixtures; it does not modify host functions or private scope configuration. Strict exit 2 remains mandatory for the global drain gap and stale host-submission quota defect. Valid session/scope attribution remains asserted. CI does not skip or xfail these failures. The weekly workflow is offline; it is NOT a live canary.

Supported plugin observer hooks catch callback failures and continue. The documented middleware kinds wrap LLM/tool calls, not MemoryManager submission/flush. A new external application could own direct public manager/provider calls; integration into AIAgent is not established. No documented universal wrapper controls built-in CLI, gateway, cron, API, TUI and ACP entrypoints. See [the feasibility proof](hermes-lifecycle-feasibility.md). No universal readiness integration is installed or claimed.

Reproduce:

    python scripts/check_hermes_public_lifecycle.py --host /path/to/official/hermes-agent --receipt receipt.json

Use an isolated HOME/HERMES_HOME and the official host dependency environment. Network access is denied by an audit hook. Auth/transport substitutions apply to plugin fixtures only; host classes/functions are not replaced.

Release gates:

1. Barbara reviews exact source candidate and evidence.
2. A supported external orchestration integration must prove admission/scope ordering and manager-plus-provider flush before session/scope switch; none is installed by this candidate. A scope decision is required if supported entrypoints cannot provide it. Do not import PR535's unlanded host patch.
3. All strict real-host CI checks must pass, not merely provider tests.
4. Weekly live canary remains manual under SAR-1407 review authorization. Record exact reviewed plugin SHA/hash, official host SHA, explicit canary identifier/scope, durable memory job status, exact-scope readback, health, backup and rollback receipt. Do not count queued acceptance as durability. Do not mutate another profile or restart an active orchestrator.

PR535 refs and candidate549 refs remain preserved. SAR-1405/1406 files and worker clones are excluded.
