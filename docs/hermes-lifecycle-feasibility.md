# SAR-1403 / SAR-1404: supported lifecycle feasibility decision

## Targeted quota repair follow-up

The delayed-host-submission quota defect described below is historical at head378ccc71ae3e09ded61b6677f92fb2d86e3f4e7f and is now repaired. Quota ownership follows the explicit submitted session, not the current session at delayed dispatch. Immutable owner identity shares cumulative session counters across turn buckets; returning to a retained session or rewinding does not replenish that session's cap. Explicit mirror metadata uses the same ownership rule. Concurrent queued/active jobs pin their owner until completion or admission rollback.

LRU retention targets 64 TOTAL owner records, including current and queued/active pins, not 64 extra idle records. Pinned owners are never evicted; necessary pinned overflow is retained and pruned toward 64 after drain. Idle evicted sessions start a new retention window when revisited. This is not an infinite session ledger or a claim that unobserved host-queued work can be pinned. New tests retain the real official resume fixture and assert quota PASS with host drain still FAIL/exit2. No host contract or strict gate was changed. The remaining host-global gap and entrypoint feasibility decision below still apply.

The exact minimal semantic upstream proposal is preserved separately in [hermes-upstream-lifecycle-api-proposal.md](hermes-upstream-lifecycle-api-proposal.md). It introduces no imaginary symbol/signature and no upstream issue was opened.

## Historical feasibility evidence at head378ccc71

## Result

No full-contract candidate is established within the approved plugin/external-only boundary for the existing production entrypoints. Keep PR550 a draft and keep strict CI red. This is a bounded feasibility gate for the inspected official versions and documented APIs, not a claim that no future upstream API or replacement application can solve it.

No host core edit, fork, monkeypatch, runtime replacement, deployment, restart or live memory write was used. SAR-1405/1406 branches and artifacts are excluded.

## Exact versions and evidence

- Palace original lifecycle candidate: a368363dedf547da667c7b8eab32f3ecdd1f73ea; based on candidate549 403b8f46ff7fd10cb0b7f14b537cdd2a32ffee40.
- Official installed Hermes: b65cd56cfcdc33cbf6657326b5711a7c23cd1485.
- Earlier official upstream/hosted run: c57331677d7fc46298a7159cf3a650853f0966c5.
- Official upstream moved during investigation. New detached, unmodified checkout tested: 1f2b9e08644c6de4a5b5c50d0e4676a19c5577c5. Do not call the earlier SHA latest.
- Official documentation inspected: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins and https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin.
- Local evidence root: /home/asarver/.hermes/cache/scratch/sar1402-completion/lifecycle/feasibility/ . JSON receipts, logs, scripts, fleet inventory and hashes are durable there. The official host sources are unchanged; offline provider/auth and application fixtures are identified in each probe.

## Scope probe correction: do not weaken the valid requirement

The original strict probe changed provider._scope_type and provider._scope_key directly while the official executor was blocked. Plugin source assigns those configuration fields in construction/initialization; inspected runtime session changes do not change them. That synthetic mutation is withdrawn as proof of a supported production scope defect. Prior hosted observations remain historical, not production scope acceptance evidence.

A new event-driven test blocks the real manager using an offline provider prefetch hook, submits an old-session write, then invokes the unchanged official CLI /resume-/branch helper, hermes_cli.cli_commands_mixin._sync_agent_to_session. It uses small offline agent/CLI fixtures, not a host function replacement. Explicit old session ID and configured session scope remain intact. Independent agent-scope diagnostic receipts also retain the initialized offline-agent scope.

The valid requirement remains: all supported session/identity transitions must retain submission identity and quota ownership. A real failure now has stronger evidence: the old manager submission is admitted by the provider only after resume; it consumes the NEW session quota, changing its count from 0 to 1. Existing provider-local tests exercise old jobs already admitted to the provider, not this earlier manager-queue delay. This is a provider accounting defect exposed through the official boundary, not a hypothetical profile mutation. No general multi-profile acceptance is claimed.

## Host and provider failure classification

| Observation | Classification | Evidence/owner |
| --- | --- | --- |
| MemoryManager.flush_pending returns true while nested provider transport is blocked | Supported-host integration gap relative to parent contract; manager promises executor task completion, not nested provider completion | Actual official manager, installed/prior/current upstream; SAR-1404 gate |
| Delayed old-session job consumes new session quota | Provider defect | Actual official CLI resume boundary and manager on all three host SHAs; SAR-1403 |
| Old payload session and configured session scope remain old | Passing supported transition observation | Corrected strict probe; retain as an acceptance assertion |
| Private scope change produces new workspace scope | Artificial old verifier scenario | Withdraw production-defect claim; preserve historical receipt |
| Lifecycle barrier callback raises but execution continues | Documented observer behavior, not fail-closed admission authority | Real PluginContext.register_hook + PluginManager.invoke_hook; failure is caught and later callback runs on all three hosts |
| Prior hosted checks fail exit 2 after 207 provider tests pass | Executed safety probe failure, not bootstrap, dependency, auth or runner failure | Run 38104438372; jobs 114366819848 and 114366819952; original receipt contains synthetic scope criterion and genuine nested drain finding |

