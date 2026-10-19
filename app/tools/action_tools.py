"""VA-7's write tools: save and unsave a job.

Two capabilities, chosen because they are the smallest real mutation the product has. Both are
reversible by the student in one tap, both affect only rows that student owns, neither touches
money, credentials, account state or anything another person can see. Nothing is learned from
proving the boundary on a dangerous action that is not learned from proving it here, and a
mistake while building it costs a tracker row.

**Save and unsave are separate transitions, not a toggle.** A toggle is the wrong shape for
anything that can be retried: a duplicate submission of "flip it" undoes the first submission's
effect, so the second request succeeds while leaving the student with the opposite of what they
asked for. These two name their target state instead, so a repeat is a no-op rather than a
reversal.

**Arguments are listing ids, not saved-job ids.** `listing_id` identifies the job in both
directions, which is what lets save and unsave share a resource key and makes "unsave the job
you just saved" expressible without the client having to carry the row id the save produced.
The saved-job row id stays an internal detail of the tracker.

**What `jobs.save` deliberately does not do.** `job_hunt.service.save_job` also sets status, and
passing a status through would mean a repeat save on a job the student has already moved to
"interview" silently rewrote their pipeline back to "saved" -- losing a real piece of their own
history to an idempotent-looking retry. So this tool treats a job already in the pipeline as
already satisfied and does not write at all. Only a dismissed job, or one with no row, is a
genuine save. Changing a pipeline status is a different intent and will be its own action with
its own confirmation sentence.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional, TypeVar

from pydantic import Field

from app.models.job_listing import JobListing
from app.models.saved_job import SavedJob
from app.services.job_hunt import service as job_hunt_service
from app.tools.contracts import (
    ActionPreview,
    Idempotency,
    RiskClass,
    ToolContext,
    ToolInput,
    ToolKind,
    ToolOutput,
    ToolResourceNotFound,
    ToolSpec,
    Verification,
)

_R = TypeVar("_R")


def _guard(capability: str) -> Callable[[Callable[..., _R]], Callable[..., _R]]:
    """Translate the domain layer's failures into the registry's own vocabulary.

    Without this, `ListingNotFoundError` reaches the registry as an unrecognised exception and
    is wrapped as an execution failure -- which reads to a caller as "the server broke" rather
    than "there is no such job", and loses the distinction a client needs to decide whether
    retrying could ever help. The redaction is preserved in the translation: the replacement
    names neither the id nor whether the row exists for someone else.
    """

    def decorate(function: Callable[..., _R]) -> Callable[..., _R]:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> _R:
            try:
                return function(*args, **kwargs)
            except (
                job_hunt_service.ListingNotFoundError,
                job_hunt_service.SavedJobNotFoundError,
            ) as exc:
                raise ToolResourceNotFound(capability) from exc

        return wrapper

    return decorate

#: A job in any pipeline status other than this one counts as tracked. "dismissed" is part of
#: the same state machine (see app/models/saved_job.py) and means the student pushed the job
#: away, so re-saving it is a real transition rather than a no-op.
_DISMISSED = "dismissed"


class JobActionArguments(ToolInput):
    """The one argument either action takes.

    `gt=0` rather than a bare int: a negative or zero id is never a real row, and rejecting it
    in the schema means the database is never asked about it. `extra="forbid"` comes from
    `ToolInput`, so a caller that invents `profile_id` or `status` is refused rather than having
    the field ignored.
    """

    listing_id: int = Field(gt=0, description="The job listing to act on.")


class SavedJobActionResult(ToolOutput):
    """Deliberately thin. The authoritative facts are the state the registry verified and the
    label the student already saw, so there is nothing here worth a field that could disagree
    with the tracker."""

    listing_id: int
    title: str
    company: Optional[str] = None
    #: The tracker status after the action, or None when the job is no longer tracked.
    status: Optional[str] = None
    tracked: bool


# --------------------------------------------------------------------------- shared resolution


def _resolve_listing(context: ToolContext, listing_id: int) -> JobListing:
    """The listing, if this student may see it at all.

    Ownership goes through the existing `assert_listing_visible`, which is the same check the
    HTTP save route makes: a provider advert is public content, a manually entered listing
    belongs to whoever typed it. Its `ListingNotFoundError` is deliberately indistinguishable
    from a missing row, so an id cannot be confirmed by probing -- and that property only holds
    because this reuses the check rather than writing a second one.
    """
    profile = context.require_profile()
    listing = context.db.get(JobListing, listing_id)
    if listing is None:
        raise job_hunt_service.ListingNotFoundError(f"Job listing {listing_id} not found")
    job_hunt_service.assert_listing_visible(listing, profile.id)
    return listing


def _tracked_row(context: ToolContext, listing_id: int) -> Optional[SavedJob]:
    """This student's tracker row for the listing, read through the existing read service."""
    profile = context.require_profile()
    return job_hunt_service.saved_jobs_for_listings(context.db, profile.id, [listing_id]).get(listing_id)


def _label(listing: JobListing) -> str:
    return f"{listing.title} at {listing.company}" if listing.company else listing.title


def _resource_key(listing_id: int) -> str:
    return f"job_listing:{listing_id}"


# --------------------------------------------------------------------------- jobs.save


