# SAR-1402 plugin-owned outbox prototype — narrower alternative contract

Base: approved composite PR555, 56543242d6c1a50dacec0cea5a810544c883f41c.
This is source for independent review, not permission to merge, install, release or roll out.
Hermes core stays unchanged. Historical strict host-global acceptance stays mandatory and blocked.

## Supported-interface decision

The current official memory-provider guide states `sync_turn() MUST be non-blocking` (Threading Contract):
https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin

The synchronous-local-commit intermediate implementation is withdrawn: fsync can block and is not a valid universal bounded callback contract. The final prototype keeps the composite's actual native FIFO, public provider callbacks and ContextVar-aware worker spawning. No host executor, manager, factory, plugin hook or service is replaced or patched. It imports only the official provider interface plus its own plugin modules. The finite external worker talks to ordinary Palace REST endpoints.

## State and evidence contract

1. HOST SUBMITTED: task may still be only in MemoryManager's executor. Provider cannot observe or recover it. No outbox receipt exists.
2. PROVIDER QUEUED/ACTIVE: captured author/context/quota owner is in the existing native FIFO. Callback return and accepted counters are NOT local durable capture. A process crash before local commit can lose it.
3. LOCAL COMMITTED: SQLite synchronous=FULL transaction completed and a separate connection read back the exact canonical payload digest/key. Local receipt says local_durable=true, remote_durable=false, boundary=local_sqlite_FULL_commit_readback. This is the recovery boundary under SQLite/local-filesystem durability assumptions.
4. REMOTE ACCEPTED: POST returns job/item pointers. That is NOT remote durability. Crash or failure here leaves the local record pending; replay uses the SAME immutable payload/idempotency key.
5. REMOTE PROVEN: fresh authorized GET checkpoint-receipt matches durable=true, tenant, literal job/item UUIDs, exact scope, idempotency key and raw-body SHA256. Only then does recovery commit delivered plus its canonical receipt locally and read it back. No indexing/queue accepted status is substituted.

`sync_turn`/ordinary mirror callbacks still return None. `last_outbox_receipt` is only the most recently completed worker's diagnostic receipt, not an acknowledgement of every concurrent caller or a host-admission token. Use the record's exact key/digest to identify evidence. Provider flush covers its native tasks, NOT the external recovery worker. Host flush covers neither unobserved submissions nor all nested provider completion. Empty outbox cannot prove all host work was captured. Recovery results explicitly say host_submission_complete=false.

## Host submission gap: demonstrated, not repaired

Using the unmodified official MemoryManager and a public provider fixture's queue_prefetch hook, tests hold the host executor before sync_all dispatch. Provider flush returns true and the outbox is absent while host work is queued. A child process exits at that point; no record exists to recover. A second probe stalls local outbox commit in the native worker: the callback returns quickly, last_outbox_receipt is absent, provider flush remains false, and no local record yet exists. These are distinct unrecoverable precommit windows. Normal dispatch later creates a local pending record, not a remote durable receipt.

Ordinary callback errors can be caught by the official manager/native worker. This prototype cannot force all owning entrypoints to report those failures. Required precompression v2 remains the separate supported fail-closed contract; no local outbox receipt can satisfy it.

## Persistence, ordering and idempotency

Outbox payloads are normalized JSON data, not executable instructions, closures, pickle or eval. Original author/messages digests, scope, session audit metadata and captured source bodies remain immutable. Tool content remains data. Remote replay never rebuilds payloads or mints retry-time created_at. Duplicate canonical keys reuse the first committed record; a different semantic payload under the same key fails. created_at is record creation time, not invented source-event time; duplicate creation timestamps do not replace stored evidence.

Recovery is finite (max_jobs and a common network deadline <=30 seconds). A POSIX flock prevents overlapping workers; process death releases it. SQLite transactions do not remain open over HTTP. The first unproven predecessor stops the FIFO batch. Recovery is at-least-once delivery, with exactly-once effects conditional on the backend's accepted idempotency contract. Tests demonstrate replay after acceptance-before-ack against an explicit mocked backend, not a production backend. Delivered rows are retained, not silently deleted; retained row cap=1000, payload cap=131072 bytes. Cap exhaustion fails without forgetting receipts or quotas.

The checkpoint remote lane remains unchanged and can complete independently of the external capture-recovery lane. This is NOT a total remote order across checkpoints and deferred automatic captures.

## Quotas and context