Backend fast hosted run 38104438379/job 114366835095 passed 2250 tests, with 3 skipped and 1 xfailed. That does not override strict failure. Current upstream provider regression suite independently passed 207 tests. No remote ingestion durability or crash recovery was exercised.

## Why the inspected supported extensions cannot close the full gate

1. MemoryProvider.sync_turn is documented as nonblocking. Its ABC exposes initialization, per-turn sync, prefetch and lifecycle callbacks, not a host-submission admission token or a host-global nested-work receipt. Source: agent/memory_provider.py:133 onward; agent/memory_manager.py:599-641.
2. MemoryManager.flush_pending:674-695 waits for an executor sentinel. Completion proves earlier manager tasks ran; sync_turn can return while provider-owned transport continues. A plugin-local flush cannot change that method's success condition. The offline strict probe proves this on unmodified hosts.
3. The available middleware kinds are llm_request, llm_execution, tool_request and tool_execution. They surround model/tool calls, not sync_all/flush_pending. Source: hermes_cli/middleware.py:1-214. Provider turn sync is later in finalization, agent/turn_finalizer.py:771-775. LLM middleware completion is not provider write completion.
4. General lifecycle hooks are observers. PluginManager.invoke_hook catches failures and keeps running; observer results do not define manager flush success. Source: hermes_cli/plugins.py around register_hook and invoke_hook. The public-hook probe confirms a failed on_session_finalize barrier still permits the next observer to run. Blocking until success sometimes is not a bounded, fail-closed contract when timeout/failure is ignored.
5. On the inspected upstream, pre_command is observer-only: hermes_cli/plugins.py:1925-1945 ignores directive-shaped returns and catches dispatch errors. It cannot veto or rewrite a command on this host. pre_gateway_dispatch is a real narrower gateway admission seam, not universal completion authority. Neither supplies every internal memory-tool write, session transition, cron/API/TUI/ACP ingress, or manager nested-write acknowledgement.
6. The namespaced plugin event bus is cooperative plugin publication/subscription, not a replacement for host admission/flush. Core producers are not obliged to publish a durable provider receipt through it.
7. An outer shell/service wrapper can observe process start/exit. It cannot snapshot each request in a persistent gateway, await a provider object inside that process, or turn a timeout into the host's failure result without an explicit IPC/lifecycle contract. None is established by the inspected APIs.
8. A new external application can own direct public MemoryManager/MemoryProvider calls, as the probes do. This does not establish a supported admission/barrier injection into AIAgent or the built-in production callers. Any SDK-runner integration needs its own documented boundary and acceptance proof; do not reach into AIAgent's private manager to claim one. Replacing built-in agent factories or injecting manager wrappers would cross the prohibited hidden-internals boundary.

Therefore no actual readiness wiring is asserted. Do not fake it with a readiness file that no production entrypoint checks. Synchronous transport in sync_turn would need a supported contract change; it is not accepted as a repair to the documented nonblocking provider API.

## Production entrypoint coverage ledger

Source references below are from the current official checkout. This ledger covers the host families implicated by the requested all-production contract. It is not an inventory of every active cron/job/profile.

| Family | Actual ingress/execution | Supported seam and remaining gap |
| --- | --- | --- |
| CLI interactive/resume/branch | hermes_cli/cli_chat_turn_mixin.py:369; cli_commands_mixin.py shared _sync_agent_to_session | pre_command is observer-only, not a command veto; historical resume quota defect repaired; manager nested barrier absent |
| CLI single query/oneshot | hermes_cli/cli_single_query.py:84,223,246; oneshot.py:609 | Direct run_conversation; no universal command observer establishes provider completion |
| Channel gateway | gateway/run_inbound.py pre_gateway_dispatch; run_turn_runner.py:1724; run_turn.py:2489 | Gateway ingress rewrite/drop is real but does not control all other families or manager flush |
| Scheduled cron | cron/scheduler.py:2420-2434 | Own AIAgent construction/run and cleanup; replacing its factory is not supported plugin readiness wiring |
| HTTP/API gateway | gateway/platforms/api_server.py:4290; api_server_runs.py:830 | Direct conversation calls, separate response/run lifecycle; no proven provider receipt requirement |
| TUI/desktop gateway | tui_gateway/prompt_turn.py:811; methods_prompt.py:1050-1052; methods_tools.py session switch | Prompt/tool/session surfaces not covered by CLI pre_command |
| ACP | acp_adapter/server.py:810; session.py:571 | Separate caller and lifecycle; no universal pre-command/provider-drain authority |
| Direct SDK/embedded agent | run_agent.AIAgent / run_conversation | Caller can own explicit coordination, but only for that caller; not built-in callers above |

