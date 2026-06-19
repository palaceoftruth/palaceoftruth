from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievalLensProfile:
    name: str
    description: str
    graph_expansion_enabled: bool = False
    graph_signal_weight: float = 1.0
    trace_label: str = "default"

    def as_trace(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "graph_expansion_enabled": self.graph_expansion_enabled,
            "graph_signal_weight": self.graph_signal_weight,
            "trace_label": self.trace_label,
        }


RETRIEVAL_LENS_PROFILES: dict[str, RetrievalLensProfile] = {
    "default": RetrievalLensProfile(
        name="default",
        description="Default retrieval behavior with configured ranking features only.",
    ),
    "codex": RetrievalLensProfile(
        name="codex",
        description="Prioritize explainable project and run-context graph expansion for Codex recall.",
        graph_expansion_enabled=True,
        graph_signal_weight=1.15,
        trace_label="codex-context",
    ),
    "engineering": RetrievalLensProfile(
        name="engineering",
        description="Bias toward implementation-adjacent relationships while keeping source ranking explainable.",
        graph_expansion_enabled=True,
        graph_signal_weight=1.1,
        trace_label="engineering-context",
    ),
    "ops": RetrievalLensProfile(
        name="ops",
        description="Favor operational runbook and incident-neighbor graph signals for troubleshooting recall.",
        graph_expansion_enabled=True,
        graph_signal_weight=1.2,
        trace_label="ops-context",
    ),
    "research": RetrievalLensProfile(
        name="research",
        description="Use graph expansion as an advisory exploration signal for research-oriented recall.",
        graph_expansion_enabled=True,
        graph_signal_weight=1.05,
        trace_label="research-context",
    ),
}


def resolve_retrieval_lens(name: str | None) -> RetrievalLensProfile:
    normalized = (name or "default").strip().casefold()
    if not normalized:
        normalized = "default"
    try:
        return RETRIEVAL_LENS_PROFILES[normalized]
    except KeyError as exc:
        known = ", ".join(sorted(RETRIEVAL_LENS_PROFILES))
        raise ValueError(f"unknown retrieval lens {name!r}; expected one of: {known}") from exc


def validate_retrieval_lens_name(name: str | None) -> str | None:
    if name is None:
        return None
    resolved = resolve_retrieval_lens(name)
    return resolved.name
