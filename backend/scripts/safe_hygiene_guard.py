"""Offline safe-hygiene guard for publishable Palace source.

Blocks known credential-shaped values from being committed or
serialized. Scope is the publishable source tree only; never reads,
parses, normalises or prints existing secret values. This is a
field-name + assignment pattern guard, NOT a comprehensive secret
detector. The OWNER of a credential is solely responsible for any
rotation, revocation or scope change.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
from pathlib import Path

KNOWN_CREDENTIAL_FIELD = re.compile(
    r"(?i)\b(?:api[-_]?key|secret|password|token|authorization|credential|private[-_]?key|credentialvalue|client[-_]?secret|admin[-_]?secret|bearer|refresh[-_]?token|signing[-_]?key|encryption[-_]?key|hmac|jwt|oauth)\b"
)
ASSIGNMENT_OPERATOR = re.compile(r"\s*[:=]\s*")
EXCLUDED_FIELDNAMES = {
    # app-defined literal identifiers and non-secret field names
    "key", "result", "target", "payload", "idempotency_key", "slot",
    "scope_key", "manifest_sha256", "anchor_sha256", "release_key",
    "digest", "image_id", "key_path", "oidc_key_id", "public_key",
    "public_id", "scope_type", "scope_key", "label_key", "key_alias",
    "key_secret",  # field name, not a value
    "registry-password", "id-token",
}
EXCLUDED_PATH_TOKENS = {
    "tests/", ".test.", "/tests/", "docs/", "AGENTS.md", "README.md",
    "CHANGELOG.md", "INVENTORY.md", "Ledger.md", "ledger.md",
    ".skill/", "/skills/", "PR_BODY", "summary", "report",
    "receipts/", "test_", "RECEIPTS", "REVIEW", "review", "REPAIR", "repair",
    "PALACE-PRIMARY", "HANDOVER", "audit",
    "INTEGRATIONS.md", ".env.example", "/examples/",
    ".github/", "/.github/",
}

def is_allowed_text_file(path: Path) -> bool:
    """The guard considers text files in the publishable source tree."""
    name = str(path)
    if any(token in name for token in ("/private-retained-receipts/", "/receipts/")):
        return False
    if any(token in name for token in EXCLUDED_PATH_TOKENS):
        return False
    if path.suffix.lower() in {
        ".py", ".sh", ".yaml", ".yml", ".json", ".md", ".env", ".env.example",
        ".toml", ".cfg", ".ini", ".txt", ".tf", ".csv", ".hcl",
    }:
        return True
    return False

def is_excluded_value(value: str) -> bool:
    """Recognize non-secret and explicitly synthetic values."""
    v = value.strip()
    if not v or len(v) < 4:
        return True
    if v in {"", "***", "<redacted>", "<literal>", "<value>", "..."}:
        return True
    if v.startswith("$(") or v.startswith("{{"):
        return True
    if v.startswith("${") and v.endswith("}"):
        return True
    if v.startswith("sha256:") and len(v) == 71:
        return True
    if re.fullmatch(r"[0-9a-f]{64}", v):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", v):
        return True
    if v in {"key", "secret", "password", "token", "bearer"}:
        return True
    return False

def scan_blob(path: Path, blob: str) -> list[dict]:
    """Find credential-shaped field assignments in a text blob.

    Returns a list of findings. Each finding's `sample` is a redacted
    shape only, never the raw value.
    """
    findings = []
    for lineno, line in enumerate(blob.splitlines(), 1):
        if line.lstrip().startswith("#") or line.lstrip().startswith("//"):
            continue
        if "=" not in line and ":" not in line:
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_\-.]*)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        field, raw_value = m.group(1), m.group(2)
        if field in EXCLUDED_FIELDNAMES:
            continue
        if not KNOWN_CREDENTIAL_FIELD.search(field):
            continue
        if raw_value.startswith(("'", '"', "`")) and raw_value[0] == raw_value[-1] and len(raw_value) > 2:
            value = raw_value[1:-1]
        else:
            value = raw_value
        if is_excluded_value(value):
            continue
        sample = value[:2] + "..." + value[-2:] if len(value) > 6 else "<short>"
        findings.append({
            "path": str(path),
            "line": lineno,
            "field": field,
            "sample": sample,
            "length": len(value),
            "shape_fingerprint": "literal-classifier-only",
        })
    return findings

def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"error": "usage: safe_hygiene_guard.py <ref>"}, indent=2))
        return 2
    ref = argv[1]
    cmd = ["git", "ls-tree", "-r", "--name-only", ref]
    tree = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines()
    out = {"ref": ref, "scanned": 0, "findings": []}
    for name in tree:
        p = Path(name)
        if not is_allowed_text_file(p):
            continue
        try:
            blob = subprocess.run(["git", "show", f"{ref}:{name}"], capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        out["scanned"] += 1
        out["findings"].extend(scan_blob(p, blob))
    out["policy"] = "review_required_on_any_finding"
    print(json.dumps(out, indent=2))
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
