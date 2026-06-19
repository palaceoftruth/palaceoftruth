"""Vector + hybrid search service using pgvector halfvec and PostgreSQL full-text."""
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.embedding_profile import EMBEDDING_DIMENSIONS, EmbeddingProfile, is_default_embedding_profile, resolve_embedding_profile
from app.schemas.search import SearchContextChunk, SearchResult, split_system_provenance_tags
from app.services.artifact_citations import build_artifact_citation
from app.services.embedder import EmbeddingService
from app.services.memory_entries import source_project_from_memory_metadata
from app.services.retrieval_provenance import build_retrieval_provenance
from app.services.retrieval_hints import report_retrieval_hint_candidates, score_retrieval_hints_for_items
from app.services.retrieval_lenses import resolve_retrieval_lens

logger = logging.getLogger(__name__)

_VECTOR_WEIGHT = 0.7
_TEXT_WEIGHT = 0.3
_MIN_CANDIDATE_LIMIT = 20
_MAX_CANDIDATE_LIMIT = 50
_MAX_EXPLICIT_CANDIDATE_LIMIT = 200
_SELF_MEMORY_NOTE_PENALTY = 0.05
_NEGATIVE_SELF_MEMORY_NOTE_PENALTY = 0.18
_EXACT_TITLE_MATCH_BONUS = 0.12
_TITLE_PHRASE_MATCH_BONUS = 0.08
_SOURCE_URL_PHRASE_MATCH_BONUS = 0.07
_BODY_PHRASE_MATCH_BONUS = 0.05
_FULL_TOKEN_COVERAGE_BONUS = 0.04
_TITLE_TOKEN_COVERAGE_BONUS = 0.10
_BODY_TOKEN_COVERAGE_BONUS = 0.08
_STRONG_FIELD_PHRASE_MATCH_BONUS = 0.08
_LEADING_TITLE_PHRASE_MATCH_BONUS = 0.035
_SHORT_TITLE_PHRASE_MATCH_BONUS = 0.12
_MAX_LEXICAL_RESCUE_BONUS = 0.28
_SOURCE_AUTHORITY_URL_BONUS = 0.04
_SOURCE_AUTHORITY_METADATA_BONUS = 0.03
_SOURCE_OPERATIONAL_TAG_BONUS = 0.025
_NIST_SOURCE_ROLE_MATCH_BONUS = 0.18
_NIST_SOURCE_ROLE_DECOY_PENALTY = 0.14
_LOW_SIGNAL_SOURCE_PENALTY = 0.08
_LOW_SIGNAL_TAG_PENALTY = 0.05
_CURATED_SOURCE_BONUS = {
    "media": 0.06,
    "feed_article": 0.05,
    "pdf": 0.05,
    "doc": 0.05,
    "web": 0.04,
    "markdown": 0.04,
}
_AUTHORITY_URL_PATTERNS = (
    "doi.org/",
    "nist.gov",
    "csrc.nist.gov",
    ".gov/",
    ".mil/",
)
_AUTHORITY_SOURCE_TYPES = {"pdf", "doc", "markdown", "web", "feed_article"}
_AUTHORITY_TAGS = {
    "authority",
    "authoritative",
    "official",
    "primary-source",
    "standard",
    "standards",
    "policy",
    "compliance",
    "nist-sp800",
}
_OPERATIONAL_TAGS = {"runbook", "incident", "ops", "operational", "control-tower"}
_LOW_SIGNAL_SOURCE_TYPES = {"transcript", "log", "chat", "conversation"}
_LOW_SIGNAL_TAGS = {
    "transcript",
    "raw-transcript",
    "chat-log",
    "conversation-log",
    "debug-log",
    "logs",
}
_DERIVED_ARTIFACT_METADATA_KEYS = (
    "memory_dream",
    "diary_rollup",
    "wakeup_brief",
    "retrieval_hint",
    "conversation_fact",
)
_DERIVED_ARTIFACT_SOURCE_URL_PREFIXES = {
    "memory://dream/": "memory_dream",
    "memory://diary-rollup/": "diary_rollup",
    "memory://wakeup-brief/": "wakeup_brief",
}
_DERIVED_ARTIFACT_TAG_KEYS = {
    "memory-dream": "memory_dream",
    "diary-rollup": "diary_rollup",
    "wake-up-brief": "wakeup_brief",
    "wakeup-brief": "wakeup_brief",
    "palace-routing-manifest": "memory_dream",
    "retrieval-hint": "retrieval_hint",
    "conversation-fact": "conversation_fact",
}
_ARTIFACT_PROVENANCE_LABELS = {
    "canonical_memory": "Canonical memory",
    "legacy_memory_artifact": "Legacy memory artifact",
    "memory_dream": "Memory Dream",
    "diary_rollup": "Diary rollup",
    "wakeup_brief": "Wake-up brief",
    "retrieval_hint": "Retrieval hint",
    "conversation_fact": "Conversation fact",
    "corpus_item": "Broad corpus item",
}
_DERIVED_ARTIFACT_PENALTY = 0.45
_INTENT_RECENCY_MAX_BONUS = 0.12
_INTENT_RECENCY_HALF_LIFE_DAYS = 14.0
_EFFECTIVE_DATE_QUALITY_WEIGHT = {
    "high": 1.0,
    "medium": 0.75,
    "low": 0.35,
}
_STARTUP_CONTEXT_QUERY_PHRASES = (
    "startup context",
    "start up context",
    "wake up",
    "wake-up",
    "wakeup",
    "opening brief",
    "session brief",
)
_CATCH_UP_QUERY_PHRASES = (
    "catch me up",
    "catch-up",
    "catch up",
    "what did i miss",
    "recap",
    "summarize",
    "summary",
    "briefing",
    "brief",
    "digest",
)
_LATEST_STATUS_QUERY_TERMS = {
    "latest",
    "current",
    "status",
    "recent",
    "newest",
    "today",
    "yesterday",
    "tonight",
    "update",
    "updates",
    "now",
}
_TEMPORAL_QUERY_TERMS = {
    "before",
    "after",
    "since",
    "until",
    "during",
    "when",
    "timeline",
    "history",
    "week",
    "month",
    "year",
}
_EXPLORATORY_QUERY_PHRASES = (
    "find anything",
    "anything about",
    "related to",
    "explore",
    "brainstorm",
    "ideas",
    "similar",
)
_CANONICAL_QUERY_TERMS = {
    "canonical",
    "factual",
    "official",
    "governing",
    "source",
    "policy",
    "standard",
    "standards",
    "nist",
    "rmf",
    "framework",
    "definition",
    "define",
}
_NEGATIVE_SELF_MEMORY_PATTERNS = (
    "don't have any stored knowledge",
    "do not have any stored knowledge",
    "don't know",
    "do not know",
    "doesn't appear",
    "does not appear",
    "not in memory",
    "not in palace of truth",
    "not in palaceoftruth",
    "couldn't find",
    "could not find",
    "can't find",
    "cannot find",
    "didn't surface",
    "did not surface",
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_NIST_PUBLICATION_RE = re.compile(
    r"\b800[-\s]?(53ar5|53a|53b|160v1r1|160v2r1|[0-9]+(?:r[0-9]+)?)\b",
    re.IGNORECASE,
)

_NIST_PUBLICATION_SOURCE_ROLES = {
    "80053r5": "control-catalog",
    "80053ar5": "control-assessment",
    "80053b": "control-baseline",
    "80037r2": "rmf-lifecycle",
    "80039": "enterprise-risk",
    "80030r1": "risk-assessment",
    "800144": "public-cloud-risk",
    "800145": "cloud-definition",
    "800207": "zero-trust-architecture",
    "800160v1r1": "systems-security-engineering",
    "800160v2r1": "cyber-resiliency",
    "800161r1": "supply-chain-risk",
    "800218": "secure-software-framework",
}
_NIST_SOURCE_ROLE_QUERY_PHRASES = {
    "control-assessment": (
        "assessment procedure",
        "assessment procedures",
        "control assessment",
    ),
    "control-baseline": (
        "control baseline",
        "control baselines",
        "baseline material",
        "baselines",
    ),
    "control-catalog": (
        "control catalog",
        "security and privacy controls",
    ),
    "rmf-lifecycle": (
        "risk management framework",
        "rmf steps",
        "categorize select implement assess authorize monitor",
    ),
    "enterprise-risk": (
        "organization level risk",
        "organization mission business process",
        "enterprise risk",
        "risk framing",
    ),
    "risk-assessment": (
        "risk assessment process",
        "conducting risk assessments",
        "threat vulnerability likelihood impact",
    ),
    "cloud-definition": (
        "cloud definition",
        "cloud characteristics",
        "essential characteristics",
        "service models deployment models",
    ),
    "public-cloud-risk": (
        "public cloud risk",
        "public cloud computing outsourcing risk",
        "risk guidance",
    ),
    "zero-trust-architecture": (
        "zero trust architecture",
        "policy engine",
        "policy administrator",
    ),
    "systems-security-engineering": (
        "systems security engineering",
        "trustworthy secure resilient systems",
    ),
    "cyber-resiliency": (
        "cyber resiliency",
        "cyber resilient",
        "resiliency technique",
        "resiliency techniques",
    ),
    "supply-chain-risk": (
        "supply chain risk",
        "cybersecurity supply chain risk management",
        "suppliers acquirers",
    ),
    "secure-software-framework": (
        "secure software development framework",
        "secure software framework",
        "ssdf",
        "practices tasks",
    ),
}
_NIST_SOURCE_ROLE_DECOYS = {
    "control-assessment": {"control-catalog", "control-baseline"},
    "control-baseline": {"control-catalog", "control-assessment"},
    "control-catalog": {"control-baseline", "control-assessment"},
    "rmf-lifecycle": {"enterprise-risk", "risk-assessment"},
    "enterprise-risk": {"rmf-lifecycle", "risk-assessment"},
    "risk-assessment": {"rmf-lifecycle", "enterprise-risk"},
    "cloud-definition": {"public-cloud-risk"},
    "public-cloud-risk": {"cloud-definition"},
    "systems-security-engineering": {"cyber-resiliency"},
    "cyber-resiliency": {"systems-security-engineering"},
    "supply-chain-risk": {"control-catalog", "enterprise-risk"},
    "secure-software-framework": {"supply-chain-risk", "control-catalog"},
    "zero-trust-architecture": {"control-catalog"},
}


@dataclass(frozen=True)
class _EmbeddingSearchPlan:
    table_name: str
    vector_column: str
    half_column: str
    dimensions: int
    profile_filter: str
    profile_name: str | None


def _embedding_search_plan(profile: EmbeddingProfile) -> _EmbeddingSearchPlan:
    if is_default_embedding_profile(profile):
        return _EmbeddingSearchPlan(
            table_name="embeddings",
            vector_column="embedding",
            half_column="embedding_half",
            dimensions=EMBEDDING_DIMENSIONS,
            profile_filter="",
            profile_name=None,
        )
    if profile.dimensions not in {384, 768, 1024, 1536}:
        raise ValueError(f"unsupported embedding profile dimensions: {profile.dimensions}")
    return _EmbeddingSearchPlan(
        table_name="embedding_profile_vectors",
        vector_column=f"embedding_{profile.dimensions}",
        half_column=f"embedding_half_{profile.dimensions}",
        dimensions=profile.dimensions,
        profile_filter="AND e.profile_name = :embedding_profile_name AND e.dimensions = :embedding_dimensions",
        profile_name=profile.profile_name,
    )


@dataclass(frozen=True)
class QueryIntent:
    name: str
    allow_recency: bool
    allow_salience: bool
    allow_derived_artifacts: bool
    allow_graph_expansion: bool
    allow_strict_source_boosts: bool


_QUERY_INTENTS = {
    "canonical_factual": QueryIntent(
        name="canonical_factual",
        allow_recency=False,
        allow_salience=False,
        allow_derived_artifacts=False,
        allow_graph_expansion=False,
        allow_strict_source_boosts=True,
    ),
    "latest_status": QueryIntent(
        name="latest_status",
        allow_recency=True,
        allow_salience=True,
        allow_derived_artifacts=False,
        allow_graph_expansion=False,
        allow_strict_source_boosts=False,
    ),
    "catch_up_summary": QueryIntent(
        name="catch_up_summary",
        allow_recency=True,
        allow_salience=True,
        allow_derived_artifacts=True,
        allow_graph_expansion=False,
        allow_strict_source_boosts=False,
    ),
    "temporal": QueryIntent(
        name="temporal",
        allow_recency=True,
        allow_salience=False,
        allow_derived_artifacts=False,
        allow_graph_expansion=False,
        allow_strict_source_boosts=False,
    ),
    "exploratory": QueryIntent(
        name="exploratory",
        allow_recency=False,
        allow_salience=True,
        allow_derived_artifacts=False,
        allow_graph_expansion=True,
        allow_strict_source_boosts=False,
    ),
    "startup_context": QueryIntent(
        name="startup_context",
        allow_recency=True,
        allow_salience=True,
        allow_derived_artifacts=True,
        allow_graph_expansion=True,
        allow_strict_source_boosts=False,
    ),
}


@dataclass(frozen=True)
class _SearchCandidate:
    item_id: Any
    title: str
    summary: str | None
    source_type: str
    source_url: str | None
    tags: list[str]
    created_at: datetime
    effective_date: datetime | None
    effective_date_source: str | None
    effective_date_quality: str | None
    chunk_text: str
    chunk_index: int
    score: float
    item_metadata: dict[str, Any] | None


@dataclass(frozen=True)
class _RuntimeRerankDecision:
    item_id: Any
    score: float
    reason: str


class _RuntimeReranker(Protocol):
    name: str

    def rerank(self, *, query: str, candidates: list[_SearchCandidate]) -> list[_RuntimeRerankDecision]:
        ...


class _LexicalOverlapRuntimeReranker:
    name = "lexical-overlap"

    def rerank(self, *, query: str, candidates: list[_SearchCandidate]) -> list[_RuntimeRerankDecision]:
        query_tokens = _token_set(query)
        if not query_tokens:
            return []
        decisions: list[_RuntimeRerankDecision] = []
        for candidate in candidates:
            candidate_tokens = (
                _token_set(candidate.title)
                | _token_set(candidate.summary)
                | _token_set(candidate.chunk_text)
            )
            overlap = len(query_tokens & candidate_tokens) / len(query_tokens)
            if overlap:
                decisions.append(
                    _RuntimeRerankDecision(
                        item_id=candidate.item_id,
                        score=round(overlap, 6),
                        reason="query_token_overlap",
                    )
                )
        return decisions


def _runtime_reranker_from_settings() -> _RuntimeReranker | None:
    provider = settings.retrieval_second_stage_reranker_provider.strip().lower()
    if not provider:
        return None
    if provider == "lexical-overlap":
        return _LexicalOverlapRuntimeReranker()
    raise ValueError(f"unsupported second-stage reranker provider: {provider}")


def _candidate_fetch_limit(limit: int, candidate_limit: int | None = None) -> int:
    if candidate_limit is None:
        return min(max(limit * 4, _MIN_CANDIDATE_LIMIT), _MAX_CANDIDATE_LIMIT)
    return min(max(candidate_limit, limit, 1), _MAX_EXPLICIT_CANDIDATE_LIMIT)


def _is_agent_self_note(candidate: _SearchCandidate) -> bool:
    memory_entry = (candidate.item_metadata or {}).get("memory_entry") or {}
    scope = memory_entry.get("scope") or {}
    return (
        candidate.source_type == "note"
        and memory_entry.get("source") == "hermes-agent"
        and memory_entry.get("created_by_role") == "assistant"
        and scope.get("type") == "agent"
    )


def _looks_like_negative_self_memory(candidate: _SearchCandidate) -> bool:
    haystack = " ".join(
        part for part in (candidate.title, candidate.summary, candidate.chunk_text) if part
    ).lower()
    return any(pattern in haystack for pattern in _NEGATIVE_SELF_MEMORY_PATTERNS)


def _metadata_value(candidate: _SearchCandidate, key: str) -> Any:
    metadata = candidate.item_metadata or {}
    memory_entry = metadata.get("memory_entry") if isinstance(metadata.get("memory_entry"), dict) else {}
    return metadata.get(key) or memory_entry.get(key)


def _retrieved_scope_from_metadata(item_metadata: dict[str, Any] | None) -> tuple[str | None, str | None, str]:
    memory_entry = (item_metadata or {}).get("memory_entry")
    if not isinstance(memory_entry, dict):
        return None, None, "general"
    scope = memory_entry.get("scope")
    if not isinstance(scope, dict):
        return None, None, "general"
    scope_type = str(scope.get("type") or "").strip()
    scope_key = str(scope.get("key") or "").strip() or None
    if not scope_type:
        return None, None, "general"
    if scope_type == "tenant_shared":
        return scope_type, None, "tenant_shared"
    if scope_key:
        return scope_type, scope_key, f"{scope_type}/{scope_key}"
    return scope_type, None, scope_type


def _conversation_fact_metadata(item_metadata: dict[str, Any] | None) -> dict[str, Any]:
    metadata = item_metadata or {}
    top_level = metadata.get("conversation_fact")
    if isinstance(top_level, dict):
        return top_level
    memory_entry = metadata.get("memory_entry")
    if not isinstance(memory_entry, dict):
        return {}
    client_metadata = memory_entry.get("metadata")
    if not isinstance(client_metadata, dict):
        return {}
    nested = client_metadata.get("conversation_fact")
    return nested if isinstance(nested, dict) else {}


def _conversation_fact_source_item_id(item_metadata: dict[str, Any] | None) -> Any | None:
    value = _conversation_fact_metadata(item_metadata).get("source_item_id")
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _conversation_fact_source_span(item_metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    span = _conversation_fact_metadata(item_metadata).get("source_span")
    return span if isinstance(span, dict) else None


def _derived_artifact_keys(candidate: _SearchCandidate) -> tuple[str, ...]:
    metadata = candidate.item_metadata or {}
    keys = [
        key
        for key in _DERIVED_ARTIFACT_METADATA_KEYS
        if isinstance(metadata.get(key), dict)
    ]
    source_url = (candidate.source_url or "").lower()
    for prefix, key in _DERIVED_ARTIFACT_SOURCE_URL_PREFIXES.items():
        if source_url.startswith(prefix) and key not in keys:
            keys.append(key)
    for tag in candidate.tags:
        key = _DERIVED_ARTIFACT_TAG_KEYS.get(tag.lower())
        if key and key not in keys:
            keys.append(key)
    return tuple(keys)


def _artifact_provenance(candidate: _SearchCandidate) -> tuple[str, str]:
    derived_keys = _derived_artifact_keys(candidate)
    if derived_keys:
        key = derived_keys[0]
        return key, _ARTIFACT_PROVENANCE_LABELS.get(key, key.replace("_", " ").title())

    metadata = candidate.item_metadata or {}
    memory_entry = metadata.get("memory_entry")
    if isinstance(memory_entry, dict):
        key = "legacy_memory_artifact" if memory_entry.get("legacy_kind") else "canonical_memory"
        return key, _ARTIFACT_PROVENANCE_LABELS[key]

    return "corpus_item", _ARTIFACT_PROVENANCE_LABELS["corpus_item"]


def _tags_request_derived_artifacts(tags: list[str] | None) -> bool:
    return any(tag.lower() in _DERIVED_ARTIFACT_TAG_KEYS for tag in (tags or []))


def classify_query_intent(query: str) -> QueryIntent:
    normalized = " ".join(_TOKEN_RE.findall(query.lower()))
    if not normalized:
        return _QUERY_INTENTS["canonical_factual"]
    tokens = set(normalized.split())
    if _contains_normalized_phrase(normalized, _STARTUP_CONTEXT_QUERY_PHRASES):
        return _QUERY_INTENTS["startup_context"]
    if _contains_normalized_phrase(normalized, _CATCH_UP_QUERY_PHRASES):
        return _QUERY_INTENTS["catch_up_summary"]
    if tokens & _LATEST_STATUS_QUERY_TERMS:
        return _QUERY_INTENTS["latest_status"]
    if tokens & _TEMPORAL_QUERY_TERMS or _YEAR_RE.search(normalized):
        return _QUERY_INTENTS["temporal"]
    if _contains_normalized_phrase(normalized, _EXPLORATORY_QUERY_PHRASES):
        return _QUERY_INTENTS["exploratory"]
    if tokens & _CANONICAL_QUERY_TERMS:
        return _QUERY_INTENTS["canonical_factual"]
    return _QUERY_INTENTS["canonical_factual"]


def _contains_normalized_phrase(normalized_query: str, phrases: tuple[str, ...]) -> bool:
    return any(" ".join(_TOKEN_RE.findall(phrase)) in normalized_query for phrase in phrases)


def _query_allows_derived_artifacts(query: str) -> bool:
    return classify_query_intent(query).allow_derived_artifacts


def _apply_derived_artifact_policy(intent: QueryIntent, *, include_derived_artifacts: bool) -> QueryIntent:
    if not include_derived_artifacts or intent.allow_derived_artifacts:
        return intent
    return replace(intent, allow_derived_artifacts=True)


def _normalized_phrase(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(_TOKEN_RE.findall(value.lower()))


def _token_set(value: str | None) -> set[str]:
    return set(_TOKEN_RE.findall((value or "").lower()))


def _token_list(value: str | None) -> list[str]:
    return _TOKEN_RE.findall((value or "").lower())


def _normalize_nist_publication_id(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", value.lower())
    if normalized.startswith("nistsp"):
        normalized = normalized[6:]
    if normalized.startswith("sp"):
        normalized = normalized[2:]
    if normalized in _NIST_PUBLICATION_SOURCE_ROLES:
        return normalized
    return None


def _candidate_nist_publication_id(candidate: _SearchCandidate) -> str | None:
    metadata_value = _metadata_value(candidate, "publication_id")
    if (publication_id := _normalize_nist_publication_id(str(metadata_value) if metadata_value else None)):
        return publication_id

    nist_metadata = _metadata_value(candidate, "nist")
    if isinstance(nist_metadata, dict):
        publication = nist_metadata.get("publication_id")
        if (publication_id := _normalize_nist_publication_id(str(publication) if publication else None)):
            return publication_id

    haystack = " ".join(
        part
        for part in (candidate.title, candidate.summary, candidate.source_url, candidate.chunk_text[:200])
        if part
    )
    for match in _NIST_PUBLICATION_RE.finditer(haystack):
        if (publication_id := _normalize_nist_publication_id(match.group(0))):
            return publication_id
    return None


def _candidate_nist_source_role(candidate: _SearchCandidate) -> str | None:
    publication_id = _candidate_nist_publication_id(candidate)
    if publication_id is None:
        return None
    return _NIST_PUBLICATION_SOURCE_ROLES.get(publication_id)


def _query_nist_source_role(query: str) -> str | None:
    normalized_query = _normalized_phrase(query)
    if not normalized_query:
        return None

    matches: list[tuple[int, int, str]] = []
    for role, phrases in _NIST_SOURCE_ROLE_QUERY_PHRASES.items():
        for phrase in phrases:
            normalized_phrase = _normalized_phrase(phrase)
            position = normalized_query.find(normalized_phrase)
            if position >= 0:
                matches.append((position, -len(normalized_phrase), role))
    if not matches:
        return None
    matches.sort()
    return matches[0][2]


def _nist_source_role_adjustments(query: str, candidate: _SearchCandidate) -> dict[str, float]:
    query_role = _query_nist_source_role(query)
    candidate_role = _candidate_nist_source_role(candidate)
    if not query_role or not candidate_role:
        return {}
    if candidate_role == query_role:
        return {"nist_source_role_match": _NIST_SOURCE_ROLE_MATCH_BONUS}
    if candidate_role in _NIST_SOURCE_ROLE_DECOYS.get(query_role, set()):
        return {"nist_source_role_decoy": -_NIST_SOURCE_ROLE_DECOY_PENALTY}
    return {}


def _has_contiguous_token_overlap(query_tokens: list[str], field_tokens: list[str], *, min_length: int) -> bool:
    if len(query_tokens) < min_length or len(field_tokens) < min_length:
        return False
    query_windows = {
        tuple(query_tokens[index : index + min_length])
        for index in range(len(query_tokens) - min_length + 1)
    }
    return any(
        tuple(field_tokens[index : index + min_length]) in query_windows
        for index in range(len(field_tokens) - min_length + 1)
    )


def _has_leading_query_phrase_in_field(
    query_tokens: list[str],
    field_tokens: list[str],
    *,
    min_length: int,
) -> bool:
    if len(query_tokens) < min_length or len(field_tokens) < min_length:
        return False
    leading_query_phrase = tuple(query_tokens[:min_length])
    return any(
        tuple(field_tokens[index : index + min_length]) == leading_query_phrase
        for index in range(len(field_tokens) - min_length + 1)
    )


def _token_coverage_bonus(
    query_tokens: set[str],
    field_tokens: set[str],
    *,
    weight: float,
    min_coverage: float,
) -> float:
    if len(query_tokens) < 4:
        return 0.0
    coverage = len(query_tokens & field_tokens) / len(query_tokens)
    if coverage < min_coverage:
        return 0.0
    return weight * coverage


def _lexical_rescue_bonus(query: str, candidate: _SearchCandidate) -> float:
    if _is_agent_self_note(candidate):
        return 0.0

    normalized_query = _normalized_phrase(query)
    if len(normalized_query) < 3:
        return 0.0

    title_phrase = _normalized_phrase(candidate.title)
    summary_phrase = _normalized_phrase(candidate.summary)
    chunk_phrase = _normalized_phrase(candidate.chunk_text)
    source_url_phrase = _normalized_phrase(candidate.source_url)
    title_tokens = _token_set(candidate.title)
    summary_tokens = _token_set(candidate.summary)
    chunk_tokens = _token_set(candidate.chunk_text)
    source_url_tokens = _token_set(candidate.source_url)
    query_tokens = _token_set(query)
    query_token_list = _token_list(query)
    title_token_list = _token_list(candidate.title)
    summary_token_list = _token_list(candidate.summary)
    chunk_token_list = _token_list(candidate.chunk_text)

    bonus = 0.0
    if title_phrase == normalized_query:
        bonus += _EXACT_TITLE_MATCH_BONUS
    elif normalized_query in title_phrase:
        bonus += _TITLE_PHRASE_MATCH_BONUS

    if 2 <= len(query_token_list) <= 4 and _has_contiguous_token_overlap(
        query_token_list,
        title_token_list,
        min_length=len(query_token_list),
    ):
        bonus += _SHORT_TITLE_PHRASE_MATCH_BONUS

    if (
        _has_contiguous_token_overlap(query_token_list, title_token_list, min_length=3)
        or _has_contiguous_token_overlap(query_token_list, summary_token_list, min_length=3)
        or _has_contiguous_token_overlap(query_token_list, chunk_token_list, min_length=4)
    ):
        bonus += _STRONG_FIELD_PHRASE_MATCH_BONUS

    if _has_leading_query_phrase_in_field(
        query_token_list,
        title_token_list,
        min_length=3,
    ):
        bonus += _LEADING_TITLE_PHRASE_MATCH_BONUS

    if source_url_phrase and normalized_query in source_url_phrase:
        bonus += _SOURCE_URL_PHRASE_MATCH_BONUS

    if normalized_query in summary_phrase or normalized_query in chunk_phrase:
        bonus += _BODY_PHRASE_MATCH_BONUS

    if len(query_tokens) >= 2:
        body_tokens = summary_tokens | chunk_tokens
        searchable_tokens = title_tokens | body_tokens | source_url_tokens
        if query_tokens.issubset(searchable_tokens):
            bonus += _FULL_TOKEN_COVERAGE_BONUS
        bonus += _token_coverage_bonus(
            query_tokens,
            title_tokens,
            weight=_TITLE_TOKEN_COVERAGE_BONUS,
            min_coverage=0.35,
        )
        bonus += _token_coverage_bonus(
            query_tokens,
            body_tokens,
            weight=_BODY_TOKEN_COVERAGE_BONUS,
            min_coverage=0.6,
        )

    return min(bonus, _MAX_LEXICAL_RESCUE_BONUS)


def _source_aware_adjustments(candidate: _SearchCandidate) -> dict[str, float]:
    adjustments: dict[str, float] = {}
    source_url = (candidate.source_url or "").lower()
    tags = {tag.lower() for tag in candidate.tags}
    source_kind = str(_metadata_value(candidate, "source_kind") or "").lower()
    authority = str(_metadata_value(candidate, "authority") or "").lower()

    if candidate.source_type in _AUTHORITY_SOURCE_TYPES and any(
        pattern in source_url for pattern in _AUTHORITY_URL_PATTERNS
    ):
        adjustments["authority_url"] = _SOURCE_AUTHORITY_URL_BONUS
    if tags & _AUTHORITY_TAGS or authority in {"primary", "official", "governing"}:
        adjustments["authority_metadata"] = _SOURCE_AUTHORITY_METADATA_BONUS
    if tags & _OPERATIONAL_TAGS:
        adjustments["operational_tags"] = _SOURCE_OPERATIONAL_TAG_BONUS
    if candidate.source_type in _LOW_SIGNAL_SOURCE_TYPES or source_kind in _LOW_SIGNAL_SOURCE_TYPES:
        adjustments["low_signal_source"] = -_LOW_SIGNAL_SOURCE_PENALTY
    if tags & _LOW_SIGNAL_TAGS:
        adjustments["low_signal_tags"] = -_LOW_SIGNAL_TAG_PENALTY
    return adjustments


def _recency_adjustment(candidate: _SearchCandidate, *, now: datetime | None = None) -> float:
    effective_date = candidate.effective_date or candidate.created_at
    if effective_date.tzinfo is None:
        effective_date = effective_date.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    age_days = max((reference - effective_date.astimezone(timezone.utc)).total_seconds() / 86400, 0.0)
    freshness = 1 / (1 + (age_days / _INTENT_RECENCY_HALF_LIFE_DAYS))
    quality_weight = _EFFECTIVE_DATE_QUALITY_WEIGHT.get(candidate.effective_date_quality or "high", 1.0)
    return _INTENT_RECENCY_MAX_BONUS * quality_weight * freshness


def _score_candidate(
    candidate: _SearchCandidate,
    query: str,
    *,
    intent: QueryIntent,
    source_ranking_enabled: bool,
    relationship_graph_score: float | None = None,
    relationship_graph_weight: float = 1.0,
    retrieval_hint_score: float | None = None,
) -> tuple[float, dict[str, float]]:
    adjustments: dict[str, float] = {}
    curated_source_bonus = _CURATED_SOURCE_BONUS.get(candidate.source_type, 0.0)
    if curated_source_bonus:
        adjustments["curated_source"] = curated_source_bonus
    lexical_rescue_bonus = _lexical_rescue_bonus(query, candidate)
    if lexical_rescue_bonus:
        adjustments["lexical_rescue"] = lexical_rescue_bonus
    if _is_agent_self_note(candidate):
        adjustments["self_memory_note"] = -_SELF_MEMORY_NOTE_PENALTY
        if _looks_like_negative_self_memory(candidate):
            adjustments["negative_self_memory_note"] = -_NEGATIVE_SELF_MEMORY_NOTE_PENALTY
    if _derived_artifact_keys(candidate) and not intent.allow_derived_artifacts:
        adjustments["derived_artifact"] = -_DERIVED_ARTIFACT_PENALTY
    if intent.allow_recency:
        adjustments["intent_recency"] = _recency_adjustment(candidate)
    if source_ranking_enabled and intent.allow_strict_source_boosts:
        adjustments.update(_source_aware_adjustments(candidate))
        adjustments.update(_nist_source_role_adjustments(query, candidate))
    if relationship_graph_score:
        adjustments["relationship_graph"] = min(
            settings.retrieval_relationship_max_bonus,
            settings.retrieval_relationship_max_bonus * max(relationship_graph_score, 0.0) * relationship_graph_weight,
        )
    if retrieval_hint_score:
        adjustments["retrieval_hint"] = min(
            settings.retrieval_hint_ranking_max_bonus,
            settings.retrieval_hint_ranking_max_bonus * max(retrieval_hint_score, 0.0),
        )
    adjusted = candidate.score + sum(adjustments.values())
    return adjusted, adjustments


def _second_stage_candidate_limit(effective_candidate_limit: int) -> int:
    configured = max(settings.retrieval_second_stage_reranker_candidate_limit, 1)
    return min(configured, effective_candidate_limit)


def _build_disabled_reranker_trace(*, reason: str = "disabled") -> dict[str, Any]:
    return {
        "enabled": False,
        "provider": None,
        "status": reason,
        "candidate_limit": None,
        "candidate_count": 0,
        "latency_ms": None,
        "changed_top_k": False,
        "top_k_before": [],
        "top_k_after": [],
    }


def _apply_second_stage_reranker(
    *,
    query: str,
    limit: int,
    effective_candidate_limit: int,
    scored_candidates: list[tuple[float, dict[str, float], _SearchCandidate]],
) -> tuple[list[tuple[float, dict[str, float], _SearchCandidate]], dict[str, Any], dict[Any, dict[str, Any]]]:
    if not settings.retrieval_second_stage_reranker_enabled:
        return scored_candidates, _build_disabled_reranker_trace(), {}

    provider = settings.retrieval_second_stage_reranker_provider.strip()
    bounded_limit = _second_stage_candidate_limit(effective_candidate_limit)
    candidate_rows = scored_candidates[:bounded_limit]
    trace: dict[str, Any] = {
        "enabled": True,
        "provider": provider or None,
        "model": provider or None,
        "status": "disabled",
        "candidate_limit": bounded_limit,
        "candidate_count": len(candidate_rows),
        "timeout_ms": settings.retrieval_second_stage_reranker_timeout_ms,
        "latency_ms": None,
        "changed_top_k": False,
        "top_k_before": [str(candidate.item_id) for _, _, candidate in scored_candidates[:limit]],
        "top_k_after": [str(candidate.item_id) for _, _, candidate in scored_candidates[:limit]],
    }
    if not candidate_rows:
        trace["status"] = "empty_candidates"
        return scored_candidates, trace, {}

    try:
        reranker = _runtime_reranker_from_settings()
    except ValueError as exc:
        trace["status"] = "fallback_error"
        trace["error_class"] = exc.__class__.__name__
        logger.warning("Second-stage reranker disabled by invalid provider: %s", exc)
        return scored_candidates, trace, {}
    if reranker is None:
        trace["status"] = "missing_provider"
        return scored_candidates, trace, {}

    start = time.perf_counter()
    try:
        decisions = reranker.rerank(
            query=query,
            candidates=[candidate for _, _, candidate in candidate_rows],
        )
    except Exception as exc:  # pragma: no cover - exercised through monkeypatched tests
        trace["latency_ms"] = round((time.perf_counter() - start) * 1000, 3)
        trace["status"] = "fallback_error"
        trace["error_class"] = exc.__class__.__name__
        logger.warning("Second-stage reranker failed; falling back to baseline ranking", exc_info=True)
        return scored_candidates, trace, {}

    latency_ms = round((time.perf_counter() - start) * 1000, 3)
    trace["latency_ms"] = latency_ms
    if latency_ms > settings.retrieval_second_stage_reranker_timeout_ms:
        trace["status"] = "fallback_timeout"
        return scored_candidates, trace, {}

    decision_by_item = {decision.item_id: decision for decision in decisions}
    if not decision_by_item:
        trace["status"] = "no_decisions"
        return scored_candidates, trace, {}

    max_bonus = max(settings.retrieval_second_stage_reranker_max_bonus, 0.0)
    per_item_trace: dict[Any, dict[str, Any]] = {}
    reranked_rows: list[tuple[float, dict[str, float], _SearchCandidate]] = []
    for adjusted_score, adjustments, candidate in scored_candidates:
        decision = decision_by_item.get(candidate.item_id)
        if decision is None:
            reranked_rows.append((adjusted_score, adjustments, candidate))
            continue
        normalized_score = max(min(float(decision.score), 1.0), 0.0)
        bonus = max_bonus * normalized_score
        updated_adjustments = dict(adjustments)
        if bonus:
            updated_adjustments["second_stage_reranker"] = bonus
        per_item_trace[candidate.item_id] = {
            "score": round(normalized_score, 6),
            "bonus": round(bonus, 6),
            "reason": decision.reason,
            "provider": reranker.name,
        }
        reranked_rows.append((adjusted_score + bonus, updated_adjustments, candidate))

    reranked_rows.sort(key=lambda pair: pair[0], reverse=True)
    trace["status"] = "applied"
    trace["provider"] = reranker.name
    trace["model"] = reranker.name
    trace["top_k_after"] = [str(candidate.item_id) for _, _, candidate in reranked_rows[:limit]]
    trace["changed_top_k"] = trace["top_k_before"] != trace["top_k_after"]
    return reranked_rows, trace, per_item_trace


class SearchService:
    def __init__(self, db: AsyncSession, embedder: EmbeddingService, tenant_id: str = "default"):
        self.db = db
        self.embedder = embedder
        self.embedding_profile = getattr(embedder, "profile", resolve_embedding_profile())
        self.tenant_id = tenant_id
        self.last_ranking_trace: dict[str, Any] | None = None

    async def vector_search(
        self,
        query: str,
        limit: int = 10,
        source_type: str | None = None,
        retrieval_lens: str | None = None,
        item_ids: list | None = None,
        room_ids: list | None = None,
        scope_type: str | None = None,
        scope_key: str | None = None,
        tags: list[str] | None = None,
        tags_mode: str = "any",
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        min_score: float | None = None,
        query_vector: list[float] | None = None,
        exclude_private_memory_scopes: bool = False,
        candidate_limit: int | None = None,
        include_neighbor_chunks: bool = False,
        neighbor_chunk_window: int = 1,
        context_budget_chars: int | None = None,
        include_derived_artifacts: bool = False,
    ) -> list[SearchResult]:
        lens_profile = resolve_retrieval_lens(retrieval_lens)
        query_vec = query_vector or await self.embedder.embed_single(query)
        vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"
        effective_candidate_limit = _candidate_fetch_limit(limit, candidate_limit)
        embedding_plan = _embedding_search_plan(self.embedding_profile)

        # Hybrid: 0.7 * vector cosine similarity (halfvec HNSW) + 0.3 * ts_rank.
        # Deduplicate by item — best-scoring chunk per item only.
        # Pull a slightly wider candidate set so deterministic hygiene reranking can
        # demote stale self-chat memories without another DB round-trip or embedding call.
        # Use CAST() instead of :: to avoid conflict with SQLAlchemy :param syntax.
        sql = text(f"""
            WITH ranked AS (
                SELECT
                    e.item_id,
                    e.chunk_text,
                    e.chunk_index,
                    1 - (e.{embedding_plan.half_column} <=> CAST(:vec AS halfvec({embedding_plan.dimensions}))) AS vec_score,
                    COALESCE(
                        ts_rank(i.search_vector, plainto_tsquery('english', :query)),
                        0
                    ) AS text_score,
                    i.title,
                    i.summary,
                    i.source_type,
                    i.source_url,
                    i.tags,
                    i.created_at,
                    COALESCE(i.effective_date, i.created_at) AS effective_date,
                    i.effective_date_source,
                    i.effective_date_quality,
                    i.metadata AS item_metadata,
                    ROW_NUMBER() OVER (
                        PARTITION BY e.item_id
                        ORDER BY (
                            :vw * (1 - (e.{embedding_plan.half_column} <=> CAST(:vec AS halfvec({embedding_plan.dimensions})))) +
                            :tw * COALESCE(
                                ts_rank(i.search_vector, plainto_tsquery('english', :query)),
                                0
                            )
                        ) DESC
                    ) AS rn
                FROM {embedding_plan.table_name} e
                JOIN items i ON e.item_id = i.id
                WHERE i.status = 'ready'
                  AND i.deleted_at IS NULL
                  AND i.tenant_id = :tenant_id
                  {embedding_plan.profile_filter}
                  AND (CAST(:source_type AS varchar) IS NULL OR i.source_type = :source_type)
                  AND (CAST(:item_ids AS uuid[]) IS NULL OR e.item_id = ANY(CAST(:item_ids AS uuid[])))
                  AND (
                      CAST(:room_ids AS uuid[]) IS NULL
                      OR EXISTS (
                          SELECT 1
                          FROM room_memberships rm
                          WHERE rm.tenant_id = :tenant_id
                            AND rm.item_id = i.id
                            AND rm.room_id = ANY(CAST(:room_ids AS uuid[]))
                      )
                  )
                  AND (
                      CAST(:scope_type AS text) IS NULL
                      OR COALESCE(
                          i.metadata->'memory_entry'->'scope'->>'type',
                          CASE
                              WHEN CAST(:scope_type AS text) = 'tenant_shared' THEN 'tenant_shared'
                              ELSE NULL
                          END
                      ) = CAST(:scope_type AS text)
                  )
                  AND (
                      CAST(:scope_key AS text) IS NULL
                      OR i.metadata->'memory_entry'->'scope'->>'key' = CAST(:scope_key AS text)
                  )
                  AND (
                      CAST(:exclude_private_memory_scopes AS boolean) IS FALSE
                      OR i.metadata->'memory_entry' IS NULL
                      OR COALESCE(
                          i.metadata->'memory_entry'->'scope'->>'type',
                          'tenant_shared'
                      ) = 'tenant_shared'
                  )
                  AND (
                      CAST(:tags AS text[]) IS NULL
                      OR (CAST(:tags_mode AS text) = 'all' AND i.tags @> CAST(:tags AS text[]))
                      OR (CAST(:tags_mode AS text) = 'any' AND i.tags && CAST(:tags AS text[]))
                  )
                  AND (CAST(:date_from AS timestamptz) IS NULL OR COALESCE(i.effective_date, i.created_at) >= :date_from)
                  AND (CAST(:date_to   AS timestamptz) IS NULL OR COALESCE(i.effective_date, i.created_at) <= :date_to)
            )
            SELECT
                item_id, chunk_text, chunk_index,
                title, summary, source_type, source_url, tags, created_at,
                effective_date, effective_date_source, effective_date_quality, item_metadata,
                (:vw * vec_score + :tw * text_score) AS score
            FROM ranked
            WHERE rn = 1
            ORDER BY score DESC
            LIMIT :candidate_limit
        """)

        rows = (
            await self.db.execute(
                sql,
                {
                    "vec": vec_str,
                    "query": query,
                    "vw": _VECTOR_WEIGHT,
                    "tw": _TEXT_WEIGHT,
                    "tenant_id": self.tenant_id,
                    "source_type": source_type,
                    "item_ids": item_ids,
                    "room_ids": room_ids,
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "tags": tags,
                    "tags_mode": tags_mode,
                    "date_from": date_from,
                    "date_to": date_to,
                    "candidate_limit": effective_candidate_limit,
                    "exclude_private_memory_scopes": exclude_private_memory_scopes,
                    "embedding_profile_name": embedding_plan.profile_name,
                    "embedding_dimensions": embedding_plan.dimensions,
                },
            )
        ).fetchall()

        candidates = [
            _SearchCandidate(
                item_id=r.item_id,
                title=r.title,
                summary=r.summary,
                source_type=r.source_type,
                source_url=r.source_url,
                tags=r.tags or [],
                created_at=r.created_at,
                effective_date=getattr(r, "effective_date", None),
                effective_date_source=getattr(r, "effective_date_source", None),
                effective_date_quality=getattr(r, "effective_date_quality", None),
                chunk_text=r.chunk_text,
                chunk_index=r.chunk_index,
                score=float(r.score),
                item_metadata=r.item_metadata or {},
            )
            for r in rows
        ]
        derived_artifacts_requested = include_derived_artifacts or _tags_request_derived_artifacts(tags)
        query_intent = _apply_derived_artifact_policy(
            classify_query_intent(query),
            include_derived_artifacts=derived_artifacts_requested,
        )
        relationship_graph_scores: dict[Any, float] = {}
        relationship_graph_candidate_count = 0
        relationship_graph_expansion_enabled = (
            (bool(settings.retrieval_relationship_expansion_enabled) or lens_profile.graph_expansion_enabled)
            and query_intent.allow_graph_expansion
            and item_ids is None
            and bool(candidates)
        )
        if relationship_graph_expansion_enabled:
            graph_candidates, relationship_graph_scores = await self._relationship_graph_candidates(
                query=query,
                vec_str=vec_str,
                embedding_plan=embedding_plan,
                seed_candidates=candidates,
                source_type=source_type,
                room_ids=room_ids,
                scope_type=scope_type,
                scope_key=scope_key,
                tags=tags,
                tags_mode=tags_mode,
                date_from=date_from,
                date_to=date_to,
                exclude_private_memory_scopes=exclude_private_memory_scopes,
            )
            relationship_graph_candidate_count = len(graph_candidates)
            existing_item_ids = {candidate.item_id for candidate in candidates}
            candidates.extend(
                candidate
                for candidate in graph_candidates
                if candidate.item_id not in existing_item_ids
            )
        retrieval_hint_ranking_enabled = (
            bool(settings.retrieval_hint_ranking_enabled)
            and query_intent.allow_salience
        )
        hint_scores: dict[Any, float] = {}
        if retrieval_hint_ranking_enabled:
            hint_scores = await score_retrieval_hints_for_items(
                self.db,
                tenant_id=self.tenant_id,
                query=query,
                candidate_item_ids=[candidate.item_id for candidate in candidates],
                room_ids=room_ids,
                limit=effective_candidate_limit,
            )
        source_ranking_enabled = (
            bool(settings.retrieval_source_ranking_enabled)
            and query_intent.allow_strict_source_boosts
        )
        scored_candidates = [
            (
                *_score_candidate(
                    candidate,
                    query,
                    intent=query_intent,
                    source_ranking_enabled=source_ranking_enabled,
                    relationship_graph_score=relationship_graph_scores.get(candidate.item_id),
                    relationship_graph_weight=lens_profile.graph_signal_weight,
                    retrieval_hint_score=hint_scores.get(candidate.item_id),
                ),
                candidate,
            )
            for candidate in candidates
        ]
        reranked = sorted(
            scored_candidates,
            key=lambda pair: pair[0],
            reverse=True,
        )
        reranked, second_stage_trace, second_stage_item_trace = _apply_second_stage_reranker(
            query=query,
            limit=limit,
            effective_candidate_limit=effective_candidate_limit,
            scored_candidates=reranked,
        )

        results: list[SearchResult] = []
        ranking_trace_rows: list[dict[str, Any]] = []
        for adjusted_score, adjustments, candidate in reranked:
            reranker_item_trace = second_stage_item_trace.get(candidate.item_id)
            retrieved_scope_type, retrieved_scope_key, retrieved_scope_label = (
                _retrieved_scope_from_metadata(candidate.item_metadata)
            )
            source_item_id = _conversation_fact_source_item_id(candidate.item_metadata)
            source_span = _conversation_fact_source_span(candidate.item_metadata)
            artifact_provenance_type, artifact_provenance_label = _artifact_provenance(candidate)
            source_project = source_project_from_memory_metadata(candidate.item_metadata)
            artifact_citation = build_artifact_citation(
                candidate.item_metadata,
                source_url=candidate.source_url,
                original_artifact_url=f"/api/v1/items/{candidate.item_id}/artifact",
            )
            retrieval_provenance = build_retrieval_provenance(
                candidate.item_metadata,
                item_id=candidate.item_id,
                source_type=candidate.source_type,
                source_url=candidate.source_url,
                source_item_id=source_item_id,
                source_span=source_span,
                artifact_citation=artifact_citation,
            )
            ranking_trace_rows.append(
                {
                    "item_id": str(candidate.item_id),
                    "source_type": candidate.source_type,
                    "source_project": source_project,
                    "artifact_provenance_type": artifact_provenance_type,
                    "artifact_provenance_label": artifact_provenance_label,
                    "derived_artifact_keys": list(_derived_artifact_keys(candidate)),
                    "retrieved_scope_type": retrieved_scope_type,
                    "retrieved_scope_key": retrieved_scope_key,
                    "retrieved_scope_label": retrieved_scope_label,
                    "source_item_id": str(source_item_id) if source_item_id else None,
                    "source_span": source_span,
                    "candidate_modality": retrieval_provenance.modality if retrieval_provenance else None,
                    "candidate_source": retrieval_provenance.candidate_source if retrieval_provenance else None,
                    "support_level": retrieval_provenance.support_level if retrieval_provenance else None,
                    "candidate_provenance": (
                        retrieval_provenance.model_dump(mode="json", exclude_none=True)
                        if retrieval_provenance
                        else None
                    ),
                    "base_score": round(candidate.score, 6),
                    "effective_date_source": candidate.effective_date_source,
                    "effective_date_quality": candidate.effective_date_quality,
                    "adjustments": {
                        name: round(value, 6)
                        for name, value in adjustments.items()
                        if value
                    },
                    "source_ranking_contributors": sorted(
                        name
                        for name in adjustments
                        if name
                        in {
                            "authority_url",
                            "authority_metadata",
                            "operational_tags",
                            "low_signal_source",
                            "low_signal_tags",
                            "nist_source_role_match",
                            "nist_source_role_decoy",
                        }
                    ),
                    "source_publication_id": _candidate_nist_publication_id(candidate),
                    "source_role": _candidate_nist_source_role(candidate),
                    "query_source_role": _query_nist_source_role(query),
                    "retrieval_hint_score": (
                        round(hint_scores[candidate.item_id], 6)
                        if candidate.item_id in hint_scores
                        else None
                    ),
                    "relationship_graph_score": (
                        round(relationship_graph_scores[candidate.item_id], 6)
                        if candidate.item_id in relationship_graph_scores
                        else None
                    ),
                    "reranker_score": (
                        reranker_item_trace["score"] if reranker_item_trace else None
                    ),
                    "reranker_bonus": (
                        reranker_item_trace["bonus"] if reranker_item_trace else None
                    ),
                    "reranker_provider": (
                        reranker_item_trace["provider"] if reranker_item_trace else None
                    ),
                    "reranker_reason": (
                        reranker_item_trace["reason"] if reranker_item_trace else None
                    ),
                    "adjusted_score": round(adjusted_score, 6),
                }
            )
            if min_score is not None and adjusted_score < min_score:
                continue
            system_tags, semantic_tags = split_system_provenance_tags(candidate.tags)
            results.append(
                SearchResult(
                    item_id=candidate.item_id,
                    title=candidate.title,
                    summary=candidate.summary,
                    source_type=candidate.source_type,
                    source_url=candidate.source_url,
                    tags=candidate.tags,
                    system_tags=system_tags,
                    semantic_tags=semantic_tags,
                    source_project=source_project,
                    retrieved_scope_type=retrieved_scope_type,
                    retrieved_scope_key=retrieved_scope_key,
                    retrieved_scope_label=retrieved_scope_label,
                    source_item_id=source_item_id,
                    source_span=source_span,
                    created_at=candidate.created_at,
                    chunk_text=candidate.chunk_text,
                    chunk_index=candidate.chunk_index,
                    score=float(adjusted_score),
                    artifact_citation=artifact_citation,
                    retrieval_provenance=retrieval_provenance,
                )
            )
            if len(results) >= limit:
                break

        context_budget_truncated = False
        if include_neighbor_chunks:
            context_budget_truncated = await self._hydrate_neighbor_chunks(
                results,
                neighbor_chunk_window=neighbor_chunk_window,
                context_budget_chars=context_budget_chars,
            )

        hint_report = None
        if settings.retrieval_hint_report_enabled:
            hint_report = await report_retrieval_hint_candidates(
                self.db,
                tenant_id=self.tenant_id,
                query=query,
                current_results=results,
                room_ids=room_ids,
                limit=max(settings.retrieval_hint_report_limit, 1),
            )

        self.last_ranking_trace = {
            "ranking_features_version": 2,
            "query_intent": query_intent.name,
            "retrieval_lens": lens_profile.name,
            "retrieval_lens_profile": lens_profile.as_trace(),
            "source_ranking_enabled": source_ranking_enabled,
            "retrieval_hint_ranking_enabled": retrieval_hint_ranking_enabled,
            "relationship_graph_expansion_enabled": relationship_graph_expansion_enabled,
            "retrieval_hint_report": hint_report,
            "query_allows_derived_artifacts": query_intent.allow_derived_artifacts,
            "ranking_feature_flags": {
                "recency": query_intent.allow_recency,
                "salience": query_intent.allow_salience,
                "derived_artifacts": query_intent.allow_derived_artifacts,
                "derived_artifacts_explicit": derived_artifacts_requested,
                "graph_expansion": relationship_graph_expansion_enabled,
                "strict_source_boosts": source_ranking_enabled,
                "retrieval_hints": retrieval_hint_ranking_enabled,
                "second_stage_reranker": bool(second_stage_trace.get("enabled")),
                "retrieval_lens": lens_profile.name != "default",
            },
            "embedding_profile": {
                "profile_name": self.embedding_profile.profile_name,
                "provider": self.embedding_profile.provider,
                "model": self.embedding_profile.model,
                "dimensions": self.embedding_profile.dimensions,
                "storage": embedding_plan.table_name,
            },
            "second_stage_reranker": second_stage_trace,
            "display_limit": limit,
            "candidate_limit": effective_candidate_limit,
            "candidate_count": len(candidates),
            "relationship_graph_candidate_count": relationship_graph_candidate_count,
            "include_neighbor_chunks": include_neighbor_chunks,
            "neighbor_chunk_window": neighbor_chunk_window if include_neighbor_chunks else None,
            "context_budget_chars": context_budget_chars if include_neighbor_chunks else None,
            "context_budget_truncated": context_budget_truncated,
            "results": ranking_trace_rows[: max(limit, 1)],
        }
        return results

    async def _relationship_graph_candidates(
        self,
        *,
        query: str,
        vec_str: str,
        embedding_plan: _EmbeddingSearchPlan,
        seed_candidates: list[_SearchCandidate],
        source_type: str | None,
        room_ids: list | None,
        scope_type: str | None,
        scope_key: str | None,
        tags: list[str] | None,
        tags_mode: str,
        date_from: datetime | None,
        date_to: datetime | None,
        exclude_private_memory_scopes: bool,
    ) -> tuple[list[_SearchCandidate], dict[Any, float]]:
        seed_item_ids = [candidate.item_id for candidate in seed_candidates]
        if not seed_item_ids:
            return [], {}

        graph_limit = max(settings.retrieval_relationship_fanout_limit, 1) * len(seed_item_ids)
        rows = (
            await self.db.execute(
                text(f"""
                    WITH edges AS (
                        SELECT
                            ir.source_item_id AS seed_item_id,
                            ir.target_item_id AS related_item_id,
                            ir.confidence,
                            ir.relationship
                        FROM item_relationships ir
                        WHERE ir.source_item_id = ANY(CAST(:seed_item_ids AS uuid[]))
                          AND ir.confidence >= :min_confidence
                        UNION ALL
                        SELECT
                            ir.target_item_id AS seed_item_id,
                            ir.source_item_id AS related_item_id,
                            ir.confidence,
                            ir.relationship
                        FROM item_relationships ir
                        WHERE ir.target_item_id = ANY(CAST(:seed_item_ids AS uuid[]))
                          AND ir.confidence >= :min_confidence
                    ),
                    ranked AS (
                        SELECT
                            edges.seed_item_id,
                            edges.confidence,
                            edges.relationship,
                            e.item_id,
                            e.chunk_text,
                            e.chunk_index,
                            i.title,
                            i.summary,
                            i.source_type,
                            i.source_url,
                            i.tags,
                            i.created_at,
                            COALESCE(i.effective_date, i.created_at) AS effective_date,
                            i.effective_date_source,
                            i.effective_date_quality,
                            i.metadata AS item_metadata,
                            (
                                :vw * (1 - (e.{embedding_plan.half_column} <=> CAST(:vec AS halfvec({embedding_plan.dimensions})))) +
                                :tw * COALESCE(
                                    ts_rank(i.search_vector, plainto_tsquery('english', :query)),
                                    0
                                )
                            ) AS score,
                            ROW_NUMBER() OVER (
                                PARTITION BY edges.seed_item_id
                                ORDER BY edges.confidence DESC, (
                                    :vw * (1 - (e.{embedding_plan.half_column} <=> CAST(:vec AS halfvec({embedding_plan.dimensions})))) +
                                    :tw * COALESCE(
                                        ts_rank(i.search_vector, plainto_tsquery('english', :query)),
                                        0
                                    )
                                ) DESC
                            ) AS seed_rank,
                            ROW_NUMBER() OVER (
                                PARTITION BY e.item_id
                                ORDER BY edges.confidence DESC, (
                                    :vw * (1 - (e.{embedding_plan.half_column} <=> CAST(:vec AS halfvec({embedding_plan.dimensions})))) +
                                    :tw * COALESCE(
                                        ts_rank(i.search_vector, plainto_tsquery('english', :query)),
                                        0
                                    )
                                ) DESC
                            ) AS item_rank
                        FROM edges
                        JOIN items seed ON seed.id = edges.seed_item_id
                        JOIN items i ON i.id = edges.related_item_id
                        JOIN {embedding_plan.table_name} e ON e.item_id = i.id
                        WHERE seed.tenant_id = :tenant_id
                          AND seed.status = 'ready'
                          AND seed.deleted_at IS NULL
                          AND i.status = 'ready'
                          AND i.deleted_at IS NULL
                          AND i.tenant_id = :tenant_id
                          {embedding_plan.profile_filter}
                          AND i.id <> ALL(CAST(:seed_item_ids AS uuid[]))
                          AND (CAST(:source_type AS varchar) IS NULL OR i.source_type = :source_type)
                          AND (
                              CAST(:room_ids AS uuid[]) IS NULL
                              OR EXISTS (
                                  SELECT 1
                                  FROM room_memberships rm
                                  WHERE rm.tenant_id = :tenant_id
                                    AND rm.item_id = i.id
                                    AND rm.room_id = ANY(CAST(:room_ids AS uuid[]))
                              )
                          )
                          AND (
                              CAST(:scope_type AS text) IS NULL
                              OR COALESCE(
                                  i.metadata->'memory_entry'->'scope'->>'type',
                                  CASE
                                      WHEN CAST(:scope_type AS text) = 'tenant_shared' THEN 'tenant_shared'
                                      ELSE NULL
                                  END
                              ) = CAST(:scope_type AS text)
                          )
                          AND (
                              CAST(:scope_key AS text) IS NULL
                              OR i.metadata->'memory_entry'->'scope'->>'key' = CAST(:scope_key AS text)
                          )
                          AND (
                              CAST(:exclude_private_memory_scopes AS boolean) IS FALSE
                              OR i.metadata->'memory_entry' IS NULL
                              OR COALESCE(
                                  i.metadata->'memory_entry'->'scope'->>'type',
                                  'tenant_shared'
                              ) = 'tenant_shared'
                          )
                          AND (
                              CAST(:tags AS text[]) IS NULL
                              OR (CAST(:tags_mode AS text) = 'all' AND i.tags @> CAST(:tags AS text[]))
                              OR (CAST(:tags_mode AS text) = 'any' AND i.tags && CAST(:tags AS text[]))
                          )
                          AND (CAST(:date_from AS timestamptz) IS NULL OR COALESCE(i.effective_date, i.created_at) >= :date_from)
                          AND (CAST(:date_to AS timestamptz) IS NULL OR COALESCE(i.effective_date, i.created_at) <= :date_to)
                    )
                    SELECT *
                    FROM ranked
                    WHERE seed_rank <= :fanout_limit
                      AND item_rank = 1
                    ORDER BY confidence DESC, score DESC
                    LIMIT :graph_limit
                """),
                {
                    "seed_item_ids": seed_item_ids,
                    "min_confidence": settings.retrieval_relationship_min_confidence,
                    "fanout_limit": max(settings.retrieval_relationship_fanout_limit, 1),
                    "graph_limit": graph_limit,
                    "vec": vec_str,
                    "query": query,
                    "vw": _VECTOR_WEIGHT,
                    "tw": _TEXT_WEIGHT,
                    "tenant_id": self.tenant_id,
                    "source_type": source_type,
                    "room_ids": room_ids,
                    "scope_type": scope_type,
                    "scope_key": scope_key,
                    "tags": tags,
                    "tags_mode": tags_mode,
                    "date_from": date_from,
                    "date_to": date_to,
                    "exclude_private_memory_scopes": exclude_private_memory_scopes,
                    "embedding_profile_name": embedding_plan.profile_name,
                    "embedding_dimensions": embedding_plan.dimensions,
                },
            )
        ).fetchall()

        graph_scores: dict[Any, float] = {}
        graph_candidates: list[_SearchCandidate] = []
        for row in rows:
            graph_scores[row.item_id] = max(
                graph_scores.get(row.item_id, 0.0),
                float(row.confidence) * settings.retrieval_relationship_hop_decay,
            )
            graph_candidates.append(
                _SearchCandidate(
                    item_id=row.item_id,
                    title=row.title,
                    summary=row.summary,
                    source_type=row.source_type,
                    source_url=row.source_url,
                    tags=row.tags or [],
                    created_at=row.created_at,
                    effective_date=getattr(row, "effective_date", None),
                    effective_date_source=getattr(row, "effective_date_source", None),
                    effective_date_quality=getattr(row, "effective_date_quality", None),
                    chunk_text=row.chunk_text,
                    chunk_index=row.chunk_index,
                    score=float(row.score),
                    item_metadata=row.item_metadata or {},
                )
            )
        return graph_candidates, graph_scores

    async def _hydrate_neighbor_chunks(
        self,
        results: list[SearchResult],
        *,
        neighbor_chunk_window: int,
        context_budget_chars: int | None,
    ) -> bool:
        if not results:
            return False

        window = max(1, min(neighbor_chunk_window, 5))
        item_ids = [result.item_id for result in results]
        min_chunk = min(result.chunk_index for result in results) - window
        max_chunk = max(result.chunk_index for result in results) + window
        embedding_plan = _embedding_search_plan(self.embedding_profile)
        rows = (
            await self.db.execute(
                text(f"""
                    SELECT e.item_id, e.chunk_index, e.chunk_text
                    FROM {embedding_plan.table_name} e
                    JOIN items i ON i.id = e.item_id
                    WHERE i.tenant_id = :tenant_id
                      AND e.item_id = ANY(CAST(:item_ids AS uuid[]))
                      AND e.chunk_index BETWEEN :min_chunk AND :max_chunk
                      {embedding_plan.profile_filter}
                    ORDER BY e.item_id, e.chunk_index
                """),
                {
                    "tenant_id": self.tenant_id,
                    "item_ids": item_ids,
                    "min_chunk": min_chunk,
                    "max_chunk": max_chunk,
                    "embedding_profile_name": embedding_plan.profile_name,
                    "embedding_dimensions": embedding_plan.dimensions,
                },
            )
        ).fetchall()

        chunks_by_item: dict[Any, list[Any]] = {}
        for row in rows:
            chunks_by_item.setdefault(row.item_id, []).append(row)

        remaining_budget = context_budget_chars
        truncated = False
        for result in results:
            lower = result.chunk_index - window
            upper = result.chunk_index + window
            context_chunks: list[SearchContextChunk] = []
            for row in chunks_by_item.get(result.item_id, []):
                if row.chunk_index < lower or row.chunk_index > upper:
                    continue
                chunk_text = row.chunk_text or ""
                if remaining_budget is not None:
                    if len(chunk_text) > remaining_budget:
                        truncated = True
                        continue
                    remaining_budget -= len(chunk_text)
                if row.chunk_index < result.chunk_index:
                    relation = "previous"
                elif row.chunk_index > result.chunk_index:
                    relation = "next"
                else:
                    relation = "matched"
                context_chunks.append(
                    SearchContextChunk(
                        chunk_index=row.chunk_index,
                        chunk_text=chunk_text,
                        relation=relation,
                    )
                )
            result.context_chunks = context_chunks
        return truncated
