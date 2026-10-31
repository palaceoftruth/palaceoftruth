"""Canon caps: every design section 11 budget, proven in the degenerate cases.

Acceptance for SAR-1531. Two rules run through the whole file:

1. No capped response is ever silent. If content was cut, the value object says
   ``truncated=True`` AND carries a ``next`` cursor. A flag without a cursor is
   a test failure here, not a cosmetic gap.
2. The degenerate cases are the point. 300 pages and a 40 KB identity page are
   exactly where a naive renderer either blows the budget or returns nothing
   useful.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.services import canon
from app.services.canon import (
    CANON_DIGEST_HARD_CHARS,
    CANON_DIGEST_IDENTITY_CHARS,
    CANON_DIGEST_INDEX_LINE_CHARS,
    CANON_DIGEST_MAX_INDEX_PAGES,
    CANON_DIGEST_RULES_CHARS,
    CANON_DIGEST_TARGET_CHARS,
    CANON_PAGE_BODY_MAX_CHARS,
    CANON_PAGE_GET_DEFAULT_CHARS,
    CANON_PAGE_GET_MAX_CHARS,
    CANON_PAGES_PER_TENANT_SOFT,
    CANON_PER_CONNECT_TOKEN_BUDGET,
    CANON_SEARCH_HITS,
    CANON_SEARCH_SNIPPET_CHARS,
    CANON_SUMMARY_MAX_CHARS,
    CanonCapExceeded,
    CanonDigestPage,
    CanonValidationError,
    build_canon_digest,
    canon_digest_fingerprint,
    clamp_page_max_chars,
    enforce_page_body,
    enforce_per_connect_budget,
    enforce_summary,
    effective_digest_budget,
    page_char_count,
    pages_per_tenant_warning,
    per_connect_token_estimate,
    search_canon_pages,
    slice_canon_page,
    validate_section,
    validate_status,
)

_NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def _page(
    slug: str,
    *,
    section: str = "reference",
    summary: str | None = None,
    body: str = "",
    priority: int = 100,
    pinned: bool = False,
    status: str = "published",
    char_count: int | None = None,
) -> CanonDigestPage:
    text = body or f"Body for {slug}."
    return CanonDigestPage(
        slug=slug,
        summary=summary if summary is not None else f"Summary for {slug}",
        section=section,
        priority=priority,
        pinned_in_digest=pinned,
        status=status,
        body=text,
        char_count=len(text) if char_count is None else char_count,
        updated_at=_NOW,
    )


def _assert_truncation_is_announced(*, truncated: bool, next_hint: str | None) -> None:
    """A cut without a cursor is an unpageable dead end."""
    if truncated:
        assert next_hint, "truncated response carried no next cursor"


# --- Cap constants match design section 11 --------------------------------


def test_cap_constants_match_the_design_table() -> None:
    assert CANON_DIGEST_TARGET_CHARS == 2000
    assert CANON_DIGEST_HARD_CHARS == 2400
    assert CANON_DIGEST_IDENTITY_CHARS == 400
    assert CANON_DIGEST_RULES_CHARS == 700
    assert CANON_DIGEST_INDEX_LINE_CHARS == 60
    assert CANON_DIGEST_MAX_INDEX_PAGES == 12
    assert CANON_PAGE_GET_DEFAULT_CHARS == 6000
    assert CANON_PAGE_GET_MAX_CHARS == 12000
    assert CANON_SEARCH_HITS == 8
    assert CANON_SEARCH_SNIPPET_CHARS == 300
    assert CANON_PER_CONNECT_TOKEN_BUDGET == 2500
    assert CANON_PAGE_BODY_MAX_CHARS == 20000
    assert CANON_SUMMARY_MAX_CHARS == 200
    assert CANON_PAGES_PER_TENANT_SOFT == 500


# --- Digest: degenerate cases ---------------------------------------------


def test_digest_of_300_pages_stays_inside_the_hard_cap() -> None:
    pages = [_page(f"page-{index:03d}", body="x" * 200) for index in range(300)]

    digest = build_canon_digest(pages)

    assert digest.char_count == len(digest.text)
    assert digest.char_count <= CANON_DIGEST_HARD_CHARS
    assert digest.page_count == 300
    assert digest.truncated is True
    _assert_truncation_is_announced(truncated=digest.truncated, next_hint=digest.next)
    assert digest.next is not None and "more pages" in digest.next
    index_lines = [line for line in digest.text.splitlines() if line.startswith("- ")]
    assert len(index_lines) == CANON_DIGEST_MAX_INDEX_PAGES
    assert all(len(line) <= CANON_DIGEST_INDEX_LINE_CHARS for line in index_lines)


def test_digest_of_40kb_identity_page_is_capped_and_pageable() -> None:
    identity_body = "IDENTITY " * 4000  # 36,000 chars, past the 20 KB store cap on purpose
    pages = [
        _page("identity-core", section="identity", body=identity_body),
        _page("rules-core", section="rules", body="Rule text." * 10),
        _page("glossary-core", section="glossary", body="Terms."),
    ]

    digest = build_canon_digest(pages)

    assert digest.char_count <= CANON_DIGEST_HARD_CHARS
    assert digest.truncated is True
    _assert_truncation_is_announced(truncated=digest.truncated, next_hint=digest.next)
    assert digest.next is not None and "identity-core" in digest.next
    identity_block = digest.text.split("## Identity\n", 1)[1].split("## Rules", 1)[0].strip()
    assert len(identity_block) <= CANON_DIGEST_IDENTITY_CHARS


def test_digest_of_40kb_identity_page_under_the_hard_cap_still_truncates() -> None:
    """A 40 KB identity page must never be presented as complete, even when the
    rest of the digest is empty and the whole render fits the budget."""
    pages = [_page("identity-core", section="identity", body="A" * 40000)]

    digest = build_canon_digest(pages)

    assert digest.truncated is True
    assert digest.next == "canon:get identity-core @400"
    assert digest.char_count <= CANON_DIGEST_HARD_CHARS


def test_digest_rules_block_is_capped() -> None:
    pages = [
        _page("identity-core", section="identity", body="We build software."),
        _page("rules-core", section="rules", body="R" * 5000),
    ]

    digest = build_canon_digest(pages)

    rules_block = digest.text.split("## Rules\n", 1)[1].split("## Index", 1)[0].strip()
    assert len(rules_block) <= CANON_DIGEST_RULES_CHARS
    assert digest.truncated is True
    assert digest.next == "canon:get rules-core @700"


def test_digest_budget_above_hard_cap_is_clamped_down() -> None:
    """The clamp is defence in depth: block caps keep a real digest small, so a
    budget clamp is only observable on the helper itself and on a corpus whose
    blocks are all at their caps."""
    assert effective_digest_budget(99999) == CANON_DIGEST_HARD_CHARS
    assert effective_digest_budget(CANON_DIGEST_TARGET_CHARS) == CANON_DIGEST_TARGET_CHARS
    assert effective_digest_budget(-5) == 0

    pages = [
        _page("identity-core", section="identity", body="A" * 40000),
        _page("rules-core", section="rules", body="R" * 40000),
        *[_page(f"page-{index:02d}", summary="S" * 100) for index in range(40)],
    ]

    digest = build_canon_digest(pages, budget_chars=99999)

    assert digest.char_count <= CANON_DIGEST_HARD_CHARS


def test_digest_with_a_degenerate_budget_is_still_pageable() -> None:
    pages = [_page(f"page-{index:02d}") for index in range(30)]

    digest = build_canon_digest(pages, budget_chars=50)

    assert digest.char_count <= 50
    assert digest.truncated is True
    _assert_truncation_is_announced(truncated=digest.truncated, next_hint=digest.next)


def test_digest_zero_budget_does_not_return_a_flag_without_a_cursor() -> None:
    digest = build_canon_digest([_page("only-page")], budget_chars=0)

    assert digest.char_count == 0
    assert digest.truncated is True
    assert digest.next


def test_digest_with_no_pages_and_no_budget_still_returns_a_cursor() -> None:
    """The fallback cursor: nothing to point at, and the response is still capped."""
    digest = build_canon_digest([], budget_chars=0)

    assert digest.truncated is True
    assert digest.next == "canon:digest @page 1"


def test_small_corpus_digest_reports_no_truncation() -> None:
    pages = [
        _page("identity-core", section="identity", body="We build software."),
        _page("rules-core", section="rules", body="Ship small changes."),
        _page("glossary-core", section="glossary", summary="Short summary."),
    ]

    digest = build_canon_digest(pages)

    assert digest.truncated is False
    assert digest.next is None
    assert digest.page_count == 3
    assert digest.char_count <= CANON_DIGEST_TARGET_CHARS


def test_a_long_summary_truncated_in_the_index_is_flagged() -> None:
    """One page, everything fits, but its index line was cut. Still a cut."""
    pages = [_page("long-summary", summary="S" * 120)]

    digest = build_canon_digest(pages)

    assert digest.truncated is True
    assert digest.next == "canon:digest @line long-summary"


def test_digest_index_orders_pinned_first_then_priority() -> None:
    pages = [
        _page("alpha", priority=10),
        _page("zulu", priority=1, pinned=True),
        _page("mike", priority=5),
    ]

    digest = build_canon_digest(pages)

    slugs = [line.split(":")[0].removeprefix("- ") for line in digest.text.splitlines() if line.startswith("- ")]
    assert slugs[0] == "zulu"
    assert slugs[1:] == ["mike", "alpha"]


def test_digest_excludes_unpublished_pages_from_blocks_and_index() -> None:
    pages = [
        _page("draft-identity", section="identity", body="DRAFT IDENTITY", status="draft"),
        _page("archived-rules", section="rules", body="ARCHIVED RULES", status="archived"),
        _page("live-page", summary="Live page."),
    ]

    digest = build_canon_digest(pages)

    assert "DRAFT IDENTITY" not in digest.text
    assert "ARCHIVED RULES" not in digest.text
    assert "live-page" in digest.text
    assert digest.page_count == 1


# --- Digest: fingerprint ---------------------------------------------------


def test_fingerprint_is_order_independent() -> None:
    pages = [_page(f"page-{index}") for index in range(5)]

    forward = canon_digest_fingerprint(pages, budget_chars=CANON_DIGEST_TARGET_CHARS)
    backward = canon_digest_fingerprint(list(reversed(pages)), budget_chars=CANON_DIGEST_TARGET_CHARS)

    assert forward == backward


def test_fingerprint_changes_with_page_state_and_budget() -> None:
    base = [_page("page-a"), _page("page-b")]
    baseline = canon_digest_fingerprint(base, budget_chars=CANON_DIGEST_TARGET_CHARS)

    edited = [_page("page-a", summary="A different summary."), _page("page-b")]
    assert canon_digest_fingerprint(edited, budget_chars=CANON_DIGEST_TARGET_CHARS) != baseline
    assert canon_digest_fingerprint(base, budget_chars=1200) != baseline


def test_digest_carries_the_fingerprint_of_its_pages() -> None:
    pages = [_page("page-a")]

    digest = build_canon_digest(pages)

    assert digest.fingerprint == canon_digest_fingerprint(pages, budget_chars=CANON_DIGEST_TARGET_CHARS)


# --- Page get --------------------------------------------------------------


def test_page_get_defaults_to_6000_and_clamps_to_12000() -> None:
    assert clamp_page_max_chars(None) == CANON_PAGE_GET_DEFAULT_CHARS
    assert clamp_page_max_chars(99999) == CANON_PAGE_GET_MAX_CHARS
    assert clamp_page_max_chars(1) == 1
    assert clamp_page_max_chars(0) >= 1, "a zero-width window would never advance the cursor"


def test_page_slice_of_40kb_page_is_capped_and_pageable() -> None:
    page = _page("identity-core", section="identity", body="B" * 40000)

    sliced = slice_canon_page(page)

    assert len(sliced.body) == CANON_PAGE_GET_DEFAULT_CHARS
    assert sliced.total_char_count == 40000
    assert sliced.truncated is True
    assert sliced.next == "canon:get identity-core @6000"


def test_page_slice_never_exceeds_the_hard_max() -> None:
    page = _page("identity-core", body="B" * 40000)

    sliced = slice_canon_page(page, max_chars=99999)

    assert len(sliced.body) == CANON_PAGE_GET_MAX_CHARS
    assert sliced.truncated is True


def test_page_slice_paging_covers_the_whole_body_without_gaps() -> None:
    body = "".join(chr(ord("a") + (index % 26)) for index in range(40000))
    page = _page("long-page", body=body)

    collected = ""
    offset = 0
    windows = 0
    while True:
        sliced = slice_canon_page(page, max_chars=3000, offset=offset)
        collected += sliced.body
        windows += 1
        if not sliced.truncated:
            break
        assert sliced.next is not None
        offset = int(sliced.next.rsplit("@", 1)[1])
        assert windows < 100, "paging did not converge"

    assert collected == body
    assert windows == 14  # 13 full 3000-char windows plus the 1000-char tail


def test_page_slice_of_a_short_page_reports_no_truncation() -> None:
    page = _page("short", body="Short body.")

    sliced = slice_canon_page(page)

    assert sliced.body == "Short body."
    assert sliced.truncated is False
    assert sliced.next is None


# --- Search ----------------------------------------------------------------


def test_search_caps_hits_at_eight() -> None:
    pages = [_page(f"policy-{index:02d}", body="policy detail") for index in range(20)]

    hits = search_canon_pages("policy", pages, limit=99)

    assert len(hits) == CANON_SEARCH_HITS


def test_search_snippet_is_capped() -> None:
    pages = [_page("policy-long", body="policy " + ("z" * 5000))]

    hits = search_canon_pages("policy", pages)

    assert len(hits) == 1
    assert len(hits[0].snippet) <= CANON_SEARCH_SNIPPET_CHARS
    assert hits[0].truncated is True


def test_search_ignores_unpublished_pages_and_empty_queries() -> None:
    pages = [_page("draft-policy", body="policy", status="draft")]

    assert search_canon_pages("policy", pages) == []
    assert search_canon_pages("   ", [_page("live-policy", body="policy")]) == []


def test_search_ranks_slug_matches_above_body_matches() -> None:
    pages = [
        _page("body-only", body="mentions governance once"),
        _page("governance", body="unrelated text"),
    ]

    hits = search_canon_pages("governance", pages)

    assert [hit.slug for hit in hits] == ["governance", "body-only"]


# --- Field caps ------------------------------------------------------------


def test_summary_is_mandatory_and_capped() -> None:
    assert enforce_summary("  Short summary.  ") == "Short summary."
    with pytest.raises(CanonValidationError):
        enforce_summary(None)
    with pytest.raises(CanonValidationError):
        enforce_summary("   ")
    with pytest.raises(CanonValidationError):
        enforce_summary("s" * (CANON_SUMMARY_MAX_CHARS + 1))
    assert enforce_summary("s" * CANON_SUMMARY_MAX_CHARS) == "s" * CANON_SUMMARY_MAX_CHARS


def test_page_body_cap_is_hard() -> None:
    assert enforce_page_body("b" * CANON_PAGE_BODY_MAX_CHARS) == "b" * CANON_PAGE_BODY_MAX_CHARS
    with pytest.raises(CanonCapExceeded):
        enforce_page_body("b" * (CANON_PAGE_BODY_MAX_CHARS + 1))
    assert page_char_count("abcd") == 4


def test_section_and_status_vocabularies_are_closed() -> None:
    assert validate_section("charter") == "charter"
    assert validate_status("published") == "published"
    with pytest.raises(CanonValidationError):
        validate_section("blog")
    with pytest.raises(CanonValidationError):
        validate_status("live")


# --- Per-connect budget ----------------------------------------------------


def test_per_connect_budget_is_a_hard_stop() -> None:
    text = "c" * 40000

    cut = enforce_per_connect_budget(text)

    assert len(cut) < len(text)
    assert per_connect_token_estimate(cut) <= CANON_PER_CONNECT_TOKEN_BUDGET


def test_per_connect_budget_leaves_a_small_text_alone() -> None:
    text = "small"

    assert enforce_per_connect_budget(text) == text


def test_tenant_page_soft_ceiling_warns_only_past_500() -> None:
    assert pages_per_tenant_warning(CANON_PAGES_PER_TENANT_SOFT) is False
    assert pages_per_tenant_warning(CANON_PAGES_PER_TENANT_SOFT + 1) is True


# --- No cap lives outside this module --------------------------------------


def test_no_other_module_defines_a_canon_cap() -> None:
    """One module owns every cap, so caps cannot drift between callers.

    Scoped to *numeric* cap definitions. A cap is a budget, and every budget in
    design section 11 is a number; a `CANON_`-prefixed policy constant such as
    the human-author auth-mode set is not a cap and must not trip this. A bare
    prefix match was wrong once already in this file's history.
    """
    import re
    from pathlib import Path

    app_dir = Path(__file__).parents[1] / "app"
    definition = re.compile(r"^CANON_[A-Z0-9_]+\s*=\s*[0-9]", re.MULTILINE)
    offenders = [
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if path.name != "canon.py" and definition.search(path.read_text())
    ]
    assert offenders == []
