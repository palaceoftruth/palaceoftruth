from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.services.palace import _infer_room_name, _infer_wing_name, _route_room_score

TOKEN_RE = re.compile(r"[a-z0-9]+")
SECRETISH_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|secret|token|password)\s*[:=]\s*(?:Bearer\s+)?\S+"
)
RAW_CONTENT_KEYS = {"body", "raw_body", "memory_body", "raw_memory_body", "raw_content"}
REWARD_COMPONENTS = (
    "scope_correctness",
    "source_coverage",
    "freshness",
    "stale_memory_demotion",
    "citation_traceability",
    "abstention",
    "privacy_safe_output",
)


@dataclass(frozen=True)
class EvalItem:
    id: str
    title: str
    summary: str
    body: str
    source_type: str
    tags: list[str]
    categories: list[str]
    sync_relative_path: str


def _tokens(value: str) -> set[str]:
    return set(TOKEN_RE.findall(value.lower()))


def _item_proxy(item: EvalItem) -> SimpleNamespace:
    return SimpleNamespace(
        metadata_={"sync_relative_path": item.sync_relative_path},
        title=item.title,
        tags=item.tags,
        categories=item.categories,
    )


def _flat_score(query: str, item: EvalItem) -> float:
    query_tokens = _tokens(query)
    # Flat baseline intentionally ignores room/path context and focuses on chunk text.
    item_tokens = _tokens(item.body)
    overlap = len(query_tokens & item_tokens)
    return float(overlap)


def _palace_item_score(query: str, item: EvalItem) -> float:
    query_tokens = _tokens(query)
    searchable = " ".join([item.title, item.summary, item.body])
    item_tokens = _tokens(searchable)
    overlap = len(query_tokens & item_tokens)
    title_overlap = len(query_tokens & _tokens(item.title))
    return overlap + (title_overlap * 0.5)


def _room_summary(items: list[EvalItem]) -> str:
    parts: list[str] = []
    for item in items:
        parts.append(item.title)
        if item.summary:
            parts.append(item.summary)
        if item.tags:
            parts.append(" ".join(item.tags))
    return " ".join(parts)


def _build_rooms(items: list[EvalItem]) -> dict[tuple[str, str], list[EvalItem]]:
    rooms: dict[tuple[str, str], list[EvalItem]] = {}
    for item in items:
        proxy = _item_proxy(item)
        wing_name = _infer_wing_name(proxy)
        room_name = _infer_room_name(proxy)
        rooms.setdefault((wing_name, room_name), []).append(item)
    return rooms


def _top_item(query: str, items: list[EvalItem], *, palace_mode: bool = False) -> EvalItem:
    score_fn = _palace_item_score if palace_mode else _flat_score
    return max(items, key=lambda item: (score_fn(query, item), item.id))


