"""Canon-aware sync run: a git repo as the source of a tenant's canon.

Design section 15 binds a corpus that lives in git to canon. This module is the
run half of that binding: it reads the checkout, parses the eligible files with
the folder importer's parser, plans them with the folder importer's planner, and
applies the plan through the canon authoring service so revisions, hashes, item
status and embedding all keep working.

Three rules shape everything here.

**A run never publishes.** It writes drafts and revisions. Publishing is a
human's decision, so ``publish_canon_page`` is never called, ``palace_run_build``
is never enqueued, and no room generation column is touched. A repo merge is
evidence that canon *may* need to change, not a ratification of it.

**Nothing is deleted.** A path that disappeared from the repo archives its page
through ``archive_canon_page``; the page, its revisions and its item all stay.

**Ownership is proven, not assumed.** ``plan_import`` decides from status and
hash, and knows nothing about who wrote the page. A page this binding created has
a ``sync_source_files`` row carrying its slug; a page with the same slug and no
such row belongs to a human, so a planned update (or a planned no-op) becomes a
``candidate_canon_edit`` artifact a person decides on, never an overwrite.

The caller owns no transaction: this function commits once at the end, and marks
the run failed with the reason on any error.
"""

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canon import CanonPage
from app.models.item import Item
from app.models.palace import (
    CandidateCurationArtifact,
    Room,
    SyncRun,
    SyncSource,
    SyncSourceFile,
)
from app.services.canon import (
    CANON_IMPORT_MAX_FILE_BYTES,
    CANON_WEBHOOK_HTTP_TIMEOUT_SECONDS,
    CANON_WEBHOOK_SECRET_BYTES,
    CANON_WEBHOOK_TOKEN_BYTES,
    CanonValidationError,
    validate_section,
)
from app.services.canon_authoring import (
    UNSET,
    CanonNotFoundError,
    archive_canon_page,
    create_canon_page,
    update_canon_page,
)
from app.services.canon_import import (
    ImportFile,
    binding_section,
    check_import_batch_shape,
    parse_import_file,
    slug_from_path,
    plan_import,
    stored_body_hash,
    validate_import_batch,
)
from app.services.curation_artifacts import _record_artifact_event
from app.services.palace import (
    SYNC_ACTIVE_STATUSES,
    _git_base_environment,
    _is_remote_repo_source,
    _iter_repo_sync_files,
    _prepare_repo_checkout,
    _repo_git_environment,
    _run_git_command,
    probe_canon_repo,
)

logger = logging.getLogger(__name__)

# A run is a machine, and the governance columns record who wrote the row. The
# same principal also created the artifact, so a reviewer sees one author.
_AUTHOR_SUBJECT = "system:canon-sync"

_CONFLICT_ARTIFACT_KIND = "candidate_canon_edit"
_REMOVED_ARTIFACT_KIND = "candidate_canon_edit"
_TARGET_RUNTIME = "palace"

# ``dedupe_key`` is String(180) and the pair below is at most 16 + 80 + 1 + 64
# characters, so a slug and a content hash always fit without truncation.
_CONFLICT_DEDUPE_PREFIX = "canon-sync-conflict"
_REMOVED_DEDUPE_PREFIX = "canon-sync-removed"


class CanonSyncInFlightError(Exception):
    """Raised when a run is already active for this binding."""

    def __init__(self, run: SyncRun) -> None:
        super().__init__(f"a sync run is already {run.status} for this binding")
        self.run = run


async def get_canon_binding(
    db: AsyncSession, *, tenant_id: str, source_id: uuid.UUID
) -> SyncSource | None:
    """One sync source this tenant owns, or ``None``.

    The tenant filter is the only thing that makes an id from a caller safe, so
    it is part of the query rather than a check afterwards.
    """
    return await db.scalar(
        select(SyncSource)
        .where(SyncSource.tenant_id == tenant_id)
        .where(SyncSource.id == source_id)
        .limit(1)
    )


