"""Canon folder import: parse, plan, then commit in one transaction.

Three layers, kept apart because phase 2 repo sync reuses the first two:

* **Parse** turns one uploaded file into an :class:`ImportFile`. Pure: bytes in,
  a validated value object out, no database, no session.
* **Plan** decides what each file would do to the tenant's canon, with one query
  for the whole batch. It writes nothing, so a preview is free.
* **Commit** applies a plan the operator was already shown, in a single
  transaction: every page lands or none does.

Two rules shape the whole module.

**The front matter is an allowlist.** ``title``, ``summary``, ``section``,
``room``, ``tags`` and ``related`` become columns. Every other key is preserved
under ``metadata_.canon_extra`` and never becomes a column, so an arbitrary YAML
key from someone's notes cannot shape a canonical row.

**Nothing is silently skipped.** An unchanged body is reported ``skip-unchanged``
and performs zero writes, so re-importing a folder is a no-op. A published page
whose body differs is a ``conflict`` the operator has to decide on; the importer
never overwrites published canon by itself.

Every cap lives in ``app.services.canon``; this module imports them rather than
restating a limit.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canon import CanonPage
from app.models.item import Item
from app.models.palace import Room
from app.services.canon import (
    CANON_IMPORT_MAX_FILES,
    CANON_IMPORT_MAX_FILE_BYTES,
    CANON_IMPORT_MAX_TOTAL_BYTES,
    CANON_SUMMARY_MAX_CHARS,
    CanonCapExceeded,
    CanonValidationError,
    enforce_summary,
    validate_section,
)
from app.services.canon_authoring import (
    UNSET,
    _CANON_TAG,
    _SECTION_TAG_PREFIX,
    _item_status_for,
    CanonConflictError,
    create_canon_page,
    update_canon_page,
)

# The only file types a folder import accepts. Everything else is refused by
# name before it is decoded, so a stray PDF is a clear 422 and not mojibake.
_EXTENSIONS = (".md", ".mdx", ".txt")

# Front-matter keys that become columns. Anything else is preserved, never
# promoted: a typo like `rooms:` must not silently bind a page to a room.
FRONT_MATTER_KEYS = frozenset({"title", "summary", "section", "room", "tags", "related"})

# Bumped when the meaning of a plan changes; the id is content-addressed on the
# version plus the (slug, body hash) pairs, so an old plan id cannot be replayed
# against a newer plan scheme.
PLAN_VERSION = "canon-import-v1"

# ``canon_authoring._validate_slug`` accepts 1..80 chars; a filename can be
# longer, and failing a 400-file import over one long name helps nobody, so the
# derived slug is cut at the same ceiling here. Kept as a local literal because
# importing a private cap would move the rule into this module's import graph.
_SLUG_MAX_CHARS = 80

_HEADING = re.compile(r"^ {0,3}#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)
_SLUG_RUN = re.compile(r"[^a-z0-9]+")
_LEADING_BLANK = re.compile(r"^(?:[ \t]*\r?\n)+")
_FIRST_SENTENCE = re.compile(r"(.+?[.!?])(?:\s|$)")

_ACTIONS = ("create", "update", "skip-unchanged", "conflict")
_DECISIONS = ("apply", "skip", "rename")
_RENAME_ATTEMPTS = 100


# --- Value objects ---------------------------------------------------------


@dataclass(frozen=True)
class ImportFile:
    """One uploaded file, parsed and validated, with no database involved."""

    path: str
    body: str
    title: str
    slug: str
    summary: str
    section: str
    room_slug: str | None
    tags: list[str]
    related: list[str]
    extra: dict
    content_hash: str
    size_bytes: int
    # ``parse_import_file`` derives ``reference`` when the front matter names no
    # section, so ``section`` alone cannot tell an author's ``section: reference``
    # from an absent one. A repo binding needs that difference: its default
    # section comes from the source, not from the parser.
    section_from_front_matter: bool = False


@dataclass(frozen=True)
class PlannedFile:
    """What one file would do to the tenant's canon, as a human reads it."""

    path: str
    slug: str
    title: str
    section: str
    action: str
    content_hash: str
    existing_status: str | None
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    """A whole folder's worth of decisions, addressed by ``plan_id``."""

    plan_id: str
    files: list[PlannedFile]
    counts: dict[str, int]