def load_eval_fixture(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def load_eval_baseline(path: str | Path) -> dict[str, float]:
    return json.loads(Path(path).read_text())


def load_reward_eval_fixture(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text())
    validate_reward_fixture(payload, path=Path(path))
    return payload


def evaluate_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    items = [
        EvalItem(
            id=row["id"],
            title=row["title"],
            summary=row.get("summary", ""),
            body=row["body"],
            source_type=row.get("source_type", "note"),
            tags=row.get("tags", []),
            categories=row.get("categories", []),
            sync_relative_path=row["sync_relative_path"],
        )
        for row in payload["items"]
    ]
    item_ids = {item.id for item in items}
    rooms = _build_rooms(items)

    flat_hits = 0
    palace_hits = 0
    route_hits = 0
    route_total = 0
    query_reports: list[dict[str, Any]] = []

    for query_row in payload["queries"]:
        query = query_row["query"]
        expected_item_id = query_row["expected_item_id"]
        if expected_item_id not in item_ids:
            raise ValueError(f"Unknown expected_item_id in Palace eval fixture: {expected_item_id}")
        expected_wing = query_row.get("expected_wing")
        expected_room = query_row.get("expected_room")

        flat_top = _top_item(query, items)
        flat_hit = flat_top.id == expected_item_id
        flat_hits += int(flat_hit)

        selected_room = max(
            rooms.items(),
            key=lambda entry: (
                _route_room_score(query, entry[0][1], entry[0][0], _room_summary(entry[1])),
                entry[0],
            ),
        )
        palace_top = _top_item(query, selected_room[1], palace_mode=True)
        palace_hit = palace_top.id == expected_item_id
        palace_hits += int(palace_hit)

        routed_wing, routed_room = selected_room[0]
        route_hit = True
        if expected_wing is not None:
            route_hit = route_hit and routed_wing == expected_wing
        if expected_room is not None:
            route_hit = route_hit and routed_room == expected_room
        if expected_wing is not None or expected_room is not None:
            route_total += 1
            route_hits += int(route_hit)

        query_report = {
            "query": query,
            "expected_item_id": expected_item_id,
            "flat_top_item_id": flat_top.id,
            "flat_hit": flat_hit,
            "expected_wing": expected_wing,
            "expected_room": expected_room,
            "palace_wing": routed_wing,
            "palace_room": routed_room,
            "palace_route_hit": route_hit,
            "palace_top_item_id": palace_top.id,
            "palace_hit": palace_hit,
        }
        query_reports.append(query_report)

    total = len(payload["queries"])
    flat_accuracy = flat_hits / total if total else 0.0
    palace_accuracy = palace_hits / total if total else 0.0
    route_accuracy = route_hits / route_total if route_total else None

    return {
        "flat": {
            "hits": flat_hits,
            "total": total,
            "accuracy": flat_accuracy,
        },
        "palace": {
            "hits": palace_hits,
            "total": total,
            "accuracy": palace_accuracy,
        },
        "routing": {
            "hits": route_hits,
            "total": route_total,
            "accuracy": route_accuracy,
        },
        "delta": palace_accuracy - flat_accuracy,
        "queries": query_reports,
    }


def validate_reward_fixture(payload: dict[str, Any], *, path: Path | None = None) -> None:
    location = str(path) if path else "Palace reward eval fixture"
    if payload.get("schema_version") != 1:
        raise ValueError(f"{location}: unsupported schema_version")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError(f"{location}: cases must be a non-empty list")
    _assert_privacy_safe_payload(payload, location)

    for index, case in enumerate(cases):
        case_location = f"{location}:cases[{index}]"
        if not isinstance(case, dict):
            raise ValueError(f"{case_location}: case must be an object")
        if not str(case.get("id") or "").strip():
            raise ValueError(f"{case_location}: missing id")
        if not str(case.get("query") or "").strip():
            raise ValueError(f"{case_location}: missing query")
        expected_scope = case.get("expected_scope")
        if expected_scope is not None and not _scope_label(expected_scope):
            raise ValueError(f"{case_location}: expected_scope must include type and key")
        candidate = case.get("candidate")
        if not isinstance(candidate, dict):
            raise ValueError(f"{case_location}: candidate must be an object")
        if "answer" in candidate:
            raise ValueError(f"{case_location}: candidate.answer is not allowed; use answer_excerpt")
        judge = candidate.get("cached_judge")
        if judge is not None:
            if not isinstance(judge, dict):
                raise ValueError(f"{case_location}: cached_judge must be an object")
            if "score" not in judge or not isinstance(judge["score"], int | float):
                raise ValueError(f"{case_location}: cached_judge.score must be numeric")
            if not _list_text(judge.get("source_ids")):
                raise ValueError(f"{case_location}: cached_judge.source_ids must be non-empty")
            if not str(judge.get("cache_key") or "").strip():
                raise ValueError(f"{case_location}: cached_judge.cache_key is required")


def evaluate_reward_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    validate_reward_fixture(payload)
    cases = [_evaluate_reward_case(case) for case in payload["cases"]]
    component_scores = {
        component: _mean([case["components"][component]["score"] for case in cases])
        for component in REWARD_COMPONENTS
    }
    cached_judge_cases = [
        case for case in cases if case.get("cached_judge_score") is not None
    ]
    return {
        "schema_version": 1,
        "fixture_id": payload.get("fixture_id"),
        "description": payload.get("description"),
        "artifact_metadata": payload.get("artifact_metadata"),
        "summary": {
            "case_count": len(cases),
            "average_deterministic_reward": _mean(
                [case["deterministic_reward"] for case in cases]
            ),
            "average_total_reward": _mean([case["total_reward"] for case in cases]),
            "component_scores": component_scores,
            "cached_judge_case_count": len(cached_judge_cases),
            "privacy_safe": all(
                case["components"]["privacy_safe_output"]["score"] == 1.0
                for case in cases
            ),
        },
        "cases": cases,
    }


def _evaluate_reward_case(case: dict[str, Any]) -> dict[str, Any]:
    candidate = case["candidate"]
    retrieved_ids = _list_text(candidate.get("retrieved_source_ids"))
    cited_ids = _list_text(candidate.get("cited_source_ids"))
    answer_excerpt = str(candidate.get("answer_excerpt") or "")
    required_ids = _list_text(case.get("required_source_ids"))
    stale_ids = set(_list_text(case.get("stale_source_ids")))
    fresh_ids = set(_list_text(case.get("fresh_source_ids")))
    support_required = bool(case.get("support_required", True))
    abstained = bool(candidate.get("abstained", False))

    components = {
        "scope_correctness": _component(
            float(_scope_label(candidate.get("scope")) == _scope_label(case.get("expected_scope"))),
            "candidate scope matches expected scope",
        ),
        "source_coverage": _component(
            _coverage(required_ids, retrieved_ids + cited_ids),
            "required source ids are present in retrieval or citation trace",
        ),
        "freshness": _component(
            _freshness_score(retrieved_ids, stale_ids, fresh_ids),
            "fresh source ids appear without stale-only support",
        ),
        "stale_memory_demotion": _component(
            _stale_demotion_score(retrieved_ids, stale_ids, fresh_ids),
            "stale source ids are absent or ranked behind fresh support",
        ),
        "citation_traceability": _component(
            _citation_traceability_score(required_ids, retrieved_ids, cited_ids),
            "cited source ids are retrieved and cover required support",
        ),
        "abstention": _component(
            float((support_required and not abstained) or (not support_required and abstained)),
            "abstention state matches support availability",
        ),
        "privacy_safe_output": _component(
            _privacy_score(answer_excerpt, _list_text(case.get("forbidden_output_terms"))),
            "answer excerpt contains no forbidden terms or secret-like values",
        ),
    }
    deterministic_reward = _mean(
        [component["score"] for component in components.values()]
    )
    judge = candidate.get("cached_judge")
    cached_judge_score = None
    total_reward = deterministic_reward
    if isinstance(judge, dict):
        cached_judge_score = max(0.0, min(1.0, float(judge["score"])))
        total_reward = round((deterministic_reward * 0.8) + (cached_judge_score * 0.2), 4)

    return {
        "id": case["id"],
        "query": case["query"],
        "components": components,
        "deterministic_reward": deterministic_reward,
        "cached_judge_score": cached_judge_score,
        "total_reward": total_reward,
        "retrieved_source_ids": retrieved_ids,
        "cited_source_ids": cited_ids,
        "abstained": abstained,
    }


def _assert_privacy_safe_payload(value: Any, location: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in RAW_CONTENT_KEYS:
                raise ValueError(f"{location}: raw memory body field is not allowed")
            _assert_privacy_safe_payload(item, f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_privacy_safe_payload(item, f"{location}[{index}]")
        return
    if isinstance(value, str) and SECRETISH_RE.search(value):
        raise ValueError(f"{location}: secret-like value is not allowed")


def _list_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _scope_label(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    scope_type = str(value.get("type") or "").strip()
    scope_key = str(value.get("key") or "").strip()
    if not scope_type or not scope_key:
        return ""
    return f"{scope_type}/{scope_key}"


def _coverage(required_ids: list[str], observed_ids: list[str]) -> float:
    if not required_ids:
        return 1.0
    observed = set(observed_ids)
    return round(len(set(required_ids) & observed) / len(set(required_ids)), 4)


def _freshness_score(
    retrieved_ids: list[str],
    stale_ids: set[str],
    fresh_ids: set[str],
) -> float:
    if not stale_ids and not fresh_ids:
        return 1.0
    retrieved = set(retrieved_ids)
    if retrieved & fresh_ids:
        return 1.0
    if retrieved & stale_ids:
        return 0.0
    return 0.5


def _stale_demotion_score(
    retrieved_ids: list[str],
    stale_ids: set[str],
    fresh_ids: set[str],
) -> float:
    stale_ranks = [retrieved_ids.index(item_id) for item_id in stale_ids if item_id in retrieved_ids]
    if not stale_ranks:
        return 1.0
    fresh_ranks = [retrieved_ids.index(item_id) for item_id in fresh_ids if item_id in retrieved_ids]
    if fresh_ranks and min(fresh_ranks) < min(stale_ranks):
        return 1.0
    return 0.0


def _citation_traceability_score(
    required_ids: list[str],
    retrieved_ids: list[str],
    cited_ids: list[str],
) -> float:
    if not cited_ids:
        return 0.0 if required_ids else 1.0
    if not set(cited_ids).issubset(set(retrieved_ids)):
        return 0.0
    return _coverage(required_ids, cited_ids)


def _privacy_score(answer_excerpt: str, forbidden_terms: list[str]) -> float:
    if SECRETISH_RE.search(answer_excerpt):
        return 0.0
    answer_lower = answer_excerpt.lower()
    if any(term.lower() in answer_lower for term in forbidden_terms):
        return 0.0
    return 1.0


def _component(score: float, rationale: str) -> dict[str, Any]:
    return {"score": round(max(0.0, min(1.0, score)), 4), "rationale": rationale}


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)
