"""The canon read tier, as agents reach it.

Three surfaces: the `palace_canon` MCP tool, the REST routes it calls, and the
precedence text that tells an agent what to do when canon and derived memory
disagree. Each is tested where it can actually fail.

The caps are not re-tested here. `test_canon.py` already proves the service
enforces them, and the point of routing everything through the service is that
these surfaces do not own a second copy of any number.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app.mcp_server as mcp_server
from app import auth
from app.api import canon as canon_api
from app.database import get_db
from app.services.canon import (
    CanonDigest,
    CanonPageSlice,
    CanonSearchHit,
    CanonDigestPage,
)

PRECEDENCE = "tenant canon outranks derived memory"
SILENCE_RULE = "If canon is silent, say so"


# --- 1. Precedence is stated where an agent will read it -------------------


def test_server_instructions_state_the_precedence() -> None:
    """An agent reads the server instructions before any tool. If the order is
    not there, it will answer from a derived snapshot and sound confident."""
    instructions = mcp_server.mcp.instructions

    assert PRECEDENCE in instructions.lower()
    assert "canon > tenant_shared memory entries > derived room snapshots" in instructions
    assert SILENCE_RULE in instructions
    assert "palace_canon" in instructions


def test_the_canon_tool_description_states_the_precedence() -> None:
    """A tool description is often the only text a model sees for that tool."""
    doc = mcp_server.palace_canon.__doc__ or ""

    assert PRECEDENCE in doc.lower()
    assert SILENCE_RULE in doc
    assert "slug" in doc and "revision" in doc
    assert "candidate_canon_edit" in doc, "the refusal path must be reachable from the tool"


def test_the_canon_tool_is_registered() -> None:
    tools = mcp_server.mcp._tool_manager._tools
    assert "palace_canon" in tools


# --- 2. The tool routes each action and refuses a malformed call -----------


@pytest.fixture
def captured(monkeypatch):
    """Capture the operation and params without touching the network."""
    seen: dict = {}

    async def fake_run(ctx, *, operation, params, call):
        seen["operation"] = operation
        seen["params"] = params
        seen["call"] = call
        return {"ok": True}

    monkeypatch.setattr(mcp_server, "_run_mcp_operation", fake_run)
    return seen


@pytest.mark.asyncio
async def test_digest_is_the_default_action(captured) -> None:
    result = await mcp_server.palace_canon(ctx=object())

    assert result == {"ok": True}
    assert captured["operation"] == "canon_digest"


@pytest.mark.asyncio
async def test_get_routes_to_the_slice_operation(captured) -> None:
    await mcp_server.palace_canon(
        ctx=object(), action="get", slug="company-charter", max_chars=2000, offset=4000
    )

    assert captured["operation"] == "canon_get"
    assert captured["params"]["slug"] == "company-charter"
    assert captured["params"]["max_chars"] == 2000
    assert captured["params"]["offset"] == 4000


@pytest.mark.asyncio
async def test_search_routes_to_the_search_operation(captured) -> None:
    await mcp_server.palace_canon(ctx=object(), action="search", query="onboarding")

    assert captured["operation"] == "canon_search"
    assert captured["params"]["query"] == "onboarding"


@pytest.mark.asyncio
async def test_get_without_a_slug_is_refused(captured) -> None:
    with pytest.raises(ValueError, match="requires a slug"):
        await mcp_server.palace_canon(ctx=object(), action="get")


@pytest.mark.asyncio
async def test_search_without_a_query_is_refused(captured) -> None:
    with pytest.raises(ValueError, match="requires a query"):
        await mcp_server.palace_canon(ctx=object(), action="search")


@pytest.mark.asyncio
async def test_an_unknown_action_is_refused(captured) -> None:
    with pytest.raises(ValueError, match="unknown palace_canon action"):
        await mcp_server.palace_canon(ctx=object(), action="publish", slug="x")


# --- 3. Every canon client call is a read ---------------------------------
#
# The P2 wall is about the API, but a tool that quietly called a write endpoint
# would route around it. These methods state their scope explicitly, so the
# claim is checkable rather than asserted in prose.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, kwargs, expected_path",
    [
        ("get_canon_digest", {}, "/api/v1/canon/digest"),
        ("get_canon_page_slice", {"slug": "a-b", "max_chars": 100, "offset": 0}, "/api/v1/canon/pages/a-b/slice"),
        ("search_canon", {"query": "q", "limit": 3}, "/api/v1/canon/search"),
    ],
)
async def test_canon_client_methods_request_read_scope(monkeypatch, method, kwargs, expected_path) -> None:
    calls: dict = {}

    async def fake_request(self, http_method, path, *, params=None, json_body=None, required_scope, use_caller_identity=True):
        calls.update(http_method=http_method, path=path, scope=required_scope, params=params)
        return {}

    monkeypatch.setattr(mcp_server.SecondBrainApiClient, "_request_json", fake_request)

    client = mcp_server.SecondBrainApiClient.__new__(mcp_server.SecondBrainApiClient)
    await getattr(client, method)(**kwargs)

    assert calls["http_method"] == "GET"
    assert calls["scope"] == "read"
    assert calls["path"] == expected_path


# --- 4. The REST routes the tool calls ------------------------------------


def _app(monkeypatch, *, digest=None, pages=None, hits=None, sliced=None) -> FastAPI:
    app = FastAPI()
    app.include_router(canon_api.router, prefix="/api/v1")

    async def fake_verify_memory_auth(request: Request) -> str:
        auth._attach_auth_context(
            request,
            auth._context_from_scopes(
                tenant_id="tenant-a",
                auth_mode="mcp_oauth",
                subject_id="subject-1",
                token_hash_reference="hash-1",
                scopes=["read", "write"],
                audit_metadata={"api_key_id": "key-1"},
            ),
        )
        return ""

    async def fake_db():
        yield object()

    app.dependency_overrides[auth.verify_memory_auth] = fake_verify_memory_auth
    app.dependency_overrides[get_db] = fake_db

    if digest is not None:
        async def fake_digest(*_a, **_k):
            return digest
        monkeypatch.setattr(canon_api, "get_canon_digest", fake_digest)

    if pages is not None:
        async def fake_pages(*_a, **_k):
            return pages
        monkeypatch.setattr(canon_api, "load_canon_pages", fake_pages)

    if hits is not None:
        monkeypatch.setattr(canon_api, "search_canon_pages", lambda *a, **k: hits)

    if sliced is not None:
        async def fake_slice(*_a, **_k):
            return sliced
        monkeypatch.setattr(canon_api, "read_canon_page_slice", fake_slice)

    return app


def _digest(**overrides) -> CanonDigest:
    values = dict(
        text="identity: we build agentic operations software.",
        char_count=44,
        page_count=3,
        truncated=False,
        next=None,
        fingerprint="fp-1",
    )
    values.update(overrides)
    return CanonDigest(**values)


def test_digest_route_returns_the_text_and_the_spend(monkeypatch) -> None:
    client = TestClient(_app(monkeypatch, digest=_digest()), base_url="https://testserver")

    response = client.get("/api/v1/canon/digest")

    assert response.status_code == 200
    body = response.json()
    assert body["char_count"] == 44
    assert body["page_count"] == 3
    assert body["truncated"] is False


def test_a_truncated_digest_carries_a_cursor(monkeypatch) -> None:
    """A cut digest without a working cursor strands the caller."""
    client = TestClient(
        _app(monkeypatch, digest=_digest(truncated=True, next="canon:digest @page 13")),
        base_url="https://testserver",
    )

    body = client.get("/api/v1/canon/digest").json()

    assert body["truncated"] is True
    assert body["next"] == "canon:digest @page 13"


def test_search_route_returns_snippets_not_bodies(monkeypatch) -> None:
    hits = [
        CanonSearchHit(
            slug="company-charter", section="charter", summary="What we are for.",
            snippet="We build agentic operations software.", score=6.0, truncated=False,
        )
    ]
    client = TestClient(_app(monkeypatch, pages=[], hits=hits), base_url="https://testserver")

    body = client.get("/api/v1/canon/search", params={"q": "agentic"}).json()

    assert body["total"] == 1
    hit = body["hits"][0]
    assert hit["slug"] == "company-charter"
    assert "body" not in hit
    assert hit["snippet"]


def test_search_requires_a_query(monkeypatch) -> None:
    client = TestClient(_app(monkeypatch, pages=[], hits=[]), base_url="https://testserver")

    assert client.get("/api/v1/canon/search").status_code == 422


def test_slice_route_returns_a_window_and_a_cursor(monkeypatch) -> None:
    sliced = CanonPageSlice(
        slug="company-charter", section="charter", summary="What we are for.",
        body="We build agentic operations software.", total_char_count=9000,
        truncated=True, next="canon:company-charter @6000",
    )
    client = TestClient(_app(monkeypatch, sliced=sliced), base_url="https://testserver")

    body = client.get("/api/v1/canon/pages/company-charter/slice", params={"offset": 6000}).json()

    assert body["truncated"] is True
    assert body["next"] == "canon:company-charter @6000"
    assert body["total_char_count"] == 9000


def test_slice_route_passes_the_clamped_cap_to_the_service(monkeypatch) -> None:
    """The route must not invent its own limit; it asks the service's clamp.

    A caller asking for a million characters is clamped to the hard max, and a
    caller asking for nothing gets the default, both decided in one module.
    """
    seen: dict = {}
    sliced = CanonPageSlice(
        slug="p", section="reference", summary="s", body="b",
        total_char_count=1, truncated=False, next=None,
    )

    async def fake_slice(_db, *, tenant_id, slug, max_chars, offset):
        seen.update(tenant_id=tenant_id, slug=slug, max_chars=max_chars, offset=offset)
        return sliced

    app = _app(monkeypatch, sliced=sliced)
    app.dependency_overrides[get_db] = app.dependency_overrides[get_db]
    import app.api.canon as mod
    monkeypatch.setattr(mod, "read_canon_page_slice", fake_slice)
    client = TestClient(app, base_url="https://testserver")

    client.get("/api/v1/canon/pages/p/slice", params={"max_chars": 999999})

    assert seen["max_chars"] == 12000
    assert seen["tenant_id"] == "tenant-a"

    client.get("/api/v1/canon/pages/p/slice")

    assert seen["max_chars"] == 6000


def test_canon_reads_are_open_to_an_agent(monkeypatch) -> None:
    """P2 closed writes. Reads must stay open or the tier is unreachable."""
    client = TestClient(_app(monkeypatch, digest=_digest()), base_url="https://testserver")

    assert client.get("/api/v1/canon/digest").status_code == 200
