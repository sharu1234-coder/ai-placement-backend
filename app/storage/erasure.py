"""What it takes to be able to say an object has actually been erased.

Stage A made sure a deletion job can only be acted on against the storage it was enqueued
against. It did not change *what* the delete does, and on a versioned bucket what it does is
not an erasure. AWS states it plainly: "When versioning is enabled, a simple DELETE cannot
permanently delete an object. (A simple DELETE request is a request that does not specify a
version ID.) Instead, Amazon S3 inserts a delete marker". The object stops being listed, the
API answers 404, and every byte is still there under its version id -- so the job is marked
done, the request closes, the last row holding the key is deleted, and the data survives
something the user was told was a permanent deletion.

Two consequences shape everything in this module.

**A 404 is not evidence.** HEAD and GET answer 404 as soon as a delete marker exists, which is
precisely the state in which nothing has been erased. So absence is established by enumerating
the key version history -- `ListObjectVersions` -- and not by asking whether the object can
still be fetched. The one exception is a bucket the service itself has told us has never been
versioned, where there is no history to enumerate; that case is marked as such in the outcome
rather than being allowed to look like the others.

**Deleting is not finishing.** A multi-object delete returns per-object results and can report
`200 OK` with individual failures inside it, so "the call did not raise" says nothing about
whether every version went. The contract here is that `erase` returns only after a second
enumeration found nothing left, and raises otherwise. The worker never marks a job done on
anything weaker.

Nothing in this module ever bypasses Object Lock. `BypassGovernanceRetention` and the
`s3:BypassGovernanceRetention` permission exist, and using them would turn a retention policy
into a suggestion. A locked version is reported as locked and handed to a human.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The bucket is versioned: deletes without a version id insert delete markers.
VERSIONING_ENABLED = "enabled"
#: Versioning was enabled and then suspended. History from before the suspension is still
#: there, and new writes land on the "null" version id. AWS: "If versioning is suspended for a
#: bucket, a DELETE request ... Can only remove an object whose version ID is null", and
#: "you will lose the content for the null version when a delete marker replaces it" -- so a
#: version-less delete here destroys one copy and hides the rest.
VERSIONING_SUSPENDED = "suspended"
#: Versioning has never been enabled. There is no version history, so a delete is permanent.
VERSIONING_NONE = "unversioned"

#: Absence was established by enumerating the key version history. This is the normal case and
#: the only one that can speak for a versioned bucket.
VERIFIED_BY_INVENTORY = "inventory"
#: Absence was established by the object simply not being there -- a 404 from HeadObject, or a
#: missing file on disk. Sound *only* where no version history can exist to be missed: a
#: filesystem, or a bucket the service itself reported as never having been versioned. Recorded
#: separately so this weaker evidence can never be read as the enumeration above.
VERIFIED_BY_ABSENCE_WITHOUT_HISTORY = "absence-where-no-history-can-exist"

# --- failure codes, carried on StorageError.code -------------------------------------------
# These sit alongside the backend own codes (AccessDenied, SlowDown, NoSuchBucket) in the same
# field, because that field is what an operator triages on and what the worker classifies. Each
# names a condition the S3 API does not have a code for.

#: Deletes reported success and the second enumeration still found versions or delete markers.
#: Not treated as a configuration fault: a concurrent writer can legitimately add a version
#: between the two passes, and the next attempt erases it. It is never a success.
ERASURE_VERSIONS_REMAIN = "VersionsRemain"
#: GetBucketVersioning answered with a status this code does not recognise. Fail closed: an
#: unknown versioning mode means an unknown deletion semantic.
ERASURE_VERSIONING_UNKNOWN = "VersioningStateUnknown"
#: The bucket is versioned but the service will not enumerate versions, so there is no way to
#: establish what remains. Some S3-compatible implementations answer NotImplemented here.
ERASURE_INVENTORY_UNSUPPORTED = "InventoryUnsupported"
#: A truncated listing came back without the marker needed to ask for the next page, or the
#: markers stopped advancing. Either way the history cannot be enumerated completely, and a
#: partial enumeration must not be allowed to look like an empty one.
ERASURE_INVENTORY_BROKEN = "InventoryPaginationBroken"
#: A version could not be deleted and the service confirmed, when asked, that it is under an
#: Object Lock retention. Needs a person: honouring it is the point of the lock.
ERASURE_OBJECT_LOCKED = "ObjectLockActive"
#: As above, for a legal hold.
ERASURE_LEGAL_HOLD = "LegalHoldActive"
#: The provider in use has no version-aware erasure. Fail closed rather than fall back to a
#: plain delete, which on a versioned bucket is the defect this module exists to fix.
ERASURE_NOT_IMPLEMENTED = "ErasureNotImplemented"


#: How an erasure failure is answered. These three sets are facts about the erasure contract,
#: not about any one caller, which is why they live here beside the codes themselves rather
#: than in the worker that first needed them. Both callers now classify from the same copy:
#: `deletion_worker` turns them into `needs_attention`, and `resume_service` turns them into a
#: refusal the student sees -- and a single resume deletion must not be able to drift into
#: being more permissive than the account-level one.

#: Backend error codes that describe the *configuration*, not the object. These cannot clear by
#: being retried -- no number of attempts conjures a bucket into existence. A bucket whose
#: versioning mode is unrecognised, a service that will not enumerate versions, and a provider
#: with no version-aware erasure at all are each a statement that this deployment cannot prove
#: an erasure, which a retry does not change. `NoSuchBucket` belongs here because the S3
#: adapter once treated it as "already gone", which turned a missing bucket into a completed
#: erasure.
CONFIGURATION_ERROR_CODES = frozenset(
    {
        "NoSuchBucket",
        ERASURE_VERSIONING_UNKNOWN,
        ERASURE_INVENTORY_UNSUPPORTED,
        ERASURE_NOT_IMPLEMENTED,
    }
)

#: An Object Lock retention or a legal hold refused the delete. Honouring the lock is the point
#: of it: the bypass exists, using it would make the retention policy a suggestion, and nothing
#: in this package will do it. Deliberately not a failure of the caller -- the storage is doing
#: exactly what it was configured to do, and the answer is a person, not a retry.
PROTECTED_ERROR_CODES = frozenset({ERASURE_OBJECT_LOCKED, ERASURE_LEGAL_HOLD})

#: The request was refused rather than failed. A policy has to change for this to succeed, so
#: retrying it on a backoff only delays the moment somebody is told.
AUTHORIZATION_ERROR_CODES = frozenset({"AccessDenied", "AllAccessDisabled", "MethodNotAllowed"})

#: Everything that needs a person on the first attempt. What is *not* here is as deliberate: a
#: throttle, a 503, a transport fault and `VersionsRemain` all clear by themselves, so they are
#: worth retrying. `VersionsRemain` in particular is the shape a concurrent writer takes -- a
#: new version arriving between the delete and the verification -- and the next attempt erases
#: it.
IMMEDIATE_ATTENTION_CODES = (
    CONFIGURATION_ERROR_CODES | PROTECTED_ERROR_CODES | AUTHORIZATION_ERROR_CODES
)


@dataclass(frozen=True)
class StoredVersion:
    """One entry in a key version history.

    `ListObjectVersions` returns versions and delete markers in two separate containers
    (`Version` and `DeleteMarker`), and both have to go: a delete marker left behind keeps the
    key listed as deleted, and a version left behind keeps the data. Which of the two this was
    is kept because the counts are reported separately and because deleting a delete marker has
    the visible side effect of making an older version current again -- worth being able to
    explain in a report.
    """

    version_id: str
    is_delete_marker: bool = False


@dataclass(frozen=True)
class ErasureOutcome:
    """The result of an erasure that is known to have finished.

    It only ever describes a verified end state: `erase` raises rather than returning an
    outcome it could not confirm, so there is no "probably" to represent here. What the fields
    carry is *how* that end state was reached, which is what an operator and a test need in
    order to tell a real version sweep from a bucket where there was nothing to sweep.
    """

    versioning: str
    verified_by: str
    versions_deleted: int = 0
    markers_deleted: int = 0
    #: Nothing existed under the key when the enumeration ran. A real state, not a failure, and
    #: the normal result of a retry after a partial success -- which is what makes repeated
    #: execution safe.
    already_absent: bool = False

    @property
    def removed(self) -> int:
        return self.versions_deleted + self.markers_deleted
