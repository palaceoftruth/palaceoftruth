"""The canon write wall. This test is the enforcement, not the docs.

Two independent proofs, because either one alone is a false green:

1. **Coverage.** Every mutating route on the canon router carries the
   human-author gate. A handler added later without it fails here, which is the
   regression that actually happens.
2. **Behaviour.** Each non-human principal is refused with
   ``403 canon_human_author_only``, whatever grants it holds, and a human browser
   session is not refused.

A behaviour test on one endpoint would pass while a second endpoint stayed open,
and a coverage test alone would pass while the gate itself refused nobody.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import ast
import pathlib

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth
from app.api import canon as canon_api
from app.database import get_db
from app.models.canon import CanonPage
from app.models.item import Item
from app.services.canon_authoring import CanonPageWrite

WRITE_ROUTES = (
    ("POST", "/api/v1/canon/pages"),
    ("PATCH", "/api/v1/canon/pages/company-charter"),
    ("POST", "/api/v1/canon/pages/company-charter/publish"),
    ("POST", "/api/v1/canon/pages/company-charter/ratify"),
    ("POST", "/api/v1/canon/pages/company-charter/archive"),
)

CREATE_BODY = {
    "slug": "company-charter",
    "title": "Company charter",
    "summary": "What this company is for.",
    "section": "charter",
    "body": "We build agentic operations software.",
}


def _app(*, auth_mode: str, scopes: list[str] | None = None) -> FastAPI:
    """A real app carrying the real router and the real dependency chain."""
    app = FastAPI()
    app.include_router(canon_api.router, prefix="/api/v1")
    # The write handlers read the shared services off app.state, as the real app
    # sets them at startup. The service calls are stubbed, so any object will do.
    app.state.embedder = object()
    app.state.llm = object()

    async def fake_verify_memory_auth(request: Request) -> str:
        auth._attach_auth_context(
            request,
            auth._context_from_scopes(
                tenant_id="tenant-a",
                auth_mode=auth_mode,
                subject_id="subject-1",
                token_hash_reference="hash-1",
                scopes=["read", "write"] if scopes is None else scopes,
                audit_metadata={"api_key_id": "key-1"},
            ),
        )
        return ""

    async def fake_db():
        yield object()

    app.dependency_overrides[auth.verify_memory_auth] = fake_verify_memory_auth
    app.dependency_overrides[get_db] = fake_db
    return app


def _client(*, auth_mode: str, scopes: list[str] | None = None) -> TestClient:
    return TestClient(_app(auth_mode=auth_mode, scopes=scopes), base_url="https://testserver")


def _call(client: TestClient, method: str, path: str):
    if method == "POST" and path.endswith("/canon/pages"):
        return client.post(path, json=CREATE_BODY)
    if method == "PATCH":
        return client.patch(path, json={"title": "Renamed"})
    return client.post(path)


# --- 1. Coverage: every mutating canon route carries the gate --------------


def _gate_carried(route) -> bool:
    for dependency in route.dependant.dependencies:
        call = dependency.call
        if getattr(call, "canon_human_author_gate", False):
            return True
    return False


def test_every_mutating_canon_route_carries_the_human_author_gate() -> None:
    routes = [route for route in canon_api.router.routes if getattr(route, "methods", None)]
    mutating = [
        route
        for route in routes
        if route.methods & {"POST", "PUT", "PATCH", "DELETE"}
    ]

    # Guard against the check passing because it found no routes at all.
    assert len(mutating) >= 5, f"expected the canon write surface, found {len(mutating)}"
    missing = [route.path for route in mutating if not _gate_carried(route)]
    assert missing == [], f"canon write routes without the human-author gate: {missing}"


def test_the_gate_is_not_attached_to_read_routes() -> None:
    """A gate on reads would break the connect-time read path for every agent."""
    reads = [
        route
        for route in canon_api.router.routes
        if getattr(route, "methods", None) and route.methods == {"GET"}
    ]
    assert reads, "expected canon read routes"
    assert [route.path for route in reads if _gate_carried(route)] == []


# --- 2. Behaviour: every non-human principal is refused --------------------


@pytest.mark.parametrize("auth_mode", ["mcp_oauth", "api_key", "browser_extension"])
@pytest.mark.parametrize("method,path", WRITE_ROUTES)
def test_non_human_principal_is_refused_on_every_write(auth_mode: str, method: str, path: str) -> None:
    response = _call(_client(auth_mode=auth_mode), method, path)

    assert response.status_code == 403, f"{method} {path} allowed {auth_mode}"
    detail = response.json()["detail"]
    assert detail["error"] == "canon_human_author_only"
    assert detail["reason"], "a refusal must say why"
    assert 'error="canon_human_author_only"' in response.headers["www-authenticate"]


@pytest.mark.parametrize("method,path", WRITE_ROUTES)
def test_an_agent_holding_a_write_grant_is_still_refused(method: str, path: str) -> None:
    """The wall is about who may author, not what the credential was scoped for.

    The principal below carries the full write scope. It must still receive the
    canon refusal rather than passing the capability check.
    """
    client = _client(auth_mode="mcp_oauth", scopes=["read", "write", "admin"])

    response = _call(client, method, path)

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "canon_human_author_only"


def test_an_unknown_auth_mode_is_refused() -> None:
    response = _call(_client(auth_mode="future_mode"), "POST", "/api/v1/canon/pages")

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "canon_human_author_only"


def test_an_agent_without_a_write_grant_still_gets_the_canon_refusal() -> None:
    """Pins the dependency order: policy before capability.

    If the capability check ran first, an agent holding only a read scope would
    receive `insufficient_scope` and would go looking for a wider grant, when the
    truth is that no grant of any size opens this door.
    """
    client = _client(auth_mode="mcp_oauth", scopes=["read"])

    response = _call(client, "POST", "/api/v1/canon/pages")

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "canon_human_author_only"


# --- 3. A human session is allowed through ---------------------------------


def _fake_write(slug: str = "company-charter") -> CanonPageWrite:
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
    item = Item(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        source_type="canon",
        title="Company charter",
        summary="What this company is for.",
        raw_content="We build agentic operations software.",
        metadata_={},
        tags=["canon"],
        categories=[],
        status="ready",
        created_at=now,
        updated_at=now,
    )
    page = CanonPage(
        id=uuid.uuid4(),
        tenant_id="tenant-a",
        item_id=item.id,
        room_id=None,
        slug=slug,
        section="charter",
        summary="What this company is for.",
        priority=100,
        pinned_in_digest=False,
        status="draft",
        char_count=38,
        created_at=now,
        updated_at=now,
    )
    return CanonPageWrite(page=page, item=item, revision=1, embedded=False)


@pytest.mark.parametrize("method,path", WRITE_ROUTES)
def test_a_human_session_passes_the_wall(monkeypatch, method: str, path: str) -> None:
    """Not refused. The handler runs; the service is stubbed so no DB is needed."""
    async def fake_create(*_args, **_kwargs):
        return _fake_write()

    async def fake_write(*_args, **_kwargs):
        return _fake_write()

    monkeypatch.setattr(canon_api, "create_canon_page", fake_create)
    monkeypatch.setattr(canon_api, "update_canon_page", fake_write)
    monkeypatch.setattr(canon_api, "publish_canon_page", fake_write)
    monkeypatch.setattr(canon_api, "ratify_canon_page", fake_write)
    monkeypatch.setattr(canon_api, "archive_canon_page", fake_write)

    response = _call(_client(auth_mode="browser_session"), method, path)

    assert response.status_code != 403, response.text
    assert response.status_code in (200, 201), response.text
    assert response.json()["page"]["slug"] == "company-charter"


def test_a_human_session_create_returns_201(monkeypatch) -> None:
    async def fake_create(*_args, **_kwargs):
        return _fake_write()

    monkeypatch.setattr(canon_api, "create_canon_page", fake_create)

    response = _client(auth_mode="browser_session").post("/api/v1/canon/pages", json=CREATE_BODY)

    assert response.status_code == 201
    body = response.json()
    assert body["revision"] == 1
    assert body["embedded"] is False
    assert body["page"]["status"] == "draft"


# --- 4. Reads stay open to agents ------------------------------------------


def test_an_agent_can_read_canon(monkeypatch) -> None:
    """Agents must read canon on connect, so read is not behind the wall."""
    async def fake_list(*_args, **_kwargs):
        return [], 0

    monkeypatch.setattr(canon_api, "list_canon_pages", fake_list)

    response = _client(auth_mode="mcp_oauth").get("/api/v1/canon/pages")

    assert response.status_code == 200
    assert response.json() == {"pages": [], "total": 0, "limit": 50, "offset": 0}


# --- projection safety -------------------------------------------------------
#
# A write can succeed and still hand back ORM instances whose attributes are
# unreadable. ``process_prebuilt_item`` rolls back on its own error paths, and a
# rollback in an async session expires every loaded instance; the next attribute
# read then raises MissingGreenlet, so the request 500s *after* the change was
# committed. The user is told it failed while it is actually live.
#
# Nothing in the request path can prove this from behaviour without a real
# Postgres, so the invariant is asserted on the source: every writer must re-read
# the page and item after its final commit and before it builds the response.

def _write_functions() -> list[ast.AsyncFunctionDef]:
    source = pathlib.Path("app/services/canon_authoring.py").read_text()
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name.endswith("_canon_page")
    ]


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return None


def test_every_canon_writer_reloads_before_returning() -> None:
    writers = _write_functions()
    assert {fn.name for fn in writers} == {
        "create_canon_page",
        "update_canon_page",
        "publish_canon_page",
        "ratify_canon_page",
        "archive_canon_page",
    }, "the writer set changed; check that the new writer also reloads"

    for fn in writers:
        # Source order, not tree order: the reload must follow the commit.
        ordered = sorted(
            (
                (node.lineno, "commit" if _is_db_commit(node) else "reload")
                for node in ast.walk(fn)
                if _is_db_commit(node) or _call_name(node) == "_reload_for_response"
            )
        )
        kinds = [kind for _, kind in ordered]
        assert "commit" in kinds, f"{fn.name} never commits"
        assert "reload" in kinds, f"{fn.name} never reloads the page before returning"
        last_commit = max(lineno for lineno, kind in ordered if kind == "commit")
        first_reload = min(lineno for lineno, kind in ordered if kind == "reload")
        assert first_reload > last_commit, (
            f"{fn.name} reloads before its final commit, so the objects it returns "
            "can still be expired"
        )

        return_node = next(
            (node for node in ast.walk(fn) if isinstance(node, ast.Return)), None
        )
        assert return_node is not None, f"{fn.name} has no return"
        assert return_node.lineno > first_reload, (
            f"{fn.name} returns before reloading, so the response is built from "
            "instances that may be expired"
        )


def _is_db_commit(node: ast.AST) -> bool:
    """True for an ``await db.commit()`` expression."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "commit"
        and isinstance(func.value, ast.Name)
        and func.value.id == "db"
    )