@_guard("jobs.save")
def _preview_save(context: ToolContext, args: JobActionArguments) -> ActionPreview:
    listing = _resolve_listing(context, args.listing_id)
    existing = _tracked_row(context, args.listing_id)
    already = existing is not None and existing.status != _DISMISSED
    label = _label(listing)
    return ActionPreview(
        description=(
            f"{label} is already in your tracker, so nothing will change."
            if already
            else f"Save {label} to your tracker."
        ),
        resource_key=_resource_key(args.listing_id),
        resource_label=label,
        target_state="tracked",
        already_satisfied=already,
    )


@_guard("jobs.save")
def _execute_save(context: ToolContext, args: JobActionArguments) -> SavedJobActionResult:
    profile = context.require_profile()
    listing = _resolve_listing(context, args.listing_id)
    existing = _tracked_row(context, args.listing_id)
    if existing is not None and existing.status != _DISMISSED:
        # Already satisfied. Returning without writing is what protects a pipeline status the
        # student has moved on from -- see the module docstring.
        return SavedJobActionResult(
            listing_id=listing.id,
            title=listing.title,
            company=listing.company,
            status=existing.status,
            tracked=True,
        )
    saved = job_hunt_service.save_job(
        context.db,
        profile.id,
        listing.id,
        "saved",
        # The honest value of the four the column accepts. The student named this job, so it is
        # not a recommendation, a saved search or an alert. An "assistant" source would need a
        # migration and would tell the tracker nothing it cannot already say.
        source="manual",
    )
    return SavedJobActionResult(
        listing_id=listing.id,
        title=listing.title,
        company=listing.company,
        status=saved.status,
        tracked=True,
    )


@_guard("jobs.save")
def _verify_save(context: ToolContext, args: JobActionArguments) -> bool:
    """Read back through the same read service the tracker screen uses.

    Not the handler's return value, and not the row the handler happened to hold: an ORM object
    carried out of the write is the write's own view of itself, which is exactly the evidence
    that cannot distinguish a commit from a rollback.
    """
    row = _tracked_row(context, args.listing_id)
    return row is not None and row.status != _DISMISSED


# --------------------------------------------------------------------------- jobs.unsave


@_guard("jobs.unsave")
def _preview_unsave(context: ToolContext, args: JobActionArguments) -> ActionPreview:
    listing = _resolve_listing(context, args.listing_id)
    existing = _tracked_row(context, args.listing_id)
    label = _label(listing)
    if existing is None:
        description = f"{label} is not in your tracker, so nothing will change."
    elif existing.status == "saved":
        description = f"Remove {label} from your tracker."
    else:
        # Named explicitly, because removing a row at "interview" also discards the notes,
        # follow-up date and applied-at that row carries. A student who is told "remove this"
        # has not been told that.
        description = (
            f"Remove {label} from your tracker. It is currently at '{existing.status}', "
            "and its notes and follow-up date will be deleted with it."
        )
    return ActionPreview(
        description=description,
        resource_key=_resource_key(args.listing_id),
        resource_label=label,
        target_state="not_tracked",
        already_satisfied=existing is None,
    )


@_guard("jobs.unsave")
def _execute_unsave(context: ToolContext, args: JobActionArguments) -> SavedJobActionResult:
    profile = context.require_profile()
    listing = _resolve_listing(context, args.listing_id)
    existing = _tracked_row(context, args.listing_id)
    if existing is not None:
        # Scoped by profile inside the service, which raises its own not-found for a row owned
        # by someone else. Passing the row id this student's own query returned means the
        # service's check and this one agree by construction.
        job_hunt_service.delete_saved_job(context.db, profile.id, existing.id)
    return SavedJobActionResult(
        listing_id=listing.id,
        title=listing.title,
        company=listing.company,
        status=None,
        tracked=False,
    )


@_guard("jobs.unsave")
def _verify_unsave(context: ToolContext, args: JobActionArguments) -> bool:
    return _tracked_row(context, args.listing_id) is None


# --------------------------------------------------------------------------- specs


def action_specs() -> list[ToolSpec]:
    """VA-7's complete write catalogue. Two entries, and that is the phase's point.

    `KEY_REQUIRED` on both: a save and an unsave are both repeat-safe in their effect, but the
    claim row is what collapses a duplicate submission into one intended mutation and what makes
    a replay answerable from authoritative state instead of re-running. Declaring NATURAL would
    have been true of the effect and would have switched off the mechanism that proves it.
    """
    shared = {
        "kind": ToolKind.ACTION,
        "input_model": JobActionArguments,
        "output_model": SavedJobActionResult,
        "idempotency": Idempotency.KEY_REQUIRED,
        "verification": Verification.USER_CONFIRMATION,
        "risk_class": RiskClass.LOW,
        "max_items": 1,
    }
    return [
        ToolSpec(
            name="jobs.save",
            summary="Add one job to the student's own tracker. Reversible with jobs.unsave.",
            handler=_execute_save,
            preview=_preview_save,
            verify=_verify_save,
            side_effects=("SavedJob",),
            **shared,
        ),
        ToolSpec(
            name="jobs.unsave",
            summary="Remove one job from the student's own tracker, with its notes and follow-up date.",
            handler=_execute_unsave,
            preview=_preview_unsave,
            verify=_verify_unsave,
            side_effects=("SavedJob",),
            **shared,
        ),
    ]


__all__ = ["JobActionArguments", "SavedJobActionResult", "action_specs"]
