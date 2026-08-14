# Browser Session Persistence Design

## Goal

Keep a browser signed in to Palace for a rolling 30 days after one API-key
exchange. The session must survive page reloads and browser restarts. This rule
applies to normal and administration-enabled sessions, so the operator does not
need to enter the API key again during the trusted-browser period.

## Approved Behavior

- Exchange the API key once for the existing server-side browser session.
- Never store the API key in `localStorage`, `sessionStorage`, cookies, or React
  state after the exchange completes.
- Set normal and administration-enabled session lifetimes to 30 days.
- Preserve the existing `HttpOnly`, `Secure`, `SameSite=Strict` session cookie,
  readable CSRF companion cookie, tenant binding, scope checks, revocation, and
  per-tenant session limit.
- Restore the session when Palace starts without delaying the first render when
  no legacy API-key migration is required.
- Refresh the session when seven days or less remain. A successful refresh
  rotates both browser tokens and starts a new 30-day period.
- Check the refresh threshold at startup, at the scheduled threshold, and when
  the page becomes visible or focused after browser suspension.

## Components and Data Flow

The backend configuration will use `2592000` seconds for both
`BROWSER_SESSION_TTL_SECONDS` and `ELEVATED_BROWSER_SESSION_TTL_SECONDS`. The
existing session issue and refresh routes will continue to calculate database
expiry and cookie `Max-Age` from those settings. No schema change is required.

The frontend session bootstrap module will own browser-session maintenance. It
will retain the existing one-time legacy-key migration and add a small lifecycle
controller that:

1. Reads the current session from `GET /api/v1/browser/session`.
2. Schedules a check for the point when seven days remain.
3. Calls `POST /api/v1/browser/session/refresh` only when the threshold is met.
4. Repeats the schedule from the new expiry after a successful rotation.
5. Rechecks after `focus` and visible `visibilitychange` events.

Only one maintenance operation may run at a time in each tab. If another tab
rotates the shared cookie first, a failed refresh will perform one session
readback. A valid readback is treated as success and uses the new shared-cookie
expiry.

## Failure Behavior

- An absent, expired, or revoked session stops maintenance and leaves the UI's
  existing sign-in-required handling in control.
- A temporary network or server failure does not clear cookies or sign the user
  out. Maintenance retries after a bounded delay and on the next focus or
  visibility event.
- Refresh failures do not restore or reuse the API key. Manual sign-in is
  required only after the 30-day session expires without renewal, the user signs
  out, the session is revoked, the source API key is revoked or expires, or the
  server rejects the session.

## Verification

Backend tests will prove that normal and administration-enabled cookies and
server expiries are close to 30 days and that refresh extends the same session
row while rotating both tokens.

Frontend tests will prove that maintenance:

- restores a session after startup without an API key;
- does not refresh when more than seven days remain;
- refreshes when seven days or less remain;
- schedules the next threshold after rotation;
- coalesces concurrent checks;
- recovers from a refresh race by reading the shared session; and
- does not erase a usable session after a temporary failure.

The standard frontend unit tests and build, focused backend browser-session
tests, and a browser-level sign-in/reload scenario will verify the completed
change.

## Non-Goals

- Storing the tenant API key in the browser.
- Adding a second admin credential or separate admin session.
- Changing API-key, OAuth, browser-extension, tenant, or scope authority.
- Merging, deploying, or changing production configuration in this task.
