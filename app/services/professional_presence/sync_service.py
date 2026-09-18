"""Read a connected provider account and turn it into evidence the rest of the app can use.

This is the module that makes external_evidence_items and external_profile_snapshots mean
something. Before it, both tables existed and were covered by account deletion, but nothing ever
wrote a row to either.

Three properties are worth stating up front because every design choice below serves one of them.

IDEMPOTENT. Syncing twice observes the same language on the same repository twice. The unique
constraint on (connection_id, skill, signal_kind, source_ref) is what makes the second write a
no-op rather than a duplicate, and duplicates would not merely be untidy: the Evidence Engine
counts breadth in *distinct sources*, so multiplying rows would inflate a student's confidence
on nothing more than a refresh.

A FAILED SYNC DESTROYS NOTHING. `last_synced_at` advances only on success, evidence is never
cleared on failure, and the previous snapshot stays exactly where it was. A provider outage must
read as "GitHub could not be reached", never as "this student has no evidence" -- the second is
a claim about the student made from a fact about our infrastructure.

NOTHING IS INVENTED. Every evidence row traces to something GitHub measured or the student typed
on their own repository, and which of the two it was survives in `signal_kind` so that the
Evidence Engine can weigh them differently. There is no score column to put a made-up number in.
"""

import datetime
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import token_crypto
from app.models.external_evidence_item import (
    SIGNAL_REPO_LANGUAGE,
    SIGNAL_REPO_PRIMARY_LANGUAGE,
    SIGNAL_REPO_TOPIC,
    ExternalEvidenceItem,
)
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.provider_connection import (
    STATUS_ACTIVE,
    STATUS_NEEDS_REAUTH,
    SYNC_FAILED,
    SYNC_OK,
    ProviderConnection,
)
from app.services.professional_presence.errors import (
    ProviderError,
    ProviderUnauthorized,
)
from app.services.professional_presence.providers import github as github_provider
from app.services.professional_presence.providers.base import ProviderFetch, ProviderRepository

logger = logging.getLogger(__name__)

# The snapshot payload's shape, stored alongside the data so a later reader can tell which
# version of this module wrote a historical row rather than guessing from its keys.
SNAPSHOT_SCHEMA_VERSION = 1

# A repository with no measured language and no topics carries no skill signal at all. Forks and
# archives are excluded from *evidence* for different reasons: a fork is somebody else's code
# until proven otherwise, and attributing it would be the "infer project ownership" failure the
# specification rules out; an archive is explicitly finished work, which is fine as history but
# should not be presented as current capability. Both still appear in the snapshot, because the
# audit legitimately has things to say about them.
_EVIDENCE_EXCLUDED = ("fork", "archived")


class ConnectionNotFoundError(Exception):
    """No live connection for this (profile, provider) -- nothing to sync."""


class ConnectionNotSyncableError(Exception):
    """A live connection exists but holds no usable credential.

    Distinct from "not connected": the row is there and the student can see it, but our copy of
    the token is gone or unreadable, so the honest instruction is "reconnect" rather than
    "connect".
    """


@dataclass(frozen=True)
class SyncResult:
    """What a sync actually changed, for the caller and for the logs.

    `snapshot_written` is False on the common path. Opening the screen and re-syncing an account
    that has not changed costs no snapshot row at all -- see `_write_snapshot_if_changed`.
    """

    provider: str
    evidence_added: int
    evidence_removed: int
    repositories_seen: int
    snapshot_written: bool
    complete: bool


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _adapter(provider_key: str):
    return github_provider.get_provider(provider_key)


def live_connection(db: Session, profile_id: int, provider_key: str) -> Optional[ProviderConnection]:
    """The one un-revoked connection for this profile and provider, if any.

    Duplicated deliberately rather than imported from connection_service: that module's copy is
    private to its own flow, and a sync reading its neighbour's private helper would couple two
    services that are otherwise free to change independently.
    """
    return (
        db.query(ProviderConnection)
        .filter(
            ProviderConnection.profile_id == profile_id,
            ProviderConnection.provider == provider_key,
            ProviderConnection.revoked_at.is_(None),
        )
        .one_or_none()
    )


