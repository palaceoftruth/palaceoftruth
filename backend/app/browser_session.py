"""Short-lived SPA sessions that keep the tenant API key out of the browser (H-20).

The SPA used to hold a tenant-wide API key in ``localStorage`` and attach it to
every request. Any script on the origin could read it in one line and keep it
forever. Here the key is presented once, exchanged server-side, and replaced by:

* ``palace_session`` - the session token. ``HttpOnly``, so no script can read
  it; ``Secure``; ``SameSite=Strict``; short TTL; revocable.
* ``palace_session_csrf`` - a companion token that IS readable by same-origin
  script, and must be echoed in ``X-Palace-CSRF`` on every unsafe request.
  Cookie auth is otherwise ambient, so this is the double-submit half.

Only hashes are stored, using the same peppered verifier as every other Palace
credential. A stolen database row does not yield a usable session.
"""

import json
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from fastapi import Request, Response
from sqlalchemy import text

# One-way dependency: app.auth reaches back into this module through a
# function-level import, so nothing here may be imported by auth at module
# scope.
from app.auth import (
    _parse_stored_api_key_scopes,
    compare_secret,
    hash_secret,
    secret_hash_candidates,
)
from app.config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "palace_session"
CSRF_COOKIE_NAME = "palace_session_csrf"
CSRF_HEADER_NAME = "X-Palace-CSRF"

# Requests that cannot change state do not need the CSRF echo.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# `admin` is deliberately absent. A browsing session must not be able to
# register MCP clients or mint extension pairing keys unless the operator asked
# for that explicitly at sign-in; see `resolve_session_scopes`.
DEFAULT_SESSION_SCOPES: tuple[str, ...] = (
    "read",
    "write",
    "write:agent",
    "write:workspace",
    "write:session",
    "capture:write",
    "capture:job:read",
)

ELEVATED_SESSION_SCOPES: tuple[str, ...] = (*DEFAULT_SESSION_SCOPES, "admin")


@dataclass(frozen=True)
class IssuedBrowserSession:
    session_token: str
    csrf_token: str
    tenant_id: str
    scopes: tuple[str, ...]
    expires_at: datetime


@dataclass(frozen=True)
class BrowserSessionRecord:
    id: Any
    tenant_id: str
    api_key_id: Any
    scopes: tuple[str, ...]
    expires_at: datetime
    session_token_hash: str


def session_ttl(*, elevated: bool = False) -> timedelta:
    seconds = settings.elevated_browser_session_ttl_seconds if elevated else settings.browser_session_ttl_seconds
    return timedelta(seconds=seconds)


def resolve_session_scopes(key_scopes: Iterable[str], *, elevated: bool) -> tuple[str, ...]:
    """Intersect the requested session grant with what the key actually holds.

    A session can only ever be narrower than its key. Asking for elevation with
    a key that has no ``admin`` grant yields a normal session, not an error, so
    the caller cannot use this endpoint to probe a key's scopes.
    """
    requested = ELEVATED_SESSION_SCOPES if elevated else DEFAULT_SESSION_SCOPES
    held = set(key_scopes)
    # An `admin` key implies every capability elsewhere in the codebase
    # (AuthContext.has_capability), so honour that implication here too.
    if "admin" in held:
        return tuple(requested)
    return tuple(scope for scope in requested if scope in held)


def _cookie_is_secure(request: Request) -> bool:
    """Whether to set ``Secure``.

    Deliberately not derived from the request host or scheme: both are
    proxy-supplied and an attacker who can influence them could strip the
    attribute. Browsers treat http://localhost as a secure context and accept
    ``Secure`` cookies there, so local development needs no exception - the
    setting exists only for a deployment that knowingly runs without TLS.
    """
    del request  # kept in the signature so callers read the same at both sites
    return settings.browser_session_cookie_secure


def attach_session_cookies(response: Response, request: Request, issued: IssuedBrowserSession) -> None:
    max_age = max(int((issued.expires_at - datetime.now(timezone.utc)).total_seconds()), 0)
    secure = _cookie_is_secure(request)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.session_token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="strict",
        path="/",
    )
    # httponly=False is the point: the SPA reads this one to echo it back.
    response.set_cookie(
        CSRF_COOKIE_NAME,
        issued.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="strict",
        path="/",
    )


def clear_session_cookies(response: Response, request: Request) -> None:
    secure = _cookie_is_secure(request)
    for name, http_only in ((SESSION_COOKIE_NAME, True), (CSRF_COOKIE_NAME, False)):
        response.set_cookie(
            name,
            "",
            max_age=0,
            httponly=http_only,
            secure=secure,
            samesite="strict",
            path="/",
        )


