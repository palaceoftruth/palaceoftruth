from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


CodexMemorySecretKind = Literal[
    "api_key",
    "bearer_token",
    "password",
    "private_key",
    "secret",
    "token",
]
CodexMemoryPrivacySeverity = Literal["none", "low", "medium", "high", "critical"]

_SEVERITY_RANK: dict[CodexMemoryPrivacySeverity, int] = {
    "none": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_SECRET_LABEL_RE = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth[_-]?token|bearer|client[_-]?secret|"
    r"password|passwd|private[_-]?key|refresh[_-]?token|secret|token)",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(
    r"(?P<prefix>\bBearer\s+)(?P<value>[A-Za-z0-9._~+/=-]{20,})",
    re.IGNORECASE,
)
_ASSIGNMENT_RE = re.compile(
    r"(?P<label>\b[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"client[_-]?secret|password|passwd|private[_-]?key|refresh[_-]?token|secret|token)"
    r"[A-Za-z0-9_.-]*\b)"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"'`,;]{6,})"
    r"(?P=quote)",
    re.IGNORECASE,
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?P<value>"
    r"github_pat_[A-Za-z0-9_]{22,}|"
    r"gh[pousr]_[A-Za-z0-9_]{30,}|"
    r"sk-(?:live|test|proj)-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{20,}|"
    r"AKIA[0-9A-Z]{16}|"
    r"ASIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_-]{35}|"
    r"(?:rk|sk)_(?:live|test)_[0-9A-Za-z]{20,}"
    r")"
)


@dataclass(frozen=True)
class CodexMemorySecretFinding:
    kind: CodexMemorySecretKind
    severity: CodexMemoryPrivacySeverity
    start: int
    end: int
    line: int
    column: int
    pattern: str


@dataclass(frozen=True)
class CodexMemoryPrivacyScan:
    severity: CodexMemoryPrivacySeverity
    findings: list[CodexMemorySecretFinding] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)


@dataclass(frozen=True)
class CodexMemoryPrivacyWarning:
    source_file: str
    line_number: int
    code: str
    detail: str


def scan_codex_memory_privacy(text: str) -> CodexMemoryPrivacyScan:
    findings = _dedupe_findings(
        [
            *_private_key_findings(text),
            *_bearer_token_findings(text),
            *_assignment_findings(text),
            *_known_token_findings(text),
        ]
    )
    return CodexMemoryPrivacyScan(
        severity=classify_codex_memory_privacy_severity(findings),
        findings=findings,
    )


def redact_codex_memory_preview(text: str, *, placeholder: str = "[redacted-secret]") -> str:
    findings = scan_codex_memory_privacy(text).findings
    redacted = text
    for finding in sorted(findings, key=lambda item: item.start, reverse=True):
        redacted = f"{redacted[: finding.start]}{placeholder}{redacted[finding.end :]}"
    return redacted


def classify_codex_memory_privacy_severity(
    findings: list[CodexMemorySecretFinding],
) -> CodexMemoryPrivacySeverity:
    severity: CodexMemoryPrivacySeverity = "none"
    for finding in findings:
        if _SEVERITY_RANK[finding.severity] > _SEVERITY_RANK[severity]:
            severity = finding.severity
    return severity


def has_codex_memory_secret_risk(text: str) -> bool:
    return scan_codex_memory_privacy(text).has_findings


def detect_secret_warnings(
    text: str,
    *,
    source_file: str,
    line_number: int = 1,
) -> list[CodexMemoryPrivacyWarning]:
    """Return redacted warnings for likely secrets without exposing values."""
    scan = scan_codex_memory_privacy(text)
    warnings: list[CodexMemoryPrivacyWarning] = []
    for finding in scan.findings:
        redacted = redact_codex_memory_preview(text, placeholder="<redacted-secret>")
        warnings.append(
            CodexMemoryPrivacyWarning(
                source_file=source_file,
                line_number=line_number + finding.line - 1,
                code="potential_secret",
                detail=(
                    f"Potential {finding.kind} detected by {finding.pattern}; "
                    f"preview={redacted[:240]}"
                ),
            )
        )
    return warnings


def _private_key_findings(text: str) -> list[CodexMemorySecretFinding]:
    return [
        _finding(
            text,
            kind="private_key",
            severity="critical",
            start=match.start(),
            end=match.end(),
            pattern="private_key_block",
        )
        for match in _PRIVATE_KEY_RE.finditer(text)
    ]


