# Minimal upstream admission/completion API proposal

Status: proposal only. No upstream issue opened. No implementation, core edit, fork, monkeypatch, synchronous sync_turn replacement or production wiring is claimed.

The required semantics are the exact minimum missing from the inspected host contract:

1. At host submission, capture an immutable operation identity (session ID, supported principal/profile/scope identity and turn ID). Carry it unchanged to provider callbacks. A provider must be able to allocate ownership at this boundary, before deferred dispatch or a session switch. Preserve existing nonblocking callbacks and documented compatibility.
2. Expose supported completion tracking for provider-owned asynchronous work. A provider returns/registers a completion handle for each admitted write, distinguishing rejected, accepted, completed, failed and timed-out work. The handle cannot be a queued remote-ingestion acknowledgement misrepresented as durable ingestion.
3. A public bounded host barrier waits for both prior host dispatch and registered provider work. A timeout/failure is visible in its result; it must not return success while registered work is pending. Closing admission is atomic with shutdown. Timeout does not silently cancel or discard accepted work.
4. Owning CLI, channel gateway, cron, HTTP/API, TUI/desktop, ACP and embedded callers consume that result before session/identity transition or successful final readiness/shutdown acknowledgement. Document exact ordering and error propagation. Observer hooks alone are not completion authority.

Proposed API names/signatures are intentionally NOT invented. Upstream must choose the supported interface and compatibility rules. Requirements are normative; symbol design is not assumed.

Acceptance: real official-host tests for delayed submission followed by resume/branch/rewind, immutable attribution and quota ownership, nested provider work blocked at host flush, failure/timeout, concurrent admission and shutdown, all supported production entrypoints, and compatibility with existing providers. No skipped/xfail strict gate. Remote ingestion durability requires a separate job-status/readback contract.

The Palace session-quota repair is independent of this proposal. It makes explicit submitted-session accounting correct inside the provider. It does not make the official executor sentinel a provider-completion barrier. Hold rollout until the remaining contract is supported and proved.
