"""Canon outranks an equivalent raw capture in general search.

The design asks for one thing: when a canon page and a raw capture tie on
semantic score, the canon page wins. This test builds that exact tie and checks
the ordering, rather than asserting a constant and hoping the constant is wired.

The regression risk here is real. `_score_candidate` is shared by every search
path, so a careless canon bonus would reorder results that existing tests pin.
Those tests are the other half of this proof: this file adds a canon case, and
the existing hybrid ordering suite must stay green beside it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.services.search import (
    _CURATED_SOURCE_BONUS,
    _QUERY_INTENTS,
    _SearchCandidate,
    _score_candidate,
)

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


def _candidate(*, source_type: str, score: float = 0.5, title: str = "Company charter") -> _SearchCandidate:
    return _SearchCandidate(
        item_id=uuid.uuid4(),
        title=title,
        summary="What this company is for.",
        source_type=source_type,
        source_url=None,
        tags=[],
        created_at=NOW,
        effective_date=None,
        effective_date_source=None,
        effective_date_quality=None,
        chunk_text="We build agentic operations software.",
        chunk_index=0,
        score=score,
        item_metadata=None,
    )


def _score(candidate: _SearchCandidate) -> tuple[float, dict[str, float]]:
    return _score_candidate(
        candidate,
        "what is this company for",
        intent=_QUERY_INTENTS["canonical_factual"],
        source_ranking_enabled=True,
    )


def test_canon_receives_a_curated_source_bonus() -> None:
    _total, adjustments = _score(_candidate(source_type="canon"))

    assert adjustments["curated_source"] == _CURATED_SOURCE_BONUS["canon"]


def test_canon_outranks_an_equivalent_capture() -> None:
    """The tie is the whole point. Equal semantic score, canon wins."""
    canon_total, _ = _score(_candidate(source_type="canon"))
    markdown_total, _ = _score(_candidate(source_type="markdown"))
    plain_total, _ = _score(_candidate(source_type="capture"))

    assert canon_total > markdown_total
    assert canon_total > plain_total


def test_the_canon_bonus_is_the_largest_curated_bonus() -> None:
    """If a future source type is given a bigger bonus, canon stops being the
    top tier and this fails, which is the conversation that should happen."""
    canon = _CURATED_SOURCE_BONUS["canon"]
    others = {key: value for key, value in _CURATED_SOURCE_BONUS.items() if key != "canon"}

    assert others, "expected other curated source types to exist"
    assert canon > max(others.values()), f"canon {canon} is not above {others}"


def test_a_canon_page_does_not_get_an_unbounded_boost() -> None:
    """Canon is a tiebreaker, not a trump card. A capture that is genuinely a
    better semantic match must still be able to win."""
    canon_total, _ = _score(_candidate(source_type="canon", score=0.50))
    strong_capture_total, _ = _score(_candidate(source_type="capture", score=0.80))

    assert strong_capture_total > canon_total


def test_a_non_canon_source_gets_no_canon_bonus() -> None:
    _total, adjustments = _score(_candidate(source_type="capture"))

    assert "curated_source" not in adjustments