def sync(db: Session, profile_id: int, provider_key: str, ai_provider=None) -> SyncResult:
    """Fetch, normalise and store. Raises ProviderError with a category on provider failure.

    The failure path is as much the point of this function as the success path. Whatever goes
    wrong, the connection row is left describing what happened -- `sync_status`, `last_error_kind`
    and, for a rejected credential, `status='needs_reauth'` -- and the evidence the student
    already had is left untouched.
    """
    connection = live_connection(db, profile_id, provider_key)
    if connection is None:
        raise ConnectionNotFoundError()
    if not connection.access_token_encrypted:
        raise ConnectionNotSyncableError()

    adapter = _adapter(provider_key)
    if adapter is None:
        raise ConnectionNotFoundError()

    try:
        access_token = token_crypto.decrypt(connection.access_token_encrypted)
    except token_crypto.TokenEncryptionError:
        # The stored credential cannot be read back -- a rotated or misconfigured key. This is
        # our failure, not the provider's, and not something a retry fixes, so the connection is
        # marked as needing reconnection rather than as a provider outage.
        _record_failure(db, connection, ProviderUnauthorized())
        raise ConnectionNotSyncableError()

    try:
        fetched = adapter.fetch_account(access_token)
    except ProviderError as exc:
        _record_failure(db, connection, exc)
        raise

    # The account behind the token must still be the account that was connected. A token that
    # begins returning a different id is not a refreshed grant, it is a different one, and
    # continuing would file one person's repositories under another person's profile.
    if fetched.profile.provider_user_id != connection.provider_user_id:
        logger.warning(
            "provider=%s event=sync_rejected reason=identity_changed connection_id=%s",
            provider_key,
            connection.id,
        )
        _record_failure(db, connection, ProviderUnauthorized())
        raise ConnectionNotSyncableError()

    added, removed = _reconcile_evidence(db, connection, fetched)
    snapshot = _write_snapshot_if_changed(db, connection, fetched)
    snapshot_written = snapshot is not None

    if snapshot is not None and ai_provider is not None:
        # Generated only when the account's content actually changed. Re-syncing an unchanged
        # profile costs no AI call at all, and the prose already stored against the previous
        # snapshot is still the prose for the same facts.
        _attach_narrative(db, profile_id, snapshot, ai_provider)

    # Display name only, and refreshed on every sync because a GitHub login can be renamed by
    # its owner. Ownership stays keyed on provider_user_id, which cannot be renamed.
    connection.provider_username = fetched.profile.username or connection.provider_username
    connection.sync_status = SYNC_OK
    connection.last_synced_at = _now()
    connection.last_error_kind = None
    if connection.status == STATUS_NEEDS_REAUTH:
        # A successful call proves the credential works again.
        connection.status = STATUS_ACTIVE
    db.commit()

    return SyncResult(
        provider=provider_key,
        evidence_added=added,
        evidence_removed=removed,
        repositories_seen=len(fetched.repositories),
        snapshot_written=snapshot_written,
        complete=fetched.complete,
    )


def _record_failure(db: Session, connection: ProviderConnection, exc: ProviderError) -> None:
    """Write down what went wrong without disturbing what the student already has.

    `last_synced_at` deliberately does not move: it means "when this data was last known good",
    and advancing it on a failure would put "synced 2 minutes ago" beside weeks-old evidence.
    """
    connection.sync_status = SYNC_FAILED
    connection.last_error_kind = exc.kind
    if isinstance(exc, ProviderUnauthorized):
        # The credential itself was rejected -- revoked on GitHub's settings page, most likely.
        # That needs "reconnect GitHub", which is a different instruction from "try again".
        connection.status = STATUS_NEEDS_REAUTH
    db.commit()


