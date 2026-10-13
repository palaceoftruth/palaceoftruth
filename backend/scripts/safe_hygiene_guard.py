"""Offensive-safe hygiene review-aid for publishable Palace source.

This is a path/line/rule-only review aid for known credential-shaped
field assignments. It is NOT a comprehensive secret detector: it does
not parse, normalise, hash, fingerprint, sample, length, or print
existing secret values, and it does not assert whether a finding is a
real credential. The guard never reads the live environment,
production logs, or existing assigned values. Its only output is the
file path, the source line, the matched field name, and a static rule
identifier. It is a limited review aid, not a blocking security
guard. Exit code is always 0; reviewers read the JSON rule-id list.

The OWNER of any credential is solely responsible for rotation,
revocation, scope change, validity, and containment. This tool does
not test, validate, replay, mint, or rotate anything. It surfaces
where a known credential-shaped name is being assigned so a human
reviewer can confirm the assignment is correct.
"""
from __future__ import annotations
import json
import re
import subprocess
import sys
from pathlib import Path

# Field-name patterns known to commonly hold credentials in the Palace code
# base. This is a name-only classifier; values are never inspected.
KNOWN_CREDENTIAL_FIELD = re.compile(
    r"(?i)\b(?:api[-_]?key|secret|password|token|authorization|credential|private[-_]?key|credentialvalue|client[-_]?secret|admin[-_]?secret|bearer|refresh[-_]?token|signing[-_]?key|encryption[-_]?key|hmac|jwt|oauth)(?:[_-]\w+)*\b|\b\w*(?:api[-_]?key|secret|password|token|authorization|credential|private[-_]?key|credentialvalue|client[-_]?secret|admin[-_]?secret|bearer|refresh[-_]?token|signing[-_]?key|encryption[-_]?key|hmac|jwt|oauth)\w*\b"
)

# Field names that look credential-shaped but are application-defined
# identifiers and not credential values. Listed exhaustively to avoid
# logging rule hits on app fields.
EXCLUDED_FIELDNAMES = {
    "key", "result", "target", "payload", "idempotency_key", "slot",
    "scope_key", "manifest_sha256", "anchor_sha256", "release_key",
    "digest", "image_id", "key_path", "oidc_key_id", "public_key",
    "public_id", "scope_type", "label_key", "key_alias", "key_secret",
    "registry-password", "id-token",
    "credentials_count", "credentials_dir", "credential_format",
    "credential_name", "credential_role", "credential_type",
    "credential_id", "credential_value_pointer", "credential_owner",
    "credentials_path", "credentials_endpoint", "credentials_url",
}

# Path tokens that identify true secret-handling source. Everything else
# is treated as public/assertion/text surface and the rule is still
# emitted, but the rule is the only output. The guard never inspects
# the value.
SECRET_HANDLING_PATH_TOKENS = {
    "auth.py", "secrets/", "/secrets/", "credentials.py", "authn.py",
    "authz.py", "session.py", "/tokens/", "/keys/",
}

# Path tokens that mark the actual credential publication surface. The
# guard DOES scan these and emits rules for them. They are no longer
# treated as suppressed fixtures.
PUBLICATION_PATH_TOKENS = {
    "receipts/", "/receipts/", ".env", ".env.example",
    "junit/", "test_artifact_", "test-results/", "htmlcov/",
    "build/", "/build/", "dist/", "html/", "junit.xml",
}

# Path tokens that the guard does not scan. The guard is path-aware: it
# classifies files into review-aid categories and emits the category as
# the rule id.
EXCLUDED_PATH_TOKENS = {
    "tests/", ".test.", "/tests/", "docs/", "AGENTS.md", "README.md",
    "CHANGELOG.md", "INVENTORY.md", "Ledger.md", "ledger.md",
    ".skill/", "/skills/", "PR_BODY", "summary", "report",
    "REVIEW", "REPAIR", "PALACE-PRIMARY", "HANDOVER", "audit",
    "/examples/", ".github/", "/.github/",
}

# Static rule ids. The guard never produces a per-finding unique tag,
# hash, length or sample derived from any value.
RULE_API_KEY = "rule:credential-shaped-api-key-assignment"
RULE_SECRET = "rule:credential-shaped-secret-assignment"
RULE_TOKEN = "rule:credential-shaped-token-assignment"
RULE_PASSWORD = "rule:credential-shaped-password-assignment"
RULE_AUTH = "rule:credential-shaped-authorization-assignment"
RULE_CREDENTIAL = "rule:credential-shaped-credential-assignment"
RULE_PRIVATE_KEY = "rule:credential-shaped-private-key-assignment"
RULE_CLIENT_SECRET = "rule:credential-shaped-client-secret-assignment"
RULE_ADMIN_SECRET = "rule:credential-shaped-admin-secret-assignment"
RULE_BEARER = "rule:credential-shaped-bearer-assignment"
RULE_REFRESH_TOKEN = "rule:credential-shaped-refresh-token-assignment"
RULE_SIGNING_KEY = "rule:credential-shaped-signing-key-assignment"
RULE_ENCRYPTION_KEY = "rule:credential-shaped-encryption-key-assignment"
RULE_HMAC = "rule:credential-shaped-hmac-assignment"
RULE_JWT = "rule:credential-shaped-jwt-assignment"
RULE_OAUTH = "rule:credential-shaped-oauth-assignment"
RULE_DEFAULT = "rule:credential-shaped-assignment"