The shared turn finalizer fires observer events and enqueues memory sync; its comment at agent/turn_finalizer.py:793-794 explicitly leaves provider session-end/shutdown to the owning CLI/gateway. MemoryManager.commit_session_boundary_async:747-778 serializes end/switch for /new with history, but resume/branch take a different direct boundary. Do not generalize one successful boundary to all transitions.

## Current fleet/access inventory

Eleven established targets were attempted. Seven returned on-disk plugin evidence; four did not. Counts were computed from saved JSON, not inferred from descriptions.

- Barbara: user plugin 1.0.40, init SHA256 e3aebc86c0a66520c3f279232c61d03885231e5826484428e12533c495d49be5; installed official host SHA above.
- Iris and Eve: user plugin 1.0.39, init SHA256 0842e8c1ea1c5d8441767ae22fd7e3ebbd689fe388510c5458b92dfef3469702; on-disk host e1e72f109c9540d6ed2aceeaf1b5bfcacbab5efc. This older host is not the installed/current upstream acceptance matrix.
- k3s-lab hermes-sarvent: Clara, Karen and Mara on-disk user plugin 1.0.39 with the same init hash. Pod/imageID/owner/readiness/restart/mount receipts are in kube-hermes-sarvent.json. Read-only pod exec used the actual discovered container name, not an assumed one.
- k3s-lab hermes-sandbox: Nyx on-disk plugin 1.0.28, init SHA256 27d4eb9ec27a2001d32c40aefc2e772202623bc7973b8e0e0aaacea42e635624. Receipt: kube-hermes-sandbox.json.
- Vera and Lux: SSH connection timeouts. McCoy: hostname resolution failure. Nebula: strict known-host verification rejected unknown ED25519 key. No host-key bypass, permission change or remote repair occurred.
- Iris systemd and Eve launchd observations show live services. Eve launchd ai.hermes.gateway has a live PID but historical last-exit value 1; that value alone is not proof of current failure. On-disk plugin hashes do not prove imported-object identity.
- Pod managed configuration /etc/hermes/config.yaml selects palaceoftruth; the user YAML did not resolve that selector. Do not misclassify a null partial parser result as a disabled provider.
- Read-only kubectl auth can-i reports permission to patch StatefulSets and ConfigMaps in both namespaces. That is authorization evidence, NOT approval, an apply test, or rollout readiness.
- Flux route receipts identify k3s-lab-apps at ./apps/k3s-lab and hermes-sandbox at ./apps/k3s-lab/hermes-sandbox/manifests, both from flux-control and not suspended, at applied main@sha1:0e9d4231f2fd861164e2108745f5845109bfa541. Do not mutate running pods as a rollout substitute; reconcile GitOps ownership first.
- Established historical rollout/access source: /home/asarver/.hermes/state/palace-continuation/evidence/palace-rollout-discovery-20260918.md. Current JSON observations supersede its version statements, not its missing-host assumptions.

Precedence: official exclusive memory-provider discovery selects bundled before user, then opt-in project, plus package entrypoints where applicable. The observed bundled path is absent for the seven reachable targets and user paths exist. The inventory's selected_on_disk field covers those two checked paths only; project, package, extra profile and imported-object selection remain unproven. Managed/user configuration merging is separate from plugin source selection. No profile or service was changed.

## Smallest decision and safe alternative

Reproduce in an isolated HOME/HERMES_HOME, with official-host dependencies installed:

    python scripts/check_hermes_public_hooks.py --host /path/to/official-host --receipt hooks-receipt.json
    python scripts/check_hermes_public_lifecycle.py --host /path/to/official-host --receipt strict-receipt.json

The hook probe exits 0 when it reproduces observer fail-open behavior; this is diagnostic completion, not parent acceptance. The strict probe exits 2 for either remaining contract failure. CI retains both receipts and does not suppress strict failure.

Recommended: retain the parent contract unchanged and record an explicit upstream-supported admission/barrier requirement. The smallest missing host seam is a documented bounded provider-completion/admission API, with submission identity carried through to providers and timeout/failure surfaced to owning entrypoints. An upstream proposal is a decision request, not an approved host fork.

In parallel, the real stale-quota defect can be repaired inside SAR-1403 by allocating quota against the explicit submitted session rather than the delayed current session. That alone will not fix the host-global barrier. Keep it a separate acceptance criterion.

The immediate safe alternative is to hold rollout and retain the existing documented behavior without claiming full parent acceptance. If a narrower operational boundary is wanted, approve investigation of a new owned external-runner entrypoint using documented public interfaces, with caller-owned drain, failure propagation and readiness checks; exclude other entrypoints only by explicit decision. Integration into AIAgent is still conditional on a supported access boundary and its own tests. That changes the declared production boundary. No such narrowing, disabling, replacement or rollout was performed. Do not silently count the proposed alternative as full parent acceptance.

Until that decision or a supported host API exists: preserve draft PRs and original refs, keep strict checks mandatory, do not merge/deploy/mark Done, and leave live canary/backup/rollback and imported-object provenance gates open.