@dataclass(frozen=True)
class ImportResult:
    """What a commit actually did to one file. Plain data, never an ORM row."""

    slug: str
    action: str
    new_slug: str | None


# --- Parsing ---------------------------------------------------------------


def _content_hash(body: str) -> str:
    """sha256 of the body exactly as stored.

    Deliberately not normalised: a single changed byte must change the hash, or
    re-importing an edited file would look like an unchanged one and the edit
    would be dropped.
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _basename(path: str) -> str:
    """The last path segment, tolerating either separator.

    Browsers send a bare filename; a repo sync sends a relative path. Both must
    derive the same slug from the same file.
    """
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _stem(path: str) -> str:
    name = _basename(path)
    stem, dot, _extension_text = name.rpartition(".")
    return stem if dot and stem else name


def _extension(path: str) -> str:
    name = _basename(path)
    _stem_text, dot, extension_text = name.rpartition(".")
    return f".{extension_text.lower()}" if dot else ""


def slug_from_path(path: str) -> str:
    """Derive a canon slug from a filename: lowercase, hyphenated, trimmed."""
    slug = _SLUG_RUN.sub("-", _stem(path).lower()).strip("-")
    if len(slug) > _SLUG_MAX_CHARS:
        slug = slug[:_SLUG_MAX_CHARS].strip("-")
    if not slug:
        raise CanonValidationError(f"{path}: cannot derive a canon slug from this filename")
    return slug


def _title_from_path(path: str) -> str:
    words = re.sub(r"[-_]+", " ", _stem(path)).split()
    return " ".join(word.capitalize() for word in words)


def _heading_title(body: str) -> str | None:
    match = _HEADING.search(body)
    return match.group(1).strip() if match else None


def _split_front_matter(path: str, text: str) -> tuple[dict, str]:
    """Return ``(front_matter, body)``; the front matter is never in the body.

    An opening ``---`` with no closing line is an error rather than a guess: the
    file's intent is genuinely ambiguous, and silently importing a YAML block as
    page prose would put a title and a list of tags into canon.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text

    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            block = "".join(lines[1:index])
            body = "".join(lines[index + 1:])
            try:
                loaded = yaml.safe_load(block) if block.strip() else {}
            except yaml.YAMLError as exc:
                raise CanonValidationError(f"{path}: front matter is not valid YAML ({exc})") from exc
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                raise CanonValidationError(
                    f"{path}: front matter must be a YAML mapping, not {type(loaded).__name__}"
                )
            return loaded, body

    raise CanonValidationError(f"{path}: front matter opens with '---' but never closes with one")


def _front_matter_text(value) -> str | None:
    """A scalar front-matter value as stripped text, or None when absent."""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
    elif isinstance(value, (int, float, bool)):
        cleaned = str(value)
    else:
        raise CanonValidationError(f"front matter value must be text, not {type(value).__name__}")
    return cleaned or None


def _front_matter_list(path: str, key: str, value) -> list[str]:
    """A front-matter list as text; a bare scalar becomes a one-item list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        items: list[str] = []
        for entry in value:
            text = _front_matter_text(entry)
            if text is not None and text not in items:
                items.append(text)
        return items
    raise CanonValidationError(f"{path}: front matter key {key!r} must be a list or a string")


def _jsonable(value):
    """Coerce a YAML value into something ``metadata`` (JSONB) can hold.

    PyYAML happily parses dates and sets, which JSON cannot store; stringifying
    the leaf keeps an unknown front-matter key preserved instead of failing the
    whole batch on insert.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _cap_summary(text: str) -> str:
    """Cut a derived summary to ``CANON_SUMMARY_MAX_CHARS`` on a word boundary.

    A front-matter summary is human-authored and is validated, never cut, by
    ``enforce_summary``. A derived one comes from prose the author did not write
    for the index line, so a long first sentence must not fail the import.
    """
    if len(text) <= CANON_SUMMARY_MAX_CHARS:
        return text
    cut = text[:CANON_SUMMARY_MAX_CHARS]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:-")


def _first_sentence(line: str) -> str:
    match = _FIRST_SENTENCE.match(line.strip())
    return (match.group(1) if match else line.strip()).strip()