# Map each known field-name token to its static rule id.
def _rule_for_field(field: str) -> str:
    f = field.lower()
    if "private" in f and "key" in f:
        return RULE_PRIVATE_KEY
    if "api" in f and "key" in f:
        return RULE_API_KEY
    if "client" in f and "secret" in f:
        return RULE_CLIENT_SECRET
    if "admin" in f and "secret" in f:
        return RULE_ADMIN_SECRET
    if "refresh" in f and "token" in f:
        return RULE_REFRESH_TOKEN
    if "signing" in f and "key" in f:
        return RULE_SIGNING_KEY
    if "encryption" in f and "key" in f:
        return RULE_ENCRYPTION_KEY
    if "password" in f:
        return RULE_PASSWORD
    if "bearer" in f:
        return RULE_BEARER
    if "hmac" in f:
        return RULE_HMAC
    if "jwt" in f:
        return RULE_JWT
    if "oauth" in f:
        return RULE_OAUTH
    if "authorization" in f:
        return RULE_AUTH
    if "credential" in f:
        return RULE_CREDENTIAL
    if "token" in f:
        return RULE_TOKEN
    if "secret" in f:
        return RULE_SECRET
    if "key" in f:
        return RULE_API_KEY
    return RULE_DEFAULT


# Path classification. Returns the rule id that names the rule being
# applied to the file. The guard never returns a value, hash, sample,
# length, or unique tag derived from the value.
def classify_path(path: Path) -> str:
    name = str(path)
    if "/private-retained-receipts/" in name or "/.git/" in name:
        return "category:excluded"
    if name.endswith("receipts/INDEX.md"):
        return "category:publishable-source"
    if any(token in name for token in SECRET_HANDLING_PATH_TOKENS):
        return "category:secret-handling"
    if any(token in name for token in PUBLICATION_PATH_TOKENS):
        return "category:publication-surface"
    if any(token in name for token in EXCLUDED_PATH_TOKENS):
        return "category:excluded"
    return "category:publishable-source"


def is_allowed_text_file(path: Path) -> bool:
    """Return True if the file is text we should scan for the rule list."""
    name = str(path)
    if any(token in name for token in EXCLUDED_PATH_TOKENS):
        return False
    if "/private-retained-receipts/" in name:
        return False
    if path.suffix.lower() in {
        ".py", ".sh", ".yaml", ".yml", ".json", ".md", ".env", ".env.example",
        ".toml", ".cfg", ".ini", ".txt", ".tf", ".csv", ".hcl",
        ".xml", ".junit", ".html",
    }:
        return True
    return False


def scan_blob(path: Path, blob: str) -> list[dict]:
    """Return a list of static rule ids for credential-shaped assignments.

    The guard emits only path, line, field name and the matching static
    rule id. No value, no length, no sample, no hash, no fingerprint
    and no other value-derived data is included.
    """
    findings = []
    category = classify_path(path)
    for lineno, line in enumerate(blob.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        if "=" not in line and ":" not in line:
            continue
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_\-.]*)\s*[:=]\s*(.+?)\s*$", line)
        if not m:
            continue
        field, _raw_value = m.group(1), m.group(2)
        if field in EXCLUDED_FIELDNAMES:
            continue
        if not KNOWN_CREDENTIAL_FIELD.search(field):
            continue
        findings.append({
            "path": str(path),
            "line": lineno,
            "field": field,
            "rule": _rule_for_field(field),
            "category": category,
        })
    return findings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(json.dumps({"error": "usage: safe_hygiene_guard.py <ref>"}, indent=2))
        return 2
    ref = argv[1]
    cmd = ["git", "ls-tree", "-r", "--name-only", ref]
    tree = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.splitlines()
    out = {
        "ref": ref,
        "scanned": 0,
        "policy": "review-aid-only; no value, length, sample, hash, or fingerprint is ever emitted",
        "rule_ids": [],
        "findings": [],
    }
    seen = set()
    for name in tree:
        p = Path(name)
        if not is_allowed_text_file(p):
            continue
        try:
            blob = subprocess.run(["git", "show", f"{ref}:{name}"], capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            continue
        out["scanned"] += 1
        rows = scan_blob(p, blob)
        out["findings"].extend(rows)
        for r in rows:
            seen.add(r["rule"])
    out["rule_ids"] = sorted(seen)
    out["finding_count"] = len(out["findings"])
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
