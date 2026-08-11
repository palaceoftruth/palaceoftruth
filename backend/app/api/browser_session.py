"""Sign-in, refresh and sign-out for the SPA's cookie session (H-20).

These four routes are the only place a tenant API key crosses the wire from the
browser, and it is never stored there. Everything else the SPA calls
authenticates from the ``palace_session`` cookie via ``verify_api_key``.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.browser_session import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    attach_session_cookies,
    clear_session_cookies,
    issue_session,
    load_session,
    revoke_session,
    rotate_session,
    verify_session_csrf,
)
from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/browser", tags=["browser-session"])


class BrowserSessionRequest(BaseModel):
    api_key: str = Field(min_length=8, max_length=512)
    # Off by default: an ordinary browsing session must not be able to register
    # MCP clients or mint extension pairing keys.
    elevated: bool = False


class BrowserSessionResponse(BaseModel):
    tenant_id: str
    scopes: list[str]
    expires_at: datetime


async def _require_session(request: Request, db: AsyncSession):
    """Load the caller's session from its cookie, or raise 401.

    Deliberately not reusing ``verify_api_key``: these routes must answer "you
    are not signed in" distinctly from "your session lacks a scope", and the
    sign-out path has to work even for a session that is already unusable.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="No browser session")
    session = await load_session(db, token)
    if session is None:
        raise HTTPException(status_code=401, detail="Browser session is invalid or expired")
    return session


async def _require_csrf(request: Request, db: AsyncSession, session) -> None:
    presented = request.headers.get(CSRF_HEADER_NAME)
    if not await verify_session_csrf(db, session_id=session.id, presented_token=presented):
        raise HTTPException(status_code=403, detail="Missing or invalid CSRF token")


@router.post("/session", response_model=BrowserSessionResponse, status_code=201)
async def create_browser_session(
    body: BrowserSessionRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> BrowserSessionResponse:
    """Exchange a tenant API key for a session cookie pair."""
    issued = await issue_session(db, api_key=body.api_key, elevated=body.elevated)
    if issued is None:
        # One message for "unknown key", "revoked key" and "key holds no usable
        # scopes" alike, so the response cannot be used to classify a key.
        raise HTTPException(status_code=401, detail="Invalid API key")
    await db.commit()
    attach_session_cookies(response, request, issued)
    logger.info(
        "browser session issued",
        extra={"tenant_id": issued.tenant_id, "elevated": body.elevated},
    )
    return BrowserSessionResponse(
        tenant_id=issued.tenant_id,
        scopes=list(issued.scopes),
        expires_at=issued.expires_at,
    )


@router.get("/session", response_model=BrowserSessionResponse)
async def read_browser_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BrowserSessionResponse:
    """Report the live session, so the SPA can restore state on a reload."""
    session = await _require_session(request, db)
    return BrowserSessionResponse(
        tenant_id=session.tenant_id,
        scopes=list(session.scopes),
        expires_at=session.expires_at,
    )


@router.post("/session/refresh", response_model=BrowserSessionResponse)
async def refresh_browser_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> BrowserSessionResponse:
    """Rotate the session and CSRF tokens and extend the expiry."""
    session = await _require_session(request, db)
    await _require_csrf(request, db, session)
    issued = await rotate_session(db, session)
    await db.commit()
    attach_session_cookies(response, request, issued)
    return BrowserSessionResponse(
        tenant_id=issued.tenant_id,
        scopes=list(issued.scopes),
        expires_at=issued.expires_at,
    )


@router.delete("/session", status_code=204)
async def delete_browser_session(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Sign out: revoke the row server-side and clear both cookies."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    session = await load_session(db, token) if token else None
    if session is not None:
        await _require_csrf(request, db, session)
        await revoke_session(db, session.id)
        await db.commit()
    # Clear the cookies either way. Signing out of a session that has already
    # expired must still leave the browser clean, and must not report an error.
    clear_session_cookies(response, request)
    response.status_code = 204
    return response
