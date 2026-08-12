"""Normalise untrusted third-party strings at the ingestion boundary (L-15).

oEmbed markup, feed titles and summaries, and uploaded filenames all arrive from
sources we do not control and are persisted verbatim today. Nothing renders them
as HTML right now, but "safe because every current consumer escapes correctly"
is a property that a future component, export or digest can silently break. So
clean the values once, on write, instead of relying on every reader.

HTML sanitation uses ``nh3`` (the Rust ``ammonia`` library). Hand-written tag
stripping is a well-known source of bypasses; do not replace it with a regex.
"""

import re
import unicodedata
from html import escape, unescape

import nh3

# oEmbed payloads are blockquote-shaped citation markup. Everything outside this
# set - script, style, iframe, object, form, event handlers, inline styles - is
# removed rather than escaped, because none of it carries post content.
EMBED_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "cite",
    "code",
    "em",
    "i",
    "li",
    "ol",
    "p",
    "pre",
    "q",
    "s",
    "span",
    "strong",
    "sub",
    "sup",
    "u",
    "ul",
}

EMBED_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "blockquote": {"cite", "lang"},
    "q": {"cite"},
    "span": {"lang"},
}

# nh3 drops any URL whose scheme is not listed, which closes `javascript:` and
# `data:` hrefs. Mirrors the frontend allow-list in src/lib/safeUrl.ts (L-23).
EMBED_ALLOWED_URL_SCHEMES = {"http", "https", "mailto"}

# Titles and summaries are single-line display strings. Cap them so a hostile
# feed cannot push a multi-megabyte value into a column that every list view
# loads.
MAX_TITLE_LENGTH = 512
MAX_SUMMARY_LENGTH = 20_000
MAX_FILENAME_LENGTH = 255

# C0/C1 control characters except tab, newline and carriage return, plus the
# bidirectional-override characters that let a filename display in reverse
# (U+200E..U+200F, U+202A..U+202E, U+2066..U+2069).
_CONTROL_CHARS = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]"
)
_WHITESPACE_RUN = re.compile(r"\s+")

# Rounds allowed for the decode/strip fixpoint below. Each round resolves one
# level of entity nesting, so real content converges in one or two.
_FLATTEN_ROUNDS = 6


def sanitize_embed_html(value: object) -> str:
    """Reduce provider embed markup to an inert citation-only subset.

    Returns an empty string for anything that is not a non-empty string, so a
    provider returning ``null`` or a number cannot reach the database.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    return nh3.clean(
        value,
        tags=EMBED_ALLOWED_TAGS,
        attributes=EMBED_ALLOWED_ATTRIBUTES,
        url_schemes=EMBED_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def _collapse(value: str) -> str:
    """Drop control characters and squeeze whitespace runs to single spaces."""
    return _WHITESPACE_RUN.sub(" ", _CONTROL_CHARS.sub("", value)).strip()


def sanitize_plain_text(value: object, *, max_length: int) -> str:
    """Flatten an untrusted string to plain text of a bounded length.

    Escaping alone is not enough here: a title of ``&lt;script&gt;...`` decodes
    back into a live tag the moment any consumer unescapes it. So strip and
    decode alternately until the value stops changing, which leaves a string that
    contains no tag and no entity that decodes into one.
    """
    if not isinstance(value, str) or not value.strip():
        return ""

    text = value
    for _ in range(_FLATTEN_ROUNDS):
        # tags=set() drops every element; ammonia still removes the *content* of
        # script/style. Text and entities otherwise survive.
        stripped = nh3.clean(text, tags=set(), attributes={}, strip_comments=True)
        decoded = unescape(stripped)
        if decoded == text:
            break
        text = decoded
    else:
        # No fixpoint after several rounds means deliberately nested encoding.
        # Escape the whole thing rather than store a value we cannot vouch for.
        text = escape(text, quote=False)

    return _collapse(text)[:max_length]


def sanitize_title(value: object) -> str:
    """Normalise a feed or document title for storage."""
    return sanitize_plain_text(value, max_length=MAX_TITLE_LENGTH)


def sanitize_summary(value: object) -> str:
    """Normalise a feed entry summary for storage.

    Feed summaries legitimately carry markup. It is flattened rather than kept,
    because the pipeline treats this value as article text.
    """
    return sanitize_plain_text(value, max_length=MAX_SUMMARY_LENGTH)


def sanitize_filename(value: object) -> str:
    """Reduce an uploaded filename to a safe, displayable basename.

    Storage paths are already server-generated UUIDs, so this protects the
    *displayed* and *exported* name: no directory separators, no control or
    bidirectional-override characters, no leading dots, bounded length.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    # NFC first so a decomposed separator cannot survive the replacement below.
    text = unicodedata.normalize("NFC", value)
    text = _collapse(text)
    # Take the basename under both separators: the client picks the convention.
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    # Leading dots hide the file; a bare ".." is not a name at all.
    text = text.lstrip(". ")
    if not text:
        return ""
    if len(text) <= MAX_FILENAME_LENGTH:
        return text
    stem, separator, suffix = text.rpartition(".")
    if separator and stem and len(suffix) <= 32:
        return f"{stem[: MAX_FILENAME_LENGTH - len(suffix) - 1]}.{suffix}"
    return text[:MAX_FILENAME_LENGTH]