def _evidence_signals(repo: ProviderRepository) -> List[Tuple[str, str]]:
    """Every (skill, signal_kind) pair one repository establishes.

    The primary language is separated from the rest because it is the strongest thing a
    repository says: it is the language most of the actual bytes are written in. A topic is kept
    too, but under its own kind, because a topic is typed by the owner and must not be weighed
    like a measurement.
    """
    signals: List[Tuple[str, str]] = []
    if repo.primary_language:
        signals.append((repo.primary_language, SIGNAL_REPO_PRIMARY_LANGUAGE))
    for language in repo.languages:
        if language and language != repo.primary_language:
            signals.append((language, SIGNAL_REPO_LANGUAGE))
    for topic in repo.topics:
        signals.append((topic, SIGNAL_REPO_TOPIC))
    return signals


def _is_evidence_bearing(repo: ProviderRepository) -> bool:
    return not repo.is_fork and not repo.is_archived


def _reconcile_evidence(
    db: Session, connection: ProviderConnection, fetched: ProviderFetch
) -> Tuple[int, int]:
    """Bring stored evidence into line with what the provider reports now.

    Reconciliation only ever *removes* rows for a repository we can see has gone, and only when
    the listing was exhausted. A student with more repositories than the page cap would
    otherwise have genuine evidence deleted every sync simply because we stopped reading.

    Nothing here invents a current fact about a vanished repository. A repository that is deleted
    or made private stops being evidence; it does not become a claim that the student never wrote
    it, and no replacement row is written in its place.
    """
    existing = {
        (row.skill, row.signal_kind, row.source_ref): row
        for row in db.query(ExternalEvidenceItem)
        .filter(ExternalEvidenceItem.connection_id == connection.id)
        .all()
    }

    seen: Set[Tuple[str, str, str]] = set()
    added = 0

    for repo in fetched.repositories:
        if not _is_evidence_bearing(repo):
            continue
        source_ref = f"github:repo:{repo.full_name}"
        # `pushed_at` is when the work was last touched, which is what staleness must measure.
        # Falling back to now() would make every repository look freshly worked on, turning an
        # abandoned project into current capability.
        observed_at = repo.pushed_at or connection.created_at
        detail = {"repository": repo.full_name}
        if repo.description:
            detail["description"] = repo.description[:200]

        for skill, signal_kind in _evidence_signals(repo):
            key = (skill, signal_kind, source_ref)
            if key in seen:
                continue
            seen.add(key)
            row = existing.get(key)
            if row is not None:
                # Same signal, possibly newer work. Refreshing observed_at is the one honest
                # update: the repository really was pushed to again.
                row.observed_at = observed_at
                row.detail = detail
                continue
            db.add(
                ExternalEvidenceItem(
                    connection_id=connection.id,
                    skill=skill,
                    signal_kind=signal_kind,
                    source_ref=source_ref,
                    observed_at=observed_at,
                    detail=detail,
                )
            )
            added += 1

    removed = 0
    if fetched.complete:
        live_refs = {
            f"github:repo:{repo.full_name}"
            for repo in fetched.repositories
            if _is_evidence_bearing(repo)
        }
        for key, row in existing.items():
            if key in seen:
                continue
            # Only drop rows belonging to a repository that is genuinely no longer present or no
            # longer evidence-bearing. A repository still in the listing that merely stopped
            # reporting one language keeps its other rows.
            if row.source_ref not in live_refs:
                db.delete(row)
                removed += 1

    try:
        db.flush()
    except IntegrityError:
        # Two syncs for one connection raced. The unique constraint is the authority and it has
        # already kept the table correct; this sync simply loses the write it duplicated.
        db.rollback()
        logger.info("provider=%s event=sync_evidence_raced connection_id=%s", connection.provider, connection.id)
        return 0, 0

    return added, removed


def _snapshot_content(fetched: ProviderFetch) -> Dict:
    """The material facts of an account, in the order they will be hashed.

    This is what `fingerprint` is computed over, and it deliberately excludes anything that
    changes without the student doing anything -- follower counts and star counts move on their
    own, and including them would write a new history row every sync for activity that is not
    the student's work. Stars are still *stored* on the snapshot for the audit to read; they are
    simply not part of the identity of a snapshot.
    """
    return {
        "profile": {
            "username": fetched.profile.username,
            "name": fetched.profile.name,
            "bio": fetched.profile.bio,
            "company": fetched.profile.company,
            "location": fetched.profile.location,
            "blog": fetched.profile.blog,
            "public_repos": fetched.profile.public_repos,
        },
        "repositories": [
            {
                "full_name": repo.full_name,
                "description": repo.description,
                "primary_language": repo.primary_language,
                "languages": sorted(repo.languages),
                "topics": sorted(repo.topics),
                "is_fork": repo.is_fork,
                "is_archived": repo.is_archived,
                "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
            }
            for repo in sorted(fetched.repositories, key=lambda r: r.full_name)
        ],
    }


