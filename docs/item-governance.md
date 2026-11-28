# Item governance

Palace keeps a governance surface on every item, and a matching subset on
derived claims. Important knowledge keeps its accountable owner, reviewer,
verification state, expiry deadline, risk class, and supersession chain with
the row itself, so a shared knowledge base can show which entries are current
and which are not.

All governance fields are optional. Existing items stay `unassigned` until an
operator writes them.

## PATCH payload

```http
PATCH /api/v1/items/{id}
```

```json
{
  "governance": {
    "owner_subject": "apikey:***",
    "reviewer_subject": "human:alice",
    "verification_state": "verified",
    "verified_at": "2026-08-29T12:00:00Z",
    "verified_by_subject": "human:alice",
    "verification_deadline": "2026-11-29T00:00:00Z",
    "risk_class": "high",
    "supersession_reason": "Replaced by the Q4 handbook revision",
    "superseded_by_item_id": "01HXYZ...",
    "superseded_at": "2026-08-29T12:00:00Z"
  }
}
```

`owner_subject`, `reviewer_subject`, and `verified_by_subject` are bounded to
1 to 200 characters. Use a stable identifier, such as an API key id, an MCP
client id, or a human handle. Do not use free-form prose.
`supersession_reason` is bounded to 1 to 1000 characters. A validation failure
returns `422`.

Closed enums, mirrored by PostgreSQL check constraints so the schema and the
Pydantic model cannot drift:

- `verification_state`: `unverified`, `verified`, `stale`, `rejected`
- `risk_class`: `low`, `moderate`, `high`, `critical`

## Currentness and search ranking

Every search hit carries `governance_currentness_state`, derived from the stored
columns:

| State | Trigger | Effect on ranking |
| --- | --- | --- |
| `unassigned` | No owner or verification recorded | No adjustment, the safe default for untriaged items |
| `current` | `verification_state` is `verified` or `stale`, and the deadline has not passed | No adjustment |
| `expired` | `verification_deadline` has passed, and the item is not superseded | `-0.35` penalty when `risk_class` is `high` or `critical`, otherwise no adjustment |
| `superseded` | `superseded_by_item_id` is set, or `verification_state` is `rejected` | Excluded from `current` mode, present in `historical` mode |

Expired items stay visible in current mode, ranked down, so the result carries
the explicit warning. Superseded items appear only in historical mode through
`currentness_mode = historical`. The search ranking trace reports
`governance_state_counts` and `excluded_governance_counts`, so an operator can
see the slice.

## Audit trail and structured logs

Each transition appends one entry to `metadata.governance_audit`. The list holds
the 25 most recent entries (`MAX_AUDIT_ENTRIES`). Older entries drop off in FIFO
order. Every transition also emits one structured log line:

- `governance.item.update` at INFO, with
  `extra={"item_id", "tenant_id", "actor_subject", "changed_fields", "verification_state", "risk_class", "verification_deadline"}`
- `governance.item.denied` at WARNING when a cross-tenant or scope-limited write
  is refused, with
  `extra={"tenant_id", "item_id", "actor_subject", "reason"}`

The audit trail, the structured log line, and the API wire format share one
field map. An operator can replay a log line against an audit entry without an
ORM round trip.
