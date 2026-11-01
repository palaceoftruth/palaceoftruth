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

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canon import CanonPage
from app.models.palace import (
    CandidateCurationArtifact,
    Room,
    SyncRun,
    SyncSource,
    SyncSourceFile,
)
from app.services.canon import validate_section
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
    plan_import,
    stored_body_hash,
    validate_import_batch,
)
from app.services.curation_artifacts import _record_artifact_event
from app.services.palace import (
    _git_base_environment,
    _is_remote_repo_source,
    _iter_repo_sync_files,
    _prepare_repo_checkout,
    _repo_git_environment,
    _run_git_command,
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