def fingerprint_of(content: Dict) -> str:
    """sha256 over a canonical rendering, so key order can never produce a false change."""
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _attach_narrative(db: Session, profile_id: int, snapshot: ExternalProfileSnapshot, ai_provider) -> None:
    """Write the generated prose onto the snapshot that produced it.

    Imported here rather than at module scope: audit_service reads snapshots that this module
    writes, so a top-level import in both directions would be a cycle. The dependency is real
    and one-directional at runtime -- sync produces, audit consumes -- and keeping the import
    local is what lets that stay true.

    A failure is swallowed. The snapshot and its evidence are already committed and are the
    valuable part; losing the prose costs a sentence per finding, while raising would fail a
    sync that has, in every way that matters, already succeeded.
    """
    from app.services.professional_presence import audit_service

    try:
        observations = audit_service.observations_for(db, profile_id, snapshot)
        narrative = audit_service.generate_narrative(observations, ai_provider)
    except Exception:  # noqa: BLE001 -- see docstring: never fail a completed sync over prose
        logger.warning("event=presence_narrative_skipped connection_id=%s", snapshot.connection_id)
        return

    if not narrative:
        return

    payload = dict(snapshot.payload or {})
    payload["narrative"] = narrative
    payload["narrative_prompt_version"] = audit_service.PROMPT_VERSION
    # Reassigned rather than mutated in place: SQLAlchemy does not track mutation inside a plain
    # JSON column, so an in-place update would be silently dropped on commit.
    snapshot.payload = payload


def _write_snapshot_if_changed(
    db: Session, connection: ProviderConnection, fetched: ProviderFetch
) -> Optional[ExternalProfileSnapshot]:
    """Append a history row only when the account's material content actually changed.

    Without this, history would be a scroll of identical rows recording nothing but how often
    the student opened the screen, and "what has improved since last month?" -- the question the
    table exists to answer -- would be unanswerable in practice.
    """
    content = _snapshot_content(fetched)
    fingerprint = fingerprint_of(content)

    latest = (
        db.query(ExternalProfileSnapshot)
        .filter(ExternalProfileSnapshot.connection_id == connection.id)
        .order_by(ExternalProfileSnapshot.sequence.desc())
        .first()
    )
    if latest is not None and latest.fingerprint == fingerprint:
        return None

    payload = dict(content)
    payload["schema_version"] = SNAPSHOT_SCHEMA_VERSION
    payload["stars_total"] = sum(repo.stars for repo in fetched.repositories)
    payload["followers"] = fetched.profile.followers
    payload["listing_complete"] = fetched.complete

    snapshot = ExternalProfileSnapshot(
        connection_id=connection.id,
        sequence=(latest.sequence + 1) if latest is not None else 1,
        fingerprint=fingerprint,
        payload=payload,
    )
    db.add(snapshot)
    try:
        db.flush()
    except IntegrityError:
        # Two concurrent syncs claimed the same sequence. The unique constraint caught it; one
        # of them keeps the row and the other reports "nothing written", which is true.
        db.rollback()
        logger.info(
            "provider=%s event=snapshot_sequence_raced connection_id=%s", connection.provider, connection.id
        )
        return None
    return snapshot


def latest_snapshot(db: Session, connection_id: int) -> Optional[ExternalProfileSnapshot]:
    return (
        db.query(ExternalProfileSnapshot)
        .filter(ExternalProfileSnapshot.connection_id == connection_id)
        .order_by(ExternalProfileSnapshot.sequence.desc())
        .first()
    )
