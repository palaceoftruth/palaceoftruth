#!/usr/bin/env python3
"""Smoke the sanitized Source-Backed Wakeup for Agent Teams fixture.

The demo is intentionally offline and non-mutating. It loads sanitized fixture
data, validates the public source-trust states, and prints the three operator
blocks a fresh agent should see before choosing a safe next action.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEMO_TITLE = "Source-Backed Wakeup for Agent Teams"
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "source_backed_wakeup_demo.json"
REQUIRED_STATES = {"source_backed", "generated_unpromoted"}
WARNING_STATES = {"stale_source", "source_missing"}
OPTIONAL_STATES = {"policy_limited"}
ALLOWED_SOURCE_HOST_SUFFIXES = (".test",)
FORBIDDEN_TEXT_MARKERS = (
    "api_key",
    "apikey",
    "authorization:",
    "bearer ",
    "password",
    "private_key",
    "production transcript",
    "secret",
    "token",
)


class DemoValidationError(ValueError):
    """Raised when the sanitized fixture cannot prove the wakeup story."""


def load_fixture(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as exc:
        raise DemoValidationError(f"could not read fixture {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DemoValidationError(f"fixture is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise DemoValidationError("fixture root must be a JSON object")
    return payload


def _entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries = payload.get("memory_entries")
    if not isinstance(entries, list) or not entries:
        raise DemoValidationError("fixture must include at least one memory entry")
    if not all(isinstance(entry, dict) for entry in entries):
        raise DemoValidationError("all memory entries must be objects")
    return entries


def _entry_state(entry: dict[str, Any]) -> str:
    trust = entry.get("source_trust")
    if not isinstance(trust, dict):
        raise DemoValidationError(f"entry {entry.get('id', '<unknown>')} is missing source_trust")
    state = trust.get("state")
    if not isinstance(state, str) or not state:
        raise DemoValidationError(f"entry {entry.get('id', '<unknown>')} has invalid source_trust.state")
    return state


def validate_privacy(payload: dict[str, Any]) -> list[str]:
    compact = json.dumps(payload, sort_keys=True).lower()
    leaked_markers = [marker for marker in FORBIDDEN_TEXT_MARKERS if marker in compact]
    if leaked_markers:
        raise DemoValidationError(f"fixture contains forbidden privacy marker(s): {', '.join(leaked_markers)}")

    contract = payload.get("privacy_contract")
    if not isinstance(contract, dict) or contract.get("sanitized_fixture_data_only") is not True:
        raise DemoValidationError("privacy_contract.sanitized_fixture_data_only must be true")
    if contract.get("raw_production_content") is not False:
        raise DemoValidationError("privacy_contract.raw_production_content must be false")

    checked_urls: list[str] = []
    for entry in _entries(payload):
        trust = entry.get("source_trust") if isinstance(entry.get("source_trust"), dict) else {}
        source_url = trust.get("source_url")
        if not isinstance(source_url, str) or not source_url:
            continue
        checked_urls.append(source_url)
        host = source_url.split("/", 3)[2] if "://" in source_url else ""
        if not host.endswith(ALLOWED_SOURCE_HOST_SUFFIXES):
            raise DemoValidationError(f"source_url must use sanitized .test host: {source_url}")
    return checked_urls


def validate_demo_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("demo_title") != DEMO_TITLE:
        raise DemoValidationError(f"demo_title must be {DEMO_TITLE!r}")

    entries = _entries(payload)
    states = {_entry_state(entry) for entry in entries}
    missing_required = sorted(REQUIRED_STATES - states)
    if missing_required:
        raise DemoValidationError(f"fixture missing required state(s): {', '.join(missing_required)}")
    if not states.intersection(WARNING_STATES):
        raise DemoValidationError("fixture must include stale_source or source_missing warning state")

    safe_next_action = payload.get("safe_next_action")
    if not isinstance(safe_next_action, str) or not safe_next_action.strip():
        raise DemoValidationError("fixture must include a non-empty safe_next_action")

    checked_urls = validate_privacy(payload)
    return {
        "states": sorted(states),
        "warning_states": sorted(states.intersection(WARNING_STATES)),
        "optional_states": sorted(states.intersection(OPTIONAL_STATES)),
        "checked_source_urls": checked_urls,
        "entry_count": len(entries),
    }


def render_demo(payload: dict[str, Any], *, query: str | None = None) -> str:
    validation = validate_demo_fixture(payload)
    entries = _entries(payload)
    selected = [entry for entry in entries if _entry_state(entry) in {"source_backed", "generated_unpromoted"}]
    warnings = [entry for entry in entries if _entry_state(entry) in WARNING_STATES.union(OPTIONAL_STATES)]

    lines = [
        f"# {DEMO_TITLE}",
        "",
        f"Demo query: {query or payload.get('query', 'What should this agent trust before acting?')}",
        f"Local tenant: {payload['tenant']['id']}",
        "",
        "## Context Palace selected",
    ]
    for entry in selected:
        trust = entry["source_trust"]
        lines.append(f"- {entry['title']} [{trust['state']}]: {entry['summary']}")
        lines.append(f"  Safe use: {entry['safe_use']}")

    lines.extend(["", "## Trust warnings Palace found"])
    for entry in warnings:
        trust = entry["source_trust"]
        warning = trust.get("warning") or trust.get("stale_reason") or "review_required"
        lines.append(f"- {entry['title']} [{trust['state']}]: {warning}")
        lines.append(f"  Safe use: {entry['safe_use']}")

    lines.extend(
        [
            "",
            "## Safe next action",
            payload["safe_next_action"],
            "",
            "## Fixture scan",
            f"- Entries checked: {validation['entry_count']}",
            f"- States found: {', '.join(validation['states'])}",
            f"- Warning states found: {', '.join(validation['warning_states'])}",
            f"- Privacy check: passed with {len(validation['checked_source_urls'])} sanitized source URL(s)",
        ]
    )
    if validation["optional_states"]:
        lines.append(f"- Optional scoped/policy state: {', '.join(validation['optional_states'])}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--query", default=None)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = load_fixture(args.fixture)
        validation = validate_demo_fixture(payload)
        if args.format == "json":
            print(json.dumps({"status": "ok", "validation": validation, "fixture": payload}, indent=2, sort_keys=True))
        else:
            print(render_demo(payload, query=args.query))
    except DemoValidationError as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
