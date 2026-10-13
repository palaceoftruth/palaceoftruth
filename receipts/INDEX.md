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

Andrew confirmed that the exposed credential was Barbara's key.
Owner-reported rotation COMPLETE; default Hermes environment update
COMPLETE. No further owner action is requested by this packet.

- Owner (Andrew) reported: the exposed value was Barbara's key, has
  been rotated, and `~/.hermes/.env` was updated. This is a direct
  owner report only.
- This packet did not read `~/.hermes/.env`, did not test the old
  value, did not test the new value, did not call any
  revocation/replay/mint endpoint, and does not make an independent
  claim about the live value, validity or revocation.
- Independent validity or revocation testing was not performed.
  Historical exposure remains in retained Git history.

## Verified predecessor CI

Exact predecessor: e9fe58cc02b087bbb35a5ca14475052ae108fc5c.

- build/validation workflow `38337498997`: success.
  Backend fast 2616 passed, 2 skipped, 1 xfailed; Helm/workflow
  policy 127 passed.
- Dependency vulnerability scan `38337499872`:
  success.
- Backend database, browser, frontend, image build and publication
  lanes were SKIPPED. No DB or publication proof is inferred.

These are predecessor results, not results for the commit containing
this index. Final candidate CI and PR metadata are retained outside
Git under cleanup-residual/ and reported in its SUMMARY.md. This
index does not require a self-referential commit pin.

## Hygiene guard

`backend/scripts/safe_hygiene_guard.py` is an offline path/line/rule
review aid. Findings contain exactly path, line, rule.
It does NOT emit, sample, measure, hash or
fingerprint any assigned value. It is not a comprehensive secret
detector and is not a blocking security gate. Successful scans exit
0; operational failures exit 2 with static errors. Coverage includes
quoted JSON keys and XML/JUnit Config/assertion assignments.
Classification controls eligibility; publication precedence and
explicit .env/.env.example handling apply to the actual CLI scan.
Tests, docs and .github remain excluded unless publication takes
precedence. Name-only findings can be false positives. Coverage is
intentionally limited to known credential-shaped field names; it
will miss custom names and prose-embedded secrets.
