# SAR-1402 combined source candidate — not release approval

Exact source parents:
- checkpoint PR554 f653b9a99849e35b588dd36fc480aee3df52b0a8
- provenance PR553 61cf1621e33a73874a94ddff4a52c1ce93bb1f7e
- common lifecycle ancestor 545dd75a584523ed8247f9a2c88c6cfab155cd40

Fresh isolated clone; both complete histories preserved by local source merge. No original branches rewritten. Constructor conflict keeps both `_init_checkpoint()` and `_turn_author`. Provenance atomic snapshot/immutable metadata and serialized audits stay intact; checkpoint generation notifications, hard caller deadlines and timed shutdown remain intact. Session quota tests distinguish fact writes from audit writes and also assert audit charging to the previous session. Host provenance test requires standard sys.modules registration before package execution; no assertion changes.

Offline evidence: full backend-fast 2480 passed, 2 skipped, 1 xfailed; normal official-loader combined selection 246 passed; unchanged independent reviewer scripts with provenance baseline 52 passed; older official-host selection 23 passed. Both strict real-host probes remain unmodified and return exit 2, host_global_drain unsupported. Current official host 1f2b9e08644c6de4a5b5c50d0e4676a19c5577c5; earlier v2 pin c57331677d7fc46298a7159cf3a650853f0966c5. Actual official CI matrix retains its own b65 pin and main and records those source revisions.

Draft PR against main is for honest exact-head CI. Strict failure is a mandatory BLOCKER, not an accepted success, expected-failure waiver or reason to weaken the gate. Hosted results must be collected separately at final head. Baseline workflow selection: 17 passed both candidate and origin/main. No unrelated baseline failure observed in that selection. No release workflow dispatch.

Source composition requires independent composite review. Provider drain is not host-global drain. Queue acceptance is not durability. Mocked receipt acceptance does not prove deployed Postgres durability. No core edit, live canary, merge, release, deployment or Done.

Local detailed receipts, annotated parent diffs, unchanged reviewer copies, progress and SUMMARY are under sar1402-completion/composite. Final publication SHA must be read from that SUMMARY or GitHub, not inferred from historical branch docs.