def derive_summary(path: str, body: str) -> str:
    """The first non-heading, non-empty line's first sentence."""
    for line in body.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        return _cap_summary(_first_sentence(candidate))
    raise CanonValidationError(f"{path}: cannot derive a summary; the body has no prose line")


def parse_import_file(path: str, raw: bytes) -> ImportFile:
    """Parse one uploaded file into an :class:`ImportFile`.

    Derivation when a value is missing: title from the first ``# `` heading (then
    the filename), slug always from the filename, summary from the first prose
    sentence, section ``reference``.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonValidationError(
            f"{path}: not valid UTF-8 ({exc.reason} at byte {exc.start})"
        ) from exc
    if text.startswith("\ufeff"):
        text = text[1:]

    front, remainder = _split_front_matter(path, text)
    # Leading blank lines are presentation, not content; everything after them
    # is stored and hashed byte for byte.
    body = _LEADING_BLANK.sub("", remainder)

    title = _front_matter_text(front.get("title")) or _heading_title(body) or _title_from_path(path)
    if not title.strip():
        raise CanonValidationError(f"{path}: cannot derive a canon title from this file")

    raw_section = _front_matter_text(front.get("section"))
    if raw_section is None:
        section = "reference"
        section_from_front_matter = False
    else:
        try:
            section = validate_section(raw_section)
        except CanonValidationError as exc:
            raise CanonValidationError(f"{path}: {exc}") from exc
        section_from_front_matter = True

    raw_summary = _front_matter_text(front.get("summary"))
    try:
        summary = enforce_summary(raw_summary) if raw_summary else enforce_summary(
            derive_summary(path, body)
        )
    except CanonValidationError as exc:
        raise CanonValidationError(f"{path}: {exc}") from exc

    extra = {
        str(key): _jsonable(value)
        for key, value in front.items()
        if str(key) not in FRONT_MATTER_KEYS
    }

    return ImportFile(
        path=path,
        body=body,
        title=title.strip(),
        slug=slug_from_path(path),
        summary=summary,
        section=section,
        room_slug=_front_matter_text(front.get("room")),
        tags=_front_matter_list(path, "tags", front.get("tags")),
        related=_front_matter_list(path, "related", front.get("related")),
        extra=extra,
        content_hash=_content_hash(body),
        size_bytes=len(raw),
        section_from_front_matter=section_from_front_matter,
    )


def binding_section(file: ImportFile, *, default: str) -> str:
    """The section a repo binding should write: the file's own, else the binding's.

    Front matter wins, then the binding decides — the same precedence a folder
    import uses, except that a folder import has no binding default and falls
    back to ``reference``. Kept here rather than in the sync module so the one
    parser stays the only thing that knows how a section is derived.
    """
    return file.section if file.section_from_front_matter else default


# --- Caps ------------------------------------------------------------------


def _check_extension(path: str) -> None:
    extension = _extension(path)
    if extension not in _EXTENSIONS:
        raise CanonValidationError(
            f"{path}: unsupported import file type {extension or '(none)'!r}; "
            f"expected one of {', '.join(_EXTENSIONS)}"
        )


def _check_size(path: str, size_bytes: int) -> None:
    if size_bytes > CANON_IMPORT_MAX_FILE_BYTES:
        raise CanonCapExceeded(
            f"{path}: {size_bytes} bytes; cap is {CANON_IMPORT_MAX_FILE_BYTES} per file"
        )


def check_import_upload(path: str, size_bytes: int) -> None:
    """Reject one upload by extension and size, before it is decoded or parsed."""
    _check_extension(path)
    _check_size(path, size_bytes)


def check_import_batch_shape(*, file_count: int, total_bytes: int) -> None:
    """Reject a folder that is already too big, before any of it is parsed.

    The router runs this while it reads uploads, so a 501-file folder is refused
    without decoding a single body, and ``validate_import_batch`` runs it again
    on the parsed batch so the cap holds whichever entry point is used.
    """
    if file_count > CANON_IMPORT_MAX_FILES:
        raise CanonCapExceeded(
            f"import has {file_count} files; cap is {CANON_IMPORT_MAX_FILES} per folder"
        )
    if total_bytes > CANON_IMPORT_MAX_TOTAL_BYTES:
        raise CanonCapExceeded(
            f"import is {total_bytes} bytes; cap is {CANON_IMPORT_MAX_TOTAL_BYTES} per folder"
        )


def validate_import_batch(files: list[ImportFile], *, extensions_checked: bool = True) -> None:
    """Reject a batch that breaks a cap, or that claims one slug twice.

    ``extensions_checked=False`` is for callers that already rejected bad types
    at the upload boundary, where the filename is known before the bytes are
    decoded.
    """
    check_import_batch_shape(file_count=len(files), total_bytes=0)

    total = 0
    seen: dict[str, str] = {}
    for file in files:
        if extensions_checked:
            _check_extension(file.path)
        _check_size(file.path, file.size_bytes)
        previous = seen.get(file.slug)
        if previous is not None:
            raise CanonValidationError(
                f"{file.path} and {previous} both claim canon slug {file.slug!r}; "
                "rename one of them"
            )
        seen[file.slug] = file.path
        total += file.size_bytes

    check_import_batch_shape(file_count=len(files), total_bytes=total)


# --- Planning --------------------------------------------------------------


def plan_id_for(files: list[ImportFile]) -> str:
    """sha256 over ``PLAN_VERSION`` plus the sorted ``slug:content_hash`` pairs.

    Content-addressed, so there is nothing to store and no plan table to clean
    up: the same folder always yields the same id, and any change to any body or
    slug yields a different one. A commit therefore cannot apply a plan the
    caller did not see.
    """
    pairs = sorted(f"{file.slug}:{file.content_hash}" for file in files)
    canonical = "\n".join((PLAN_VERSION, *pairs))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _existing_pages(
    db: AsyncSession, *, tenant_id: str, slugs: list[str]
) -> dict[str, tuple[str, str]]:
    """slug -> (status, sha256 of the stored body), in one query for the batch.

    The body is not on the page row; it is the governing item's ``raw_content``,
    which is what retrieval and the digest actually read.
    """
    wanted = sorted({slug for slug in slugs})
    if not wanted:
        return {}
    rows = (
        await db.execute(
            select(CanonPage.slug, CanonPage.status, Item.raw_content)
            .join(Item, Item.id == CanonPage.item_id)
            .where(CanonPage.tenant_id == tenant_id, CanonPage.slug.in_(wanted))
        )
    ).all()
    return {slug: (status, _content_hash(raw_content or "")) for slug, status, raw_content in rows}


async def stored_body_hash(db: AsyncSession, *, tenant_id: str, slug: str) -> str | None:
    """sha256 of the body canon currently stores for one slug, or None.

    The body is not on the page row; it is the governing item's ``raw_content``.
    A repo run needs it to say *what* differs in a conflict artifact, and it goes
    through the same helper ``plan_import`` uses so the two can never disagree
    about what "the stored body" means.
    """
    existing = await _existing_pages(db, tenant_id=tenant_id, slugs=[slug])
    found = existing.get(slug)
    return None if found is None else found[1]


def _action_for(file: ImportFile, status: str | None, stored_hash: str | None) -> tuple[str, str]:
    """The plan action and the sentence the preview shows for it."""
    if status is None:
        return "create", f"no canon page named {file.slug} yet"
    if stored_hash == file.content_hash:
        return "skip-unchanged", "the stored body already matches this file byte for byte"
    if status == "published":
        return "conflict", f"published page {file.slug} has a different body; decide per file"
    return "update", f"{status} page {file.slug} has a different body"


async def plan_import(db: AsyncSession, *, tenant_id: str, files: list[ImportFile]) -> ImportPlan:
    """Work out what each file would do, without writing anything."""
    existing = await _existing_pages(db, tenant_id=tenant_id, slugs=[file.slug for file in files])

    planned: list[PlannedFile] = []
    for file in files:
        status, stored_hash = existing.get(file.slug, (None, None))
        action, reason = _action_for(file, status, stored_hash)
        planned.append(
            PlannedFile(
                path=file.path,
                slug=file.slug,
                title=file.title,
                section=file.section,
                action=action,
                content_hash=file.content_hash,
                existing_status=status,
                reason=reason,
            )
        )

    counts = {action: 0 for action in _ACTIONS}
    for entry in planned:
        counts[entry.action] = counts.get(entry.action, 0) + 1
    return ImportPlan(plan_id=plan_id_for(files), files=planned, counts=counts)


# --- Committing ------------------------------------------------------------


def _slug_with_suffix(base: str, suffix: int) -> str:
    tail = f"-{suffix}"
    room = _SLUG_MAX_CHARS - len(tail)
    return f"{base[:room].strip('-')}{tail}"


async def _existing_slugs(db: AsyncSession, *, tenant_id: str, candidates: list[str]) -> set[str]:
    if not candidates:
        return set()
    rows = await db.scalars(
        select(CanonPage.slug).where(
            CanonPage.tenant_id == tenant_id, CanonPage.slug.in_(candidates)
        )
    )
    return set(rows)


async def _rename_candidates(db: AsyncSession, *, tenant_id: str, bases: list[str]) -> set[str]:
    """Every ``base-2``..``base-100`` slug that already exists, in one query."""
    candidates = [
        _slug_with_suffix(base, suffix)
        for base in bases
        for suffix in range(2, _RENAME_ATTEMPTS + 2)
    ]
    return await _existing_slugs(db, tenant_id=tenant_id, candidates=candidates)


def _free_slug(base: str, taken: set[str]) -> str:
    for suffix in range(2, _RENAME_ATTEMPTS + 2):
        candidate = _slug_with_suffix(base, suffix)
        if candidate not in taken:
            return candidate
    raise CanonConflictError(f"could not find a free canon slug for {base}")


async def _room_ids(
    db: AsyncSession, *, tenant_id: str, room_slugs: set[str]
) -> dict[str, uuid.UUID]:
    """Resolve front-matter room slugs to room ids, in one query."""
    if not room_slugs:
        return {}
    rows = (
        await db.execute(
            select(Room.slug, Room.id).where(
                Room.tenant_id == tenant_id, Room.slug.in_(sorted(room_slugs))
            )
        )
    ).all()
    return {slug: room_id for slug, room_id in rows}


def _stash_metadata(item: Item, *, extra: dict, related: list[str]) -> None:
    """Keep the front-matter values that have no column.

    Unknown keys go under ``metadata_.canon_extra`` and are never promoted to a
    column, so a stray ``weird: 1`` cannot change what canon is. ``related`` is
    allowlisted but has no column either, so it is preserved beside them under
    ``metadata_.canon_related`` rather than silently dropped.
    """
    metadata = dict(item.metadata_ or {})
    if extra:
        metadata["canon_extra"] = extra
    if related:
        metadata["canon_related"] = list(related)
    if metadata != (item.metadata_ or {}):
        item.metadata_ = metadata


def _sync_tags(item: Item, *, section: str, tags: list[str]) -> None:
    """Apply the file's tags to the item.

    ``update_canon_page`` has no ``tags`` argument: it preserves whatever a
    caller added at create time. An import's front matter is the source of
    truth for its page, so tags are applied here for creates and updates alike;
    otherwise a re-import that only changed its tags would report "updated" and
    change nothing.
    """
    merged: list[str] = []
    for tag in [_CANON_TAG, f"{_SECTION_TAG_PREFIX}{section}", *tags]:
        if tag not in merged:
            merged.append(tag)
    item.tags = merged


async def commit_import(
    db: AsyncSession,
    *,
    tenant_id: str,
    author_subject: str,
    files: list[ImportFile],
    plan_id: str,
    decisions: dict[str, str],
) -> list[ImportResult]:
    """Apply the plan the operator saw, in one transaction.

    ``decisions`` maps a slug to ``apply`` (overwrite), ``rename`` (create
    ``<slug>-2``, then ``-3``, …) or ``skip``. A conflict with no decision is
    skipped: published canon is never overwritten by default. A decision for a
    slug the plan does not contain is an error, so a stale or mistyped form
    cannot quietly do nothing.

    The plan is recomputed here. If the recomputed id differs from the caller's,
    the folder or its bodies changed after the preview and the commit is refused
    rather than applied blind.

    Every write goes through ``create_canon_page`` / ``update_canon_page`` with
    ``commit=False``, and the batch commits once at the end; on any error the
    whole batch is rolled back. Results are captured as plain data before that
    commit, because projecting ORM attributes after a rollback raises
    ``MissingGreenlet``.
    """
    validate_import_batch(files)
    plan = await plan_import(db, tenant_id=tenant_id, files=files)
    if plan.plan_id != plan_id:
        raise CanonConflictError(
            "the folder changed between preview and commit: the uploaded files no longer "
            "match the plan the operator reviewed; preview again"
        )

    unknown = sorted(slug for slug in decisions if slug not in {entry.slug for entry in plan.files})
    if unknown:
        raise CanonConflictError(
            f"decisions name slugs that are not in this import plan: {', '.join(unknown)}"
        )
    for slug, decision in decisions.items():
        if decision not in _DECISIONS:
            raise CanonValidationError(
                f"decision for {slug} is {decision!r}; expected one of {', '.join(_DECISIONS)}"
            )

    by_slug = {file.slug: file for file in files}
    outcomes: dict[str, ImportResult] = {}
    work: list[tuple[PlannedFile, ImportFile, str]] = []
    for entry in plan.files:
        # A conflict is applied only when the operator says so; everything else
        # that is not a decision is applied as planned.
        decision = decisions.get(entry.slug, "skip" if entry.action == "conflict" else "apply")
        if entry.action == "skip-unchanged" or decision == "skip":
            outcomes[entry.slug] = ImportResult(slug=entry.slug, action="skipped", new_slug=None)
            continue
        work.append((entry, by_slug[entry.slug], decision))

    rename_bases = [entry.slug for entry, _file, decision in work if decision == "rename"]
    # Every slug this batch already claims, plus what the tenant already has, so
    # a rename cannot collide with a file earlier in the same upload.
    taken: set[str] = set(by_slug) | (
        await _rename_candidates(db, tenant_id=tenant_id, bases=rename_bases)
        if rename_bases
        else set()
    )
    room_ids = await _room_ids(
        db,
        tenant_id=tenant_id,
        room_slugs={file.room_slug for _entry, file, _decision in work if file.room_slug},
    )
    for _entry, file, _decision in work:
        if file.room_slug is not None and file.room_slug not in room_ids:
            raise CanonValidationError(
                f"{file.path}: no room with slug {file.room_slug!r} in this tenant"
            )

    now = datetime.now(timezone.utc)

    try:
        for entry, file, decision in work:
            if decision == "rename":
                new_slug = _free_slug(entry.slug, taken)
                taken.add(new_slug)
                write = await create_canon_page(
                    db,
                    tenant_id=tenant_id,
                    author_subject=author_subject,
                    slug=new_slug,
                    title=file.title,
                    summary=file.summary,
                    section=file.section,
                    body=file.body,
                    room_id=room_ids.get(file.room_slug) if file.room_slug else None,
                    tags=file.tags,
                    commit=False,
                )
                outcomes[entry.slug] = ImportResult(
                    slug=entry.slug, action="renamed", new_slug=new_slug
                )
            elif entry.action == "create":
                write = await create_canon_page(
                    db,
                    tenant_id=tenant_id,
                    author_subject=author_subject,
                    slug=entry.slug,
                    title=file.title,
                    summary=file.summary,
                    section=file.section,
                    body=file.body,
                    room_id=room_ids.get(file.room_slug) if file.room_slug else None,
                    tags=file.tags,
                    commit=False,
                )
                outcomes[entry.slug] = ImportResult(
                    slug=entry.slug, action="created", new_slug=None
                )
            else:
                write = await update_canon_page(
                    db,
                    tenant_id=tenant_id,
                    author_subject=author_subject,
                    slug=entry.slug,
                    title=file.title,
                    summary=file.summary,
                    section=file.section,
                    body=file.body,
                    # A page with no room in its front matter keeps the room it
                    # already had; an import must not unbind a page by omission.
                    room_id=room_ids.get(file.room_slug) if file.room_slug else UNSET,
                    change_note="import",
                    commit=False,
                )
                outcomes[entry.slug] = ImportResult(
                    slug=entry.slug, action="updated", new_slug=None
                )

            _stash_metadata(write.item, extra=file.extra, related=file.related)
            _sync_tags(write.item, section=file.section, tags=file.tags)

        # One commit for the whole batch: a folder import is one transaction.
        # A folder with nothing to write does not commit at all, so an unchanged
        # re-import leaves the transaction exactly as it found it.
        if work:
            await db.commit()
    except Exception:
        await db.rollback()
        raise

    return [outcomes[entry.slug] for entry in plan.files]