def _bearer_token_findings(text: str) -> list[CodexMemorySecretFinding]:
    return [
        _finding(
            text,
            kind="bearer_token",
            severity="critical",
            start=match.start("value"),
            end=match.end("value"),
            pattern="bearer_authorization",
        )
        for match in _BEARER_RE.finditer(text)
    ]


def _assignment_findings(text: str) -> list[CodexMemorySecretFinding]:
    findings: list[CodexMemorySecretFinding] = []
    for match in _ASSIGNMENT_RE.finditer(text):
        value = match.group("value")
        if _looks_like_placeholder(value):
            continue
        kind = _kind_from_label(match.group("label"))
        findings.append(
            _finding(
                text,
                kind=kind,
                severity=_assignment_severity(kind, value),
                start=match.start("value"),
                end=match.end("value"),
                pattern="secret_assignment",
            )
        )
    return findings


def _known_token_findings(text: str) -> list[CodexMemorySecretFinding]:
    findings: list[CodexMemorySecretFinding] = []
    for match in _KNOWN_TOKEN_RE.finditer(text):
        start = match.start("value")
        if _has_secret_label_nearby(text, start):
            severity: CodexMemoryPrivacySeverity = "critical"
        else:
            severity = "high"
        findings.append(
            _finding(
                text,
                kind="token",
                severity=severity,
                start=start,
                end=match.end("value"),
                pattern="known_token_prefix",
            )
        )
    return findings


def _dedupe_findings(findings: list[CodexMemorySecretFinding]) -> list[CodexMemorySecretFinding]:
    ordered = sorted(
        findings,
        key=lambda item: (
            item.start,
            -(item.end - item.start),
            -_SEVERITY_RANK[item.severity],
        ),
    )
    selected: list[CodexMemorySecretFinding] = []
    for finding in ordered:
        if any(_overlaps(finding, existing) for existing in selected):
            continue
        selected.append(finding)
    return sorted(selected, key=lambda item: item.start)


def _overlaps(left: CodexMemorySecretFinding, right: CodexMemorySecretFinding) -> bool:
    return left.start < right.end and right.start < left.end


def _finding(
    text: str,
    *,
    kind: CodexMemorySecretKind,
    severity: CodexMemoryPrivacySeverity,
    start: int,
    end: int,
    pattern: str,
) -> CodexMemorySecretFinding:
    line, column = _line_column(text, start)
    return CodexMemorySecretFinding(
        kind=kind,
        severity=severity,
        start=start,
        end=end,
        line=line,
        column=column,
        pattern=pattern,
    )


def _line_column(text: str, offset: int) -> tuple[int, int]:
    line = text.count("\n", 0, offset) + 1
    line_start = text.rfind("\n", 0, offset)
    column = offset + 1 if line_start == -1 else offset - line_start
    return line, column


def _kind_from_label(label: str) -> CodexMemorySecretKind:
    normalized = label.lower()
    if "password" in normalized or "passwd" in normalized:
        return "password"
    if "private" in normalized:
        return "private_key"
    if "api" in normalized and "key" in normalized:
        return "api_key"
    if "token" in normalized:
        return "token"
    return "secret"


def _assignment_severity(kind: CodexMemorySecretKind, value: str) -> CodexMemoryPrivacySeverity:
    if kind in {"private_key", "api_key"}:
        return "critical"
    if kind == "password":
        return "high"
    if len(value) >= 20 or _looks_high_entropy(value):
        return "high"
    return "medium"


def _looks_high_entropy(value: str) -> bool:
    if len(value) < 16:
        return False
    character_classes = sum(
        [
            any(char.islower() for char in value),
            any(char.isupper() for char in value),
            any(char.isdigit() for char in value),
            any(char in "_-+/=." for char in value),
        ]
    )
    return character_classes >= 3


def _looks_like_placeholder(value: str) -> bool:
    normalized = value.strip("\"'").lower()
    return normalized in {
        "<redacted>",
        "[redacted]",
        "redacted",
        "changeme",
        "change-me",
        "example",
        "placeholder",
        "set-from-your-secret-manager",
    }


def _has_secret_label_nearby(text: str, start: int) -> bool:
    window = text[max(0, start - 80) : start]
    return bool(_SECRET_LABEL_RE.search(window))
