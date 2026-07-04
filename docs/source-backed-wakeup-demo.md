# Source-Backed Wakeup for Agent Teams

This runbook smokes the first Palace product proof: a fresh agent sees which
startup context is source-backed, generated but unpromoted, stale or missing, and
policy-limited before choosing a safe next action.

The demo uses only sanitized fixture data in
`fixtures/source_backed_wakeup_demo.json`. It does not connect to Palace, read
secrets, use production content, or mutate a database.

## Run The Smoke Demo

From a clean checkout:

```bash
python3 scripts/demo_source_backed_wakeup.py
```

The expected output is three operator-facing blocks:

1. `Context Palace selected`
2. `Trust warnings Palace found`
3. `Safe next action`

The script also prints a fixture scan that confirms the required public trust
states and sanitized `.test` source URLs.

## Fixture States

The fixture covers:

- `source_backed`: a trusted release runbook source with active source chunks.
- `generated_unpromoted`: a generated synthesis that must be verified before use.
- `stale_source`: an old rollback note that should not drive action.
- `policy_limited`: a scoped private finding that requires an authorized summary.

The privacy scan fails if the fixture includes common secret markers, raw
production-content markers, or non-`.test` source URLs.

## Local Tenant Story

The fixture includes a local demo tenant id,
`demo-tenant-source-backed-wakeup`, so the story can be explained without
requiring a live tenant. For a live local database demo, seed equivalent
sanitized records into a disposable tenant and compare the `get_wakeup_context`
response shape against the same four public trust states.
