# Receipts — sanitized evidence index for SAR-1402 source release

This index replaces the raw `receipts/*.xml` and `receipts/incident-defects.json`
files that were retained in earlier source trees. The raw bytes are preserved
privately at
`/home/asarver/.hermes/cache/scratch/sar1402-completion/receipt-cleanup/private-retained-receipts/`
(directory mode **0700**, every file mode **0600**) with byte-equivalent SHA-256
hashes that match the current private copies. The raw files disclosed a
TypeSafe-shaped credential through inherited assertion output and are not
safe to publish at the source tip.

## Historical source pins

- `e14a4af8330bc19d033ff9314160aba8fb08818f` introduced `receipts/*.xml` on
  2026-10-12 as the very first assistant-managed receipts. Their failure
  output contained inherited assertion output whose first triage
  redactor did not fully sanitize. This is the historical
  introduction of the disclosure surface.
- `947c3c46a974f8f9e97a5553be18b9094992dfea` and
  `c76350d9bc8fddd41c9077608e0c1204ef7ad166` did not change the
  receipt contents; they only evolved the rest of the source.
- `a6aaa88bb42f24e99cd34484cb814312293f8607` retired the raw receipts
  at the publishable source tip while keeping the underlying truth
  and audit trail privately.
- The current branch narrows the guard and updates this index. It
  does not touch the historical files or rewrite history.

## Original blob SHA-256 (private originals only)

| File | SHA-256 |
| --- | --- |
| receipts/all-green.xml | `b5fa7ea357b7bee97dc4ae4ccdfdef55db15e8f1d225a684e00c3372c5559bc8` |
| receipts/combined-first.xml | `313485c964fe0e6ccb6d9fd9c9a945ba1fbd702f1a5438f82fb16d9df2b79614` |
| receipts/combined-green.xml | `9f71977d3af2b9112942a1eaa0a659ac13bb51b40054d5db8c43a4c7426e4c72` |
| receipts/contract-red.xml | `1f66a09647d24b57be4163372d3b65288650b86fa3f7b7ba4ab37c7315960e78` |
| receipts/control-first.xml | `78ae853f43363b44abe21667a6c6ab841342bcf7002bd32fe4332150097a1bb5` |
| receipts/incident-defects.json | `2b35daf25e57577432e9bcc7788de179301fa6b813fd8fb09cceb7666eb88aba` |

The hashes above are identity pins for the historical artifacts, not
credential fingerprints. Private originals were not modified; their
bytes match the pins. No tip cleanup can erase the earlier push
history; this branch only changes the publishable surface.

## Honest case counts and assertion direction (sanitized)

| File | Cases | Passes | Failures | Skips | xfail | Direction |
| --- | --- | --- | --- | --- | --- | --- |
| receipts/all-green.xml | 2579 | 2548 | 12 | 18 | 1 | raw failure-run output, not acceptance |
| receipts/combined-first.xml | 97 | 94 | 3 | 0 | 0 | initial RED vs R1-R5 reference |
| receipts/combined-green.xml | 97 | 97 | 0 | 0 | 0 | repaired scope (97) |
| receipts/contract-red.xml | 3 | 0 | 3 | 0 | 0 | initial RED adversarial contract |
| receipts/control-first.xml | 58 | 58 | 0 | 0 | 0 | smoke_control green |
| receipts/incident-defects.json | n/a | n/a | n/a | n/a | n/a | prior incident-defects probe summary |

The 12 failures in `receipts/all-green.xml` were inherited from worker
runs in the unscrubbed inherited environment. All 12 disputed cases
pass on untouched base under scrubbed settings; the cause of the
original worker failures is not established. This is not an inherent
baseline-code defect claim; it is a documentation of what
uncontrolled environment produced.

## Owner-reported disposition

The disclosed value was a `TYPESAFE_API_KEY`-shaped 107-byte string
in the JUnit dump of
`tests/test_config.py::test_typesafe_settings_are_inert_by_default`.
It is NOT the live key; the live value lives only in the
`TYPESAFE_API_KEY` env var, read by
`backend/app/services/typesafe.py` with `JevConfig.api_key`
`repr=False`. The Jev path is currently disabled in default config.

- Owner (Andrew) reported: the exposed value was Barbara's key, has
  been rotated, and `~/.hermes/.env` was updated. This is a direct
  owner report only.
- This packet did not read `~/.hermes/.env`, did not test the old
  value, did not test the new value, did not call any
  revocation/replay/mint endpoint, and does not make an independent
  claim about the live value, validity or revocation.
- Independent test, validity or revocation status: NOT tested. Treat
  the historical exposure as real until the owner validates the
  rotation through a separately owner-led packet.

## Current exact-head CI for the cleanup parent (a6aaa88)

Historical pointers (NOT current branch; presented for context only).

- build/validation workflow `38228931363` at `c76350d9...` (parent
  of cleanup): success. Backend fast 2568/2/1, Helm/workflow
  policy 127 passed.
- Dependency vulnerability scan `38228931277` at `c76350d9...`:
  success.
- Backend database, browser, frontend, image build and publication
  lanes were SKIPPED. No DB or publication proof is inferred.

## Current exact-head CI for this branch (read this one)

- build/validation workflow run to be confirmed by the next
  push of this branch. The exact-head hosted CI is verified
  separately and retained in
  `/home/asarver/.hermes/cache/scratch/sar1402-completion/cleanup-repair/hosted-run.json`
  and the matching `hosted.json` / `hosted.log`. Database, image
  build and publication lanes are SKIPPED (unchanged from prior
  runs); no DB or publication proof is inferred.

## Hygiene guard

`backend/scripts/safe_hygiene_guard.py` is an offline path/line/rule
review aid. It emits only path, line, field name, static rule id
and path category. It does NOT inspect, sample, length, hash or
fingerprint any assigned value. It is not a comprehensive secret
detector and is not a blocking security gate. Exit code is always
0; reviewers read the JSON rule-id list. The guard's coverage is
intentionally limited to known credential-shaped field names; it
will miss custom names and prose-embedded secrets.