async def issue_session(
    db,
    *,
    api_key: str,
    elevated: bool,
) -> IssuedBrowserSession | None:
    """Exchange a raw API key for a session. Returns None if the key is invalid."""
    key_hash, legacy_key_hash = secret_hash_candidates(api_key)
    row = await db.execute(
        text(
            "SELECT id, tenant_id, scopes FROM api_keys "
            "WHERE key_hash IN (:hash, :legacy_hash) AND revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP) LIMIT 1"
        ),
        {"hash": key_hash, "legacy_hash": legacy_key_hash},
    )
    key = row.mappings().one_or_none()
    if key is None:
        return None

    stored_scopes = _parse_stored_api_key_scopes(key.get("scopes"))
    if not stored_scopes:
        return None
    scopes = resolve_session_scopes(stored_scopes, elevated=elevated)
    if not scopes:
        return None

    # Serialize this tenant's prune+insert pair so concurrent exchanges cannot
    # exceed the configured live-session cap.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant_id, 1324))"),
        {"tenant_id": key["tenant_id"]},
    )

    # Bound retained authority before creating another session. Revoke the
    # oldest live rows so concurrent browsers cannot grow without limit.
    await db.execute(
        text(
            """
            WITH excess AS (
                SELECT id FROM browser_sessions
                WHERE tenant_id = :tenant_id AND revoked_at IS NULL
                  AND expires_at > CURRENT_TIMESTAMP
                ORDER BY created_at DESC
                OFFSET :keep_count
            )
            UPDATE browser_sessions
            SET revoked_at = CURRENT_TIMESTAMP
            WHERE id IN (SELECT id FROM excess) AND revoked_at IS NULL
            """
        ),
        {
            "tenant_id": key["tenant_id"],
            "keep_count": max(settings.browser_session_max_per_tenant - 1, 0),
        },
    )

    session_token = f"palses_{secrets.token_urlsafe(40)}"
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + session_ttl(elevated=elevated)
    await db.execute(
        text(
            "INSERT INTO browser_sessions "
            "(tenant_id, api_key_id, session_token_hash, csrf_token_hash, scopes, expires_at) "
            "VALUES (:tenant_id, :api_key_id, :session_token_hash, :csrf_token_hash, "
            "CAST(:scopes AS jsonb), :expires_at)"
        ),
        {
            "tenant_id": key["tenant_id"],
            "api_key_id": key["id"],
            "session_token_hash": hash_secret(session_token),
            "csrf_token_hash": hash_secret(csrf_token),
            "scopes": json.dumps(list(scopes)),
            "expires_at": expires_at,
        },
    )
    return IssuedBrowserSession(
        session_token=session_token,
        csrf_token=csrf_token,
        tenant_id=key["tenant_id"],
        scopes=scopes,
        expires_at=expires_at,
    )


async def rotate_session(db, session: "BrowserSessionRecord") -> IssuedBrowserSession:
    """Replace a live session's tokens in place and extend its expiry.

    Rotating on refresh means a session token that did leak has a bounded useful
    life even while the tab stays open. The row keeps its identity, so revoking
    the originating API key still cascades.
    """
    session_token = f"palses_{secrets.token_urlsafe(40)}"
    csrf_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + session_ttl(elevated="admin" in session.scopes)
    await db.execute(
        text(
            "UPDATE browser_sessions SET session_token_hash = :session_token_hash, "
            "csrf_token_hash = :csrf_token_hash, expires_at = :expires_at, "
            "last_used_at = CURRENT_TIMESTAMP "
            "WHERE id = :id AND revoked_at IS NULL"
        ),
        {
            "session_token_hash": hash_secret(session_token),
            "csrf_token_hash": hash_secret(csrf_token),
            "expires_at": expires_at,
            "id": session.id,
        },
    )
    return IssuedBrowserSession(
        session_token=session_token,
        csrf_token=csrf_token,
        tenant_id=session.tenant_id,
        scopes=session.scopes,
        expires_at=expires_at,
    )


async def load_session(db, session_token: str) -> BrowserSessionRecord | None:
    """Look up a live session by its raw token, or None if it cannot be used."""
    if not session_token:
        return None
    token_hash = hash_secret(session_token)
    row = await db.execute(
        text(
            "SELECT id, tenant_id, api_key_id, scopes, expires_at, session_token_hash "
            "FROM browser_sessions "
            "WHERE session_token_hash = :hash AND revoked_at IS NULL "
            "  AND expires_at > CURRENT_TIMESTAMP "
            # A revoked key must not keep working through an outstanding session.
            "  AND EXISTS (SELECT 1 FROM api_keys k "
            "              WHERE k.id = browser_sessions.api_key_id AND k.revoked_at IS NULL "
            "                AND (k.expires_at IS NULL OR k.expires_at > CURRENT_TIMESTAMP)) "
            "LIMIT 1"
        ),
        {"hash": token_hash},
    )
    record = row.mappings().one_or_none()
    if record is None:
        return None

    return BrowserSessionRecord(
        id=record["id"],
        tenant_id=record["tenant_id"],
        api_key_id=record["api_key_id"],
        scopes=_parse_stored_api_key_scopes(record.get("scopes")),
        expires_at=record["expires_at"],
        session_token_hash=record["session_token_hash"],
    )


async def verify_session_csrf(db, *, session_id, presented_token: str | None) -> bool:
    """Confirm the header token matches the session's stored CSRF verifier."""
    if not presented_token:
        return False
    row = await db.execute(
        text("SELECT csrf_token_hash FROM browser_sessions WHERE id = :id LIMIT 1"),
        {"id": session_id},
    )
    record = row.mappings().one_or_none()
    if record is None:
        return False
    return compare_secret(presented_token, record["csrf_token_hash"])


async def touch_session(db, session_id) -> None:
    await db.execute(
        text("UPDATE browser_sessions SET last_used_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"id": session_id},
    )


async def revoke_session(db, session_id) -> None:
    await db.execute(
        text(
            "UPDATE browser_sessions SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE id = :id AND revoked_at IS NULL"
        ),
        {"id": session_id},
    )