async def start_canon_sync_run(
    db: AsyncSession,
    *,
    tenant_id: str,
    source: SyncSource,
    triggered_by: str = "manual",
) -> SyncRun:
    """Start one manual canon sync run for a binding this tenant owns.

    A manual sync is a human act, so every refusal has to be a sentence the
    person can act on: the source must be this tenant's canon binding, it must
    be active, no run may already be in flight, and the repository must be
    readable. The checks are read-only and run in that order, so a refused start
    leaves the database exactly as it was: no row is created until the last
    check has passed.
    """
    # 1. Another tenant's binding is indistinguishable from a missing one.
    if source.tenant_id != tenant_id:
        raise CanonValidationError("sync source not found")
    # 2. A non-canon binding has no canon to sync.
    if source.target_kind != "canon":
        raise CanonValidationError("this source is not a canon binding")
    # 3. A paused binding does not run, however it was asked.
    if source.status != "active":
        raise CanonValidationError("this binding is not active")

    # 4. One run at a time: an active run is reported, never reused or doubled.
    active = await db.scalar(
        select(SyncRun)
        .where(SyncRun.tenant_id == tenant_id)
        .where(SyncRun.sync_source_id == source.id)
        .where(SyncRun.status.in_(SYNC_ACTIVE_STATUSES))
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    if active is not None:
        raise CanonSyncInFlightError(active)

    # 5. Prove the repository is readable before a row claims a run exists. The
    # probe's 422 "repo unreachable: ..." propagates verbatim: it already names
    # the reason and already scrubs the credential out of git output.
    await probe_canon_repo(
        source.root_path, credential_ciphertext=source.credential_ciphertext
    )

    # 6. Everything passed, so the run may exist.
    run = SyncRun(
        sync_source_id=source.id,
        tenant_id=tenant_id,
        status="queued",
        triggered_by=triggered_by,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def _resolve_checkout(source: SyncSource) -> tuple[Path, str | None]:
    """The working tree this run reads, and its branch when one is meaningful.

    A remote github.com source is cloned and refreshed through the run's own git
    environment, so the same credential and the same hardening apply. A local
    path is used as it stands: it is the same repository, already checked out on
    this host, and cloning it again would only risk reading a different commit
    from the one the operator can see.
    """
    if _is_remote_repo_source(source):
        return _prepare_repo_checkout(source)
    return Path(source.root_path), None


def _head_commit_sha(source: SyncSource, checkout: Path) -> str:
    """The commit the working tree is actually sitting on."""
    if _is_remote_repo_source(source):
        with _repo_git_environment(source) as (env, _clone_url):
            sha = _run_git_command(["git", "rev-parse", "HEAD"], env=env, cwd=checkout)
    else:
        sha = _run_git_command(
            ["git", "rev-parse", "HEAD"], env=_git_base_environment(), cwd=checkout
        )
    if not sha:
        raise ValueError("Git sync failed: could not resolve the repository head")
    return sha


async def _room_id_for_slug(db: AsyncSession, *, tenant_id: str, room_slug: str) -> uuid.UUID:
    """Resolve a front matter ``room:`` slug to a room this tenant owns.

    A charter that names a room and silently lands unbound is worse than a run
    that fails, because the charter then never appears in the room it describes.
    """
    room_id = await db.scalar(
        select(Room.id).where(Room.tenant_id == tenant_id).where(Room.slug == room_slug)
    )
    if room_id is None:
        raise ValueError(f"canon file names room {room_slug!r}, which does not exist")
    return room_id


def _parse_candidates(
    checkout: Path, candidates: list
) -> tuple[list[ImportFile], list[str]]:
    """Parse every candidate, keeping a bad file out of the batch but in the run.

    One unparseable file must not cost the operator the other 499, so a parse
    error is recorded against the file and the batch continues. The cap checks
    still run over what was parsed, so a bad file cannot be used to slip a
    too-large batch past them.
    """
    files: list[ImportFile] = []
    skipped: list[str] = []
    for candidate in candidates:
        try:
            raw = (checkout / candidate.relative_path).read_bytes()
            files.append(parse_import_file(candidate.relative_path, raw))
        except Exception as exc:  # noqa: BLE001 - one file must not fail the batch
            skipped.append(f"{candidate.relative_path}: {exc}")
    return files, skipped


async def _source_file_rows(
    db: AsyncSession, *, tenant_id: str, source_id: uuid.UUID
) -> list[SyncSourceFile]:
    """Every per-path row this binding has written, in one query."""
    rows = await db.scalars(
        select(SyncSourceFile)
        .where(SyncSourceFile.tenant_id == tenant_id)
        .where(SyncSourceFile.sync_source_id == source_id)
    )
    return list(rows)


def _conflict_dedupe_key(slug: str, content_hash: str) -> str:
    """Stable for one page at one file content: a rerun files nothing new."""
    return f"{_CONFLICT_DEDUPE_PREFIX}:{slug}:{content_hash}"


def _removed_dedupe_key(slug: str, content_hash: str | None) -> str:
    return f"{_REMOVED_DEDUPE_PREFIX}:{slug}:{content_hash or 'unknown'}"


def _conflict_body(
    *,
    path: str,
    slug: str,
    commit_sha: str,
    plan_reason: str,
    file_hash: str,
    stored_hash: str | None,
    owned: bool,
) -> str:
    """Plain sentences naming the file, the commit, and what differs."""
    if owned:
        ownership = (
            "This binding created the page, but the plan refused to apply the change "
            "automatically."
        )
    else:
        ownership = (
            "This binding did not create the page, so a person owns it and the run "
            "will not overwrite it."
        )
    if stored_hash is None:
        body_state = "The stored body hash is unknown."
    elif stored_hash == file_hash:
        body_state = "The stored body already matches the file byte for byte."
    else:
        body_state = (
            f"The stored body hash is {stored_hash} and the file's is {file_hash}, "
            "so the bodies differ."
        )
    return "\n".join(
        (
            f"Repo file {path!r} at commit {commit_sha} wants to change canon page "
            f"canon:{slug}.",
            f"Plan action: {plan_reason}",
            body_state,
            ownership,
        )
    )


async def _file_artifact(
    db: AsyncSession,
    *,
    tenant_id: str,
    artifact_kind: str,
    slug: str,
    dedupe_key: str,
    candidate_body: str,
    evidence_diff: dict,
    metadata: dict,
    item_id: uuid.UUID | None,
) -> bool:
    """File one reviewable artifact; a rerun with the same key is a quiet no-op."""
    existing = await db.scalar(
        select(CandidateCurationArtifact.id).where(
            CandidateCurationArtifact.tenant_id == tenant_id,
            CandidateCurationArtifact.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return False

    item_ids = [str(item_id)] if item_id is not None else []
    artifact = CandidateCurationArtifact(
        tenant_id=tenant_id,
        artifact_kind=artifact_kind,
        target_runtime=_TARGET_RUNTIME,
        target_surface=f"canon:{slug}",
        status="reviewable",
        source_item_ids=item_ids,
        source_digests={},
        affected_item_ids=item_ids,
        affected_claim_ids=[],
        evidence_diff=evidence_diff,
        dedupe_key=dedupe_key,
        candidate_body=candidate_body,
        privacy_review={
            "safe_for_review": True,
            "raw_sensitive_content_excluded": True,
            "contains_sensitive_content": False,
            "method": "deterministic_canon_sync_v1",
        },
        eval_summary={
            "detector": "deterministic_canon_sync_v1",
            "llm_used": False,
        },
        approval={},
        created_by_principal=_AUTHOR_SUBJECT,
        metadata_=metadata,
    )
    try:
        # A savepoint, not a commit: the run owns the transaction, and a lost
        # race on the dedupe key must not take the rest of the batch down.
        async with db.begin_nested():
            db.add(artifact)
            await db.flush()
            _record_artifact_event(
                db,
                artifact=artifact,
                event_type="canon_sync_artifact_created",
                previous_snapshot=None,
            )
            await db.flush()
    except IntegrityError:
        logger.info(
            "canon_sync_artifact_deduplicated tenant_id=%s dedupe_key=%s",
            tenant_id,
            dedupe_key,
        )
        return False
    return True


def _artifact_metadata(*, source: SyncSource, path: str, commit_sha: str, proposal: str) -> dict:
    return {
        "proposal_type": proposal,
        "sync_source_id": str(source.id),
        "relative_path": path,
        "commit_sha": commit_sha,
        "review_inbox": {"resolved": False},
    }


async def run_canon_sync(
    db: AsyncSession,
    *,
    run: SyncRun,
    source: SyncSource,
    embedder,
    llm,
) -> tuple[str, str | None]:
    """Run one canon-bound sync: checkout, parse, plan, apply, archive, record.

    Returns ``("completed", None)`` or ``("failed", reason)``, exactly like
    ``run_sync_run``. Writes canon drafts and revisions only: never a publish,
    never a build, never a generation bump, never a delete.
    """
    run_id, source_id, tenant_id = run.id, source.id, run.tenant_id
    try:
        checkout, branch = _resolve_checkout(source)
        commit_sha = _head_commit_sha(source, checkout)
        run.commit_sha = commit_sha

        candidates = _iter_repo_sync_files(
            checkout,
            source=source,
            branch=branch,
            allowed_extensions=source.allowed_extensions,
        )
        files, skipped = _parse_candidates(checkout, candidates)
        # A candidate that produced no ImportFile failed to parse. Its path is
        # recorded against its row below, if the binding already has one.
        parsed_paths = {file.path for file in files}
        failed_paths = [
            candidate.relative_path
            for candidate in candidates
            if candidate.relative_path not in parsed_paths
        ]

        run.files_seen = len(candidates)
        run.files_skipped = len(skipped)
        if skipped:
            # Visible in run history rather than only in a worker log; a skipped
            # file is the operator's business, not an implementation detail.
            run.error_message = ("skipped files: " + "; ".join(skipped))[:2000]

        # The import caps are the run caps, and they are enforced on the whole
        # batch: over a cap the run refuses, it does not import a slice.
        check_import_batch_shape(
            file_count=len(files), total_bytes=sum(file.size_bytes for file in files)
        )
        validate_import_batch(files)

        # Validated before the first write, so a bad binding fails the run with
        # a reason instead of half-importing and then refusing.
        section_default = validate_section(source.canon_section_default or "reference")

        plan = await plan_import(db, tenant_id=tenant_id, files=files)
        by_slug = {file.slug: file for file in files}
        by_path = {candidate.relative_path: candidate for candidate in candidates}

        rows = await _source_file_rows(db, tenant_id=tenant_id, source_id=source_id)
        rows_by_path = {row.relative_path: row for row in rows}
        owned_slugs = {row.page_slug for row in rows if row.page_slug}

        # A file that failed to parse is not in the plan, so it is recorded here:
        # the run saw the path and did nothing with it. A row that does not exist
        # yet is not created, and an existing row keeps its last_error verbatim.
        for failed_path in failed_paths:
            failed_row = rows_by_path.get(failed_path)
            if failed_row is not None:
                failed_row.last_action = "error"
                failed_row.last_seen_run_id = run_id

        created = updated = conflicted = archived = 0
        changed = 0

        for entry in plan.files:
            file = by_slug[entry.slug]
            candidate = by_path[entry.path]
            row = rows_by_path.get(entry.path)

            action = entry.action
            # A row for this path proves the binding created the page, even after
            # a deletion cleared page_slug: that is how a returning file revives
            # its page instead of being treated as a stranger's.
            owned = entry.slug in owned_slugs or row is not None
            # plan_import knows status and hash, not authorship. A page this
            # binding did not create is a human's, so nothing that would touch
            # it may be applied by a machine.
            if action != "create" and not owned:
                action = "conflict"


            if action == "create":
                write = await create_canon_page(
                    db,
                    tenant_id=tenant_id,
                    author_subject=_AUTHOR_SUBJECT,
                    slug=entry.slug,
                    title=file.title,
                    summary=file.summary,
                    section=binding_section(file, default=section_default),
                    body=file.body,
                    room_id=(
                        await _room_id_for_slug(db, tenant_id=tenant_id, room_slug=file.room_slug)
                        if file.room_slug
                        else source.canon_room_id
                    ),
                    tags=file.tags,
                    commit=False,
                )
                created += 1
                changed += 1
            elif action == "update":
                write = await update_canon_page(
                    db,
                    tenant_id=tenant_id,
                    author_subject=_AUTHOR_SUBJECT,
                    slug=entry.slug,
                    title=file.title,
                    summary=file.summary,
                    section=binding_section(file, default=section_default),
                    body=file.body,
                    # A file with no room in its front matter keeps the room the
                    # page already had: a repo must not unbind a page by omission.
                    room_id=(
                        await _room_id_for_slug(db, tenant_id=tenant_id, room_slug=file.room_slug)
                        if file.room_slug
                        else UNSET
                    ),
                    change_note=f"repo sync {commit_sha[:12]}",
                    embedder=embedder,
                    llm=llm,
                    commit=False,
                )
                if write.page.status == "archived":
                    write.page.status = "draft"
                updated += 1
                changed += 1
            elif action == "conflict":
                stored_hash = await stored_body_hash(db, tenant_id=tenant_id, slug=entry.slug)
                await _file_artifact(
                    db,
                    tenant_id=tenant_id,
                    artifact_kind=_CONFLICT_ARTIFACT_KIND,
                    slug=entry.slug,
                    dedupe_key=_conflict_dedupe_key(entry.slug, entry.content_hash),
                    candidate_body=_conflict_body(
                        path=entry.path,
                        slug=entry.slug,
                        commit_sha=commit_sha,
                        plan_reason=entry.reason,
                        file_hash=entry.content_hash,
                        stored_hash=stored_hash,
                        owned=owned,
                    ),
                    evidence_diff={
                        "format": "canon_sync_conflict_v1",
                        "relative_path": entry.path,
                        "commit_sha": commit_sha,
                        "plan_action": entry.action,
                        "file_content_hash": entry.content_hash,
                        "stored_content_hash": stored_hash,
                        "binding_owned": owned,
                    },
                    metadata=_artifact_metadata(
                        source=source,
                        path=entry.path,
                        commit_sha=commit_sha,
                        proposal="canon_sync_conflict",
                    ),
                    item_id=row.item_id if row is not None else None,
                )
                conflicted += 1
            # ``skip-unchanged`` performs no write at all.

            if row is None:
                row = SyncSourceFile(
                    sync_source_id=source_id,
                    tenant_id=tenant_id,
                    relative_path=entry.path,
                )
                db.add(row)
                rows_by_path[entry.path] = row
            row.content_hash = file.content_hash
            row.file_size = file.size_bytes
            row.source_fingerprint = candidate.source_fingerprint
            row.modified_ns = candidate.modified_ns
            row.status = "active"
            row.last_seen_run_id = run_id
            # ``action`` is already one of create / update / conflict /
            # skip-unchanged: the plan's four outcomes, in the same words.
            row.last_action = action
            row.last_error = None
            if action in ("create", "update") or (action == "skip-unchanged" and owned):
                row.page_slug = entry.slug
            if action in ("create", "update"):
                row.item_id = write.item.id

        # --- deletions -----------------------------------------------------
        # A path that left the repo archives its page and files a question. The
        # page, its revisions and its item all survive; only the row's claim on
        # the slug is dropped, so a page that comes back is a fresh create.
        seen_paths = set(by_path)
        for row in rows:
            if row.relative_path in seen_paths:
                continue
            slug = row.page_slug
            if slug:
                try:
                    await archive_canon_page(
                        db,
                        tenant_id=tenant_id,
                        author_subject=_AUTHOR_SUBJECT,
                        slug=slug,
                    )
                except CanonNotFoundError:
                    # The page was already removed out of band; there is nothing
                    # left to archive and nothing worth failing a run over.
                    logger.info(
                        "canon_sync_archive_missing_page tenant_id=%s slug=%s", tenant_id, slug
                    )
                else:
                    archived += 1
                await _file_artifact(
                    db,
                    tenant_id=tenant_id,
                    artifact_kind=_REMOVED_ARTIFACT_KIND,
                    slug=slug,
                    dedupe_key=_removed_dedupe_key(slug, row.content_hash),
                    candidate_body="\n".join(
                        (
                            f"Repo file {row.relative_path!r} is gone at commit {commit_sha}.",
                            f"Canon page canon:{slug} was archived; nothing was deleted, and "
                            "it can be published again if the file comes back.",
                        )
                    ),
                    evidence_diff={
                        "format": "canon_sync_removed_v1",
                        "relative_path": row.relative_path,
                        "commit_sha": commit_sha,
                        "last_content_hash": row.content_hash,
                    },
                    metadata=_artifact_metadata(
                        source=source,
                        path=row.relative_path,
                        commit_sha=commit_sha,
                        proposal="canon_sync_removed",
                    ),
                    item_id=row.item_id,
                )
                row.page_slug = None
            row.status = "deleted"
            row.last_seen_run_id = run_id
            # The path left the repo whether or not its page still existed.
            row.last_action = "archive"

        now = datetime.now(timezone.utc)
        run.files_changed = changed
        run.pages_created = created
        run.pages_updated = updated
        run.pages_archived = archived
        run.pages_conflicted = conflicted
        run.status = "completed"
        run.completed_at = now
        source.last_synced_at = now
        source.last_error = None
        await db.commit()
        return "completed", None
    except Exception as exc:  # noqa: BLE001 - a run reports, it does not raise
        await db.rollback()
        logger.exception("canon sync run %s failed: %s", run_id, exc)
        reason = str(exc)[:500] or exc.__class__.__name__
        run = await db.get(SyncRun, run_id)
        source = await db.get(SyncSource, source_id)
        if run is not None:
            run.status = "failed"
            run.error_message = reason
            run.completed_at = datetime.now(timezone.utc)
        if source is not None:
            source.last_error = reason
        await db.commit()
        return "failed", str(exc)


# --- reading and settling one conflict -------------------------------------
# A conflict is a question a person answers. Reading it shows both sides without
# writing anything; accepting it is the answer "the repo's text wins". Neither
# publishes: accepting a file is not vouching for what it says.

_NO_PAGE_MESSAGE = "no page is bound to this file"
_FILE_GONE_MESSAGE = "the file is gone from the repo"


def _checkout_relative_file(checkout: Path, relative_path: str) -> Path:
    """The file inside the checkout, or a refusal that names no host path.

    ``relative_path`` arrives from a caller, so it may only address something
    inside this binding's working tree. ``resolve`` is used on both sides so a
    symlink cannot walk out of the checkout either.
    """
    root = checkout.resolve()
    target = (root / relative_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise CanonValidationError(_NO_PAGE_MESSAGE) from None
    return target


def _read_repo_side(
    target: Path, relative_path: str
) -> tuple[ImportFile | None, str, str | None, bool]:
    """``(parsed file, body text, content hash, truncated)``, never raising.

    Bad bytes are replaced rather than refused: a preview that raises on a
    mangled file shows the reviewer nothing, which is worse than showing the
    file with its damage visible. The text is capped at the import cap, because
    the same ceiling governs what a run would ever import from this file.
    """
    text = target.read_bytes().decode("utf-8", errors="replace")
    truncated = len(text) > CANON_IMPORT_MAX_FILE_BYTES
    if truncated:
        text = text[:CANON_IMPORT_MAX_FILE_BYTES]
    try:
        # Re-encoded from the replaced text so the parser cannot fail on the same
        # bytes the reader just tolerated.
        parsed = parse_import_file(relative_path, text.encode("utf-8"))
    except CanonValidationError:
        return None, text, None, truncated
    return parsed, parsed.body, parsed.content_hash, truncated


async def _page_side(
    db: AsyncSession, *, tenant_id: str, slug: str
) -> tuple[str, str]:
    """``(status, body)`` of one canon page, or a refusal.

    The body lives on the governing item, which is what retrieval reads.
    """
    row = (
        await db.execute(
            select(CanonPage.status, Item.raw_content)
            .join(Item, Item.id == CanonPage.item_id)
            .where(CanonPage.tenant_id == tenant_id)
            .where(CanonPage.slug == slug)
            .limit(1)
        )
    ).first()
    if row is None:
        raise CanonValidationError(_NO_PAGE_MESSAGE)
    status, raw_content = row
    return status, raw_content or ""


async def read_conflict_pair(
    db: AsyncSession, *, tenant_id: str, source: SyncSource, relative_path: str
) -> dict:
    """Both sides of one conflict, read live and stored nowhere.

    The repo side is the file as it stands in the checkout; the page side is the
    body canon currently holds. A file that is gone from the repo is a fact the
    caller has to see, not an error, so it comes back as empty text with
    ``missing`` set: the page still exists and still needs a decision.
    """
    checkout, _branch = _resolve_checkout(source)
    # The path is checked against the checkout before anything else touches the
    # repository: a refusal must not depend on the repo being readable.
    target = _checkout_relative_file(checkout, relative_path)
    commit_sha = _head_commit_sha(source, checkout)

    # The path's own row is what knows the slug when the file is gone: the slug
    # is derived from the filename, but there is no file left to derive it from.
    row = await db.scalar(
        select(SyncSourceFile)
        .where(SyncSourceFile.tenant_id == tenant_id)
        .where(SyncSourceFile.sync_source_id == source.id)
        .where(SyncSourceFile.relative_path == relative_path)
        .limit(1)
    )

    # ``parse_import_file`` always derives the slug from the filename, so a path
    # with no file left still names its slug the same way a run would have.
    path_slug = slug_from_path(relative_path) or None
    row_slug = row.page_slug if row is not None else None

    missing = not target.is_file()
    if missing:
        # A gone file has no text to show; the page still needs its decision.
        repo_text, file_hash, truncated = "", None, False
        slug = path_slug or row_slug
    else:
        parsed, repo_text, file_hash, truncated = _read_repo_side(target, relative_path)
        # The slug comes from the file's own front matter and name, never from
        # the row: a file that renamed its slug must not be shown under the old.
        slug = parsed.slug if parsed is not None else (path_slug or row_slug)

    if slug is None:
        raise CanonValidationError(_NO_PAGE_MESSAGE)

    page_status, page_text = await _page_side(db, tenant_id=tenant_id, slug=slug)
    return {
        "path": relative_path,
        "slug": slug,
        "repo_text": repo_text,
        "page_text": page_text,
        "page_status": page_status,
        "commit_sha": commit_sha,
        "file_hash": file_hash,
        "missing": missing,
        "truncated": truncated,
    }


async def accept_repo_version(
    db: AsyncSession,
    *,
    tenant_id: str,
    source: SyncSource,
    relative_path: str,
    actor_subject: str,
) -> CanonPage:
    """The human decision that the repo's text wins for this page.

    The page returns to ``draft``: the body now comes from the file, and nothing
    about that is a publication. Ownership moves to this binding so the next run
    treats the page as its own instead of asking again.
    """
    pair = await read_conflict_pair(
        db, tenant_id=tenant_id, source=source, relative_path=relative_path
    )
    if pair["missing"]:
        raise CanonValidationError(_FILE_GONE_MESSAGE)

    slug = pair["slug"]
    write = await update_canon_page(
        db,
        tenant_id=tenant_id,
        author_subject=actor_subject,
        slug=slug,
        body=pair["repo_text"],
        change_note=f"Accepted the repo version of {relative_path}",
        commit=False,
    )
    # ``update_canon_page`` already returns the page to draft; the item is set
    # here because an unchanged body would otherwise leave it ``ready`` and let a
    # page that is no longer published be quoted.
    write.item.status = "processing"
    await db.commit()

    # Ownership: the next run reads this row, sees the slug and the hash it just
    # accepted, and plans a no-op instead of filing the same conflict again.
    row = await db.scalar(
        select(SyncSourceFile)
        .where(SyncSourceFile.tenant_id == tenant_id)
        .where(SyncSourceFile.sync_source_id == source.id)
        .where(SyncSourceFile.relative_path == relative_path)
        .limit(1)
    )
    if row is None:
        row = SyncSourceFile(
            sync_source_id=source.id,
            tenant_id=tenant_id,
            relative_path=relative_path,
        )
        db.add(row)
    row.page_slug = slug
    row.content_hash = pair["file_hash"]
    await db.commit()

    # ``ck_candidate_curation_artifact_status`` has no ``resolved`` value
    # (draft, needs_source, reviewable, promoted, proposed, approved, rejected,
    # stale, deprecated, superseded), so the open conflict artifact is left as it
    # stands: the decision is recorded on the page and the binding's row, not by
    # inventing a status the schema does not accept.

    page = await db.scalar(
        select(CanonPage)
        .where(CanonPage.tenant_id == tenant_id)
        .where(CanonPage.slug == slug)
        .limit(1)
    )
    if page is None:  # pragma: no cover - the write above created or updated it
        raise CanonValidationError(_NO_PAGE_MESSAGE)
    return page


# --- push hooks -------------------------------------------------------------
#
# A binding can be told to sync by the repository instead of by the clock. The
# hook is not an authoring path and not a second principal: it only says "the
# default branch moved", and the run it starts is the same run the poller would
# have started, with the same rules. Publication is still a person's decision.

_GITHUB_SIGNATURE_PREFIX = "sha256="


def verify_github_signature(*, secret: str, body: bytes, header: str | None) -> bool:
    """True when ``header`` is the HMAC of ``body`` under ``secret``.

    Compared in constant time. A missing or malformed header is a refusal, never
    a pass: an unauthenticated push hook would let anyone start runs.
    """
    if not header or not header.startswith(_GITHUB_SIGNATURE_PREFIX):
        return False
    received = header[len(_GITHUB_SIGNATURE_PREFIX):].strip().lower()
    if len(received) != 64:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


async def binding_for_webhook(db: AsyncSession, *, token: str) -> SyncSource | None:
    """The binding that answers to ``token``, or ``None``.

    No tenant is known before the signature is checked, so the lookup is by token
    alone. The token is random and unique, and the caller still refuses the
    request unless the body signs correctly.
    """
    if not token or len(token) > 128:
        return None
    return await db.scalar(select(SyncSource).where(SyncSource.webhook_token == token))


async def enable_push_webhook(
    db: AsyncSession, *, source: SyncSource
) -> tuple[str, str]:
    """Give a binding a hook token and signing secret. Returns both once.

    Rotating is the same call: a new token and a new secret replace the old pair,
    so an old URL stops answering.
    """
    from app.services.palace import _encrypt_repo_credential

    token = secrets.token_urlsafe(CANON_WEBHOOK_TOKEN_BYTES)
    secret = secrets.token_urlsafe(CANON_WEBHOOK_SECRET_BYTES)
    source.webhook_token = token
    source.webhook_secret_ciphertext = _encrypt_repo_credential(secret)
    await db.commit()
    await db.refresh(source)
    return token, secret


async def disable_push_webhook(db: AsyncSession, *, source: SyncSource) -> None:
    """Remove the hook. The binding keeps syncing on its interval."""
    source.webhook_token = None
    source.webhook_secret_ciphertext = None
    await db.commit()
    await db.refresh(source)


def webhook_secret(source: SyncSource) -> str:
    from app.services.palace import _decrypt_repo_credential

    if not source.webhook_secret_ciphertext:
        raise CanonValidationError("this binding has no push hook")
    return _decrypt_repo_credential(source.webhook_secret_ciphertext)


_GITHUB_API_BASE = "https://api.github.com"


async def register_github_webhook(
    source: SyncSource, *, hook_url: str, secret: str
) -> bool:
    """Ask GitHub to deliver pushes for this repository to ``hook_url``.

    A person pressed the button; this only saves them the copy and paste, and it
    keeps the signing secret off their clipboard and out of their browser. Any
    refusal (no credential, no admin right on the repo, a network error) returns
    False and the caller falls back to showing the manual instructions. It never
    raises: a hook that could not be installed is not a failed binding.
    """
    import httpx

    from app.services.palace import _github_repo_parts, repo_credential_secret

    if not _is_remote_repo_source(source):
        return False
    token = repo_credential_secret(source)
    if not token:
        return False
    try:
        owner, repo = _github_repo_parts(source.root_path)
    except Exception:
        return False

    payload = {
        "name": "web",
        "active": True,
        "events": ["push"],
        "config": {
            "url": hook_url,
            "content_type": "json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }
    try:
        async with httpx.AsyncClient(timeout=CANON_WEBHOOK_HTTP_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/hooks",
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("canon push hook: could not reach GitHub: %s", exc)
        return False
    if response.status_code not in (200, 201):
        logger.warning(
            "canon push hook: GitHub refused the hook for %s/%s: %s %s",
            owner,
            repo,
            response.status_code,
            response.text[:200],
        )
        return False
    return True


async def unregister_github_webhook(source: SyncSource, *, hook_url: str) -> bool:
    """Delete the hook this Palace created, matched by its URL.

    Matched by URL rather than a stored id so no column is needed, and so a hook a
    person added by hand is never removed by accident.
    """
    import httpx

    from app.services.palace import _github_repo_parts, repo_credential_secret

    if not _is_remote_repo_source(source):
        return False
    token = repo_credential_secret(source)
    if not token:
        return False
    try:
        owner, repo = _github_repo_parts(source.root_path)
    except Exception:
        return False

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=CANON_WEBHOOK_HTTP_TIMEOUT_SECONDS) as client:
            listing = await client.get(
                f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/hooks", headers=headers
            )
            if listing.status_code != 200:
                return False
            for hook in listing.json():
                if (hook.get("config") or {}).get("url") == hook_url:
                    await client.delete(
                        f"{_GITHUB_API_BASE}/repos/{owner}/{repo}/hooks/{hook['id']}",
                        headers=headers,
                    )
                    return True
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("canon push hook: could not reach GitHub: %s", exc)
    return False
