# Receipts — sanitized evidence index for SAR-1402 source release

This index replaces the raw `receipts/*.xml` and `receipts/incident-defects.json`
files that were retained in earlier source trees. The raw bytes are preserved
privately at `/home/asarver/.hermes/cache/scratch/sar1402-completion/receipt-cleanup/private-retained-receipts/`
(directory mode 0700, every file mode 0600) with byte-equivalent SHA-256 hashes.
The raw files disclosed a TypeSafe-shaped credential through inherited
inherited-setting assertion output and are not safe to publish.

## Historical source pins

- **smoke-repair branch e14a4af** introduced `receipts/*.xml` on 2026-10-12.
  These were the very first assistant-managed receipts and contained
  raw failure output that the smoke-repair's first triage redactor
  did not fully sanitize.
- **smoke-repair R1-R5 branch 947c3c4** did not change the receipt contents.
- **smoke-repair R4b-R5b branch c76350d** did not change the receipt contents.
- The current branch retires the raw receipts at the publishable source tip
  but keeps the underlying truth and audit trail privately.

## Original blob SHA-256 (private originals only)

| File | SHA-256 |
| --- | --- |
| receipts/all-green.xml | `b5fa7ea357b7bee97dc4ae4ccdfdef55db15e8f1d225a684e00c3372c5559bc8` |
| receipts/combined-first.xml | `313485c964fe0e6ccb6d9fd9c9a945ba1fbd702f1a5438f82fb16d9df2b79614` |
| receipts/combined-green.xml | `9f71977d3af2b9112942a1eaa0a659ac13bb51b40054d5db8c43a4c7426e4c72` |
| receipts/contract-red.xml | `1f66a09647d24b57be4163372d3b65288650b86fa3f7b7ba4ab37c7315960e78` |
| receipts/control-first.xml | `78ae853f43363b44abe21667a6c6ab841342bcf7002bd32fe4332150097a1bb5` |
| receipts/incident-defects.json | `2b35daf25e57577432e9bcc7788de179301fa6b813fd8fb09cceb7666eb88aba` |

Private originals were not modified. No tip cleanup can erase the earlier
push history; this branch only changes the publishable surface.

## Honest case counts and assertion direction (sanitized)

| File | Cases | Failures | Skips | Direction |
| --- | --- | --- | --- | --- |
| receipts/all-green.xml | 2579 | 12 | 19 | raw failure-run output, not acceptance |
| receipts/combined-first.xml | 97 | 3 | 0 | initial RED vs R1-R5 reference |
| receipts/combined-green.xml | 97 | 0 | 0 | repaired scope (97) |
| receipts/contract-red.xml | 3 | 3 | 0 | initial RED adversarial contract |
| receipts/control-first.xml | 58 | 0 | 0 | smoke_control green |
| receipts/incident-defects.json | n/a | n/a | n/a | prior incident-defects probe summary |

The 12 failures in `receipts/all-green.xml` are worker-side retry races in
the unscrubbed inherited environment. All 12 disputed cases pass on untouched
base under scrubbed settings; the cause of the original worker failures is
not established and is not a defect claim.

## Current exact-head CI (replacement evidence)

- PR564: https://github.com/palaceoftruth/palaceoftruth/pull/564
- build/validation workflow run 38228931363 (Backend fast + Helm/workflow policy + validate)
- Dependency vulnerability scan 38228931277
- exact-head host-only outcome summary captured in `smoke-cleanup/hosted-summary.json`

These replace the raw `receipts/*.xml` for the new public source tip. The
raw receipts remain privately archived for future audit.

## Hygiene guard

`backend/scripts/safe_hygiene_guard.py` is an offline guard that flags new
credential-shaped field assignments in publishable text files. It is field-name
plus assignment-pattern based; it does NOT detect arbitrary secret values
hidden in prose and is not a substitute for owner review.