Native admission/pins, immutable quota ownership, generation notifications, timed flush/shutdown and checkpoint hard deadlines are preserved. Current-process turn/session caps still apply. Persistent outbox session admission counts committed automatic records (including delivered records) across worker/provider restarts. Recovery does not charge quotas again. Failure attempts conservatively consume the existing in-process budget; no ambiguous commit is refunded. Already committed duplicate records do not charge that provider's quota again.

The persistent ledger is an additional automatic-outbox budget, not a durable global ledger for direct checkpoint or explicit-tool writes. Existing turn buckets remain process-local; the public hook does not supply an authoritative restart-stable turn identifier. Opaque Python ContextVars are used during native preparation but are NOT serialized/restored after process death. Recovery uses only frozen semantic metadata and separately supplied current authorized credentials.

Cron/subagent automatic suppression and cron explicit-tool policy are preserved. Direct explicit tools and required checkpoint transport do not route into the outbox. Prototype scope policy supports exact agent/workspace scope or submitted-session scope; tenant_shared and bulk automatic outbox writes are excluded rather than silently weakened.

## Scope, permissions and secrets

The storage leaf must be directly under the actual profile home supplied by official initialize(hermes_home=...). No hardcoded production home or cross-profile configured path is accepted. Once active, mode/binding/home cannot silently change. Directory must be owner UID mode0700; DB/journal/worker lock must be regular, owner UID mode0600 and single-link. Symlink paths and insecure existing modes are rejected, not repaired. Same UID and root remain trusted: permissions are not encryption or protection from the owner.

Binding is immutable non-secret expected intent: exact HTTPS origin, tenant UUID, principal (whoami agent_scope_key), and explicit scope policy. It is NOT authenticated authorization at capture time. Every recovery batch first reads whoami using fresh externally supplied credentials and matches tenant/principal. Exact stored scope is revalidated before replay; backend POST/receipt authorization remains authoritative. Redirects (including HTTPS downgrade and cross-origin forwarding) are forbidden. TLS verification and bounded response size use the unchanged approved checkpoint child transport.

Provider auth headers/client secrets are not persisted. Worker reads PALACE_OUTBOX_API_KEY from its own supplied environment, never profile dotenv, command-line credential args or the outbox. Only canonical receipt fields are persisted; extra server data/credential-shaped fields are not retained. Captured user content may itself be sensitive: there is NO promise of universal secret redaction or encrypted-at-rest storage. File permissions and data-minimization remain required.

## Time and crash limits

SQLite busy_timeout bounds lock admission (~100ms), not a hung filesystem, kernel, directory fsync or disk full failure. These I/O operations run in the native worker, not the public nonblocking callback. Checkpoint callers retain their existing total deadline, and accepted worker accounting/pins settle when work can complete. Local FULL commit/readback is not evidence of power-loss behavior on every volume. Tests exercise abrupt process exit before commit, after commit, and after mocked remote acceptance, plus real loopback TLS worker execution. No physical power-loss or real deployment durability is claimed.

## Source-only configuration and finite worker

No profile is changed by this candidate. Independent tests use isolated profiles and fixture bindings. Prototype flags live in the plugin's own palaceoftruth.json inputs: outbox_prototype_enabled=true, outbox_directory (one profile-local leaf), and outbox_binding. Default is disabled. Review the scope/principal/origin before any future activation; do not put credentials into that binding.

Finite source command (not installed or scheduled):
    python scripts/recover_hermes_outbox.py --directory <isolated-outbox> --binding-file <non-secret-binding.json> --max-jobs 10 --timeout 10

No service unit, cron, gateway factory replacement or live worker is installed. Existing plugin/package version is not bumped into a release. API-key recovery is the only implemented external credential mode; OAuth-vault recovery is not claimed.

## Acceptance and exclusions

Alternative acceptance: an exactly identified locally committed normalized record survives process death and can be replayed with immutable identity/quota evidence; only exact authorized canonical readback marks remote delivery. It does not assert capture of every host submission, universal host failure propagation, host-global completion, cross-lane ordering, complete restart-stable turn quotas, deployed Postgres canary or fleet readiness.

Historical strict script/workflow/tests remain unchanged. Source tests for this narrower contract are additional tests, not xfail/skip replacements for that historical gate. Full SAR-1402 acceptance and rollout remain BLOCKED pending an explicitly accepted host boundary and independent review. No core edit, fork, monkeypatch, service control, production change, merge, release, rollout or live canary.
