"""The only sanctioned way for the test suite to learn a bucket it may destroy objects in.

Written to the same rule as `tests/db_safety.py`, for the same reason. The erasure integration
test is destructive by construction: it writes objects, deletes every version of them by id,
and then asserts the version history is empty. That is correct for a bucket which exists to be
thrown away, and unrecoverable for one which does not -- a deleted object *version* is gone,
with no delete marker to undo and no "previous version" left to restore. There is no equivalent
of a database backup in the middle of this operation.

So the bucket is never read from the environment by the module that uses it. It calls
:func:`resolve_disposable_bucket`, which returns a target only if every check below passes and
raises :class:`UnsafeTestBucket` otherwise. Making the guard the supply route rather than an
extra call at each site is deliberate, exactly as it is for the database: a test added later
gets the protection by the ordinary act of asking for the bucket.

What this is and is not:

- It is a guard against *accident* -- a stale shell, a copied command line, a production
  profile left in the environment from a debugging session, a prefix typo that widens the blast
  radius from one folder to the whole bucket.
- It is **not** isolation. Isolation is an IAM property: a dedicated principal whose policy
  permits `s3:DeleteObjectVersion` on `arn:aws:s3:::<test-bucket>/<prefix>*` and nothing else.
  No amount of client-side string checking substitutes for that, and a correct setup still
  needs it. These checks exist so that the bucket name is not the only thing being trusted.

No credential is read here, and none can be printed. The bucket, endpoint, region and account
id are configuration rather than secrets, but the account id is still withheld from messages
because it identifies an account; credentials are left entirely to boto3's own resolution
chain, so this module never holds one to leak.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Optional

#: The bucket the integration test may create and destroy object versions in.
BUCKET_VAR: Final = "TEST_S3_BUCKET"

#: The key prefix it is confined to. Required, not optional: without it a mistake reaches the
#: whole bucket, and a bucket shared with anything else would then be unsafe by default.
PREFIX_VAR: Final = "TEST_S3_PREFIX"

#: An explicit, test-only opt-in. A bucket name alone is not consent -- it can be inherited
#: from a parent shell or a CI template. This variable has no meaning outside the test suite,
#: so its presence is a statement by whoever set it.
OPT_IN_VAR: Final = "TEST_S3_DESTRUCTIVE"

#: Optional endpoint for an S3-compatible service (MinIO, R2, ...). Unset means AWS.
ENDPOINT_VAR: Final = "TEST_S3_ENDPOINT_URL"
REGION_VAR: Final = "TEST_S3_REGION"

#: Optional account id, asserted on every call as ExpectedBucketOwner. Strongly recommended on
#: AWS: it is the only check in this file that the *service* enforces rather than this process.
OWNER_VAR: Final = "TEST_S3_EXPECTED_BUCKET_OWNER"

_TRUE: Final = frozenset({"1", "true", "yes", "on"})

#: Required marker in the bucket name. A naming convention, not a security boundary -- it is
#: here to catch the typo, not to replace the IAM policy.
_REQUIRED_MARKER: Final = "test"

#: Substrings that should never appear in the name of a bucket this test writes to. Not an
#: exhaustive list of dangerous names -- no such list exists -- but these are the ones a copied
#: configuration actually carries.
_FORBIDDEN_MARKERS: Final = ("prod", "live", "backup", "archive")

#: Characters that would turn a prefix into a pattern. S3 has no wildcard in ListObjectVersions
#: -- `Prefix` is a literal -- so these can only be a misunderstanding, and acting on one would
#: mean deleting under a prefix nobody intended.
_WILDCARDS: Final = ("*", "?", "[", "]")

#: Shortest acceptable prefix. Three characters plus the required separator is enough for
#: something deliberate and too long for a slip like "a/".
_MIN_PREFIX: Final = 4


class UnsafeTestBucket(RuntimeError):
    """Raised instead of destroying object versions in a bucket not proven disposable.

    Deliberately not a `pytest.skip`: a misconfigured destructive target is a fault to fix, and
    a skip would let it pass unnoticed as "no bucket configured". Absence is a skip; a *wrong*
    configuration is an error.
    """


@dataclass(frozen=True)
class DisposableBucket:
    """A validated destructive target. Frozen, so a test cannot widen its own blast radius."""

    bucket: str
    prefix: str
    endpoint: Optional[str] = None
    region: Optional[str] = None
    expected_owner: Optional[str] = None

    def key(self, name: str) -> str:
        """A key inside the isolated prefix. The only way these tests should build one.

        Rejects traversal rather than normalising it: a test that needs `..` in a key is doing
        something the prefix confinement was supposed to prevent, and quietly collapsing the
        path would hide that.
        """
        if not name or name.startswith("/") or ".." in name:
            raise UnsafeTestBucket(
                f"{name!r} is not a usable object name inside the test prefix; it must be "
                "relative and must not contain '..'."
            )
        return f"{self.prefix}{name}"

    def describe(self) -> str:
        """Log-safe: bucket and prefix, never the account id."""
        where = self.endpoint or "aws"
        return f"{self.bucket}/{self.prefix} at {where}"


def _is_true(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in _TRUE


def check(environ: Optional[dict] = None, *, app_bucket: Optional[str] = None) -> Optional[DisposableBucket]:
    """Validate the configured target. Pure: parses and compares, never connects.

    Returns a :class:`DisposableBucket` when it is safe to destroy versions in, ``None`` when
    no bucket is configured at all, and raises :class:`UnsafeTestBucket` for anything in
    between.

    ``None`` for absent is what keeps the ordinary run working: with no bucket configured the
    integration test skips, which is the normal state of this repository -- no vendor has been
    chosen and no credential exists.
    """
    env = os.environ if environ is None else environ
    raw_bucket = (env.get(BUCKET_VAR) or "").strip()

    if not raw_bucket:
        return None

    # The opt-in, before anything else is even examined.
    if not _is_true(env.get(OPT_IN_VAR)):
        raise UnsafeTestBucket(
            f"{BUCKET_VAR} is set, but {OPT_IN_VAR} is not. This test permanently deletes "
            f"object versions, which cannot be undone -- there is no delete marker to remove "
            f"and no previous version left to restore. Set {OPT_IN_VAR}=1 only for a bucket "
            f"whose contents you are willing to lose."
        )

    bucket = raw_bucket.lower()
    if _REQUIRED_MARKER not in bucket:
        raise UnsafeTestBucket(
            f"{BUCKET_VAR} must contain {_REQUIRED_MARKER!r}; got {raw_bucket!r}. This is a "
            f"naming convention rather than a security boundary -- it is here to catch the "
            f"typo, not to replace a dedicated bucket and an IAM policy scoped to it."
        )
    for marker in _FORBIDDEN_MARKERS:
        if marker in bucket:
            raise UnsafeTestBucket(
                f"{BUCKET_VAR} is {raw_bucket!r}, which contains {marker!r}. A bucket named "
                f"like that is not a disposable one, whatever else it is called. Use a bucket "
                f"created for this test and nothing else."
            )

    # The most dangerous single mistake: the application's own bucket pasted in. Checked even
    # though the name rules above would usually catch it, because they are a convention and
    # this is a fact.
    configured = app_bucket
    if configured is None:
        from app.core.config import settings

        configured = settings.storage_s3_bucket
    if configured and configured.strip().lower() == bucket:
        raise UnsafeTestBucket(
            f"{BUCKET_VAR} names the same bucket as STORAGE_S3_BUCKET ({raw_bucket!r}). This "
            f"test would permanently delete object versions in it."
        )

    prefix = (env.get(PREFIX_VAR) or "").strip()
    if not prefix:
        raise UnsafeTestBucket(
            f"{PREFIX_VAR} is required. Without a prefix this test is confined to nothing but "
            f"the bucket itself, so a mistake reaches every object in it."
        )
    if prefix.startswith("/"):
        raise UnsafeTestBucket(
            f"{PREFIX_VAR} must not start with '/'; S3 keys have no leading slash, so "
            f"{prefix!r} would confine the test to a prefix that matches nothing and then "
            f"appear to pass."
        )
    for wildcard in _WILDCARDS:
        if wildcard in prefix:
            raise UnsafeTestBucket(
                f"{PREFIX_VAR} contains {wildcard!r}. S3 prefixes are literal, not patterns, "
                f"so a wildcard here is a misunderstanding -- and acting on it would mean "
                f"deleting under a prefix nobody chose."
            )
    if ".." in prefix:
        raise UnsafeTestBucket(f"{PREFIX_VAR} must not contain '..'; got {prefix!r}.")
    if not prefix.endswith("/"):
        raise UnsafeTestBucket(
            f"{PREFIX_VAR} must end with '/'; got {prefix!r}. Without the separator the prefix "
            f"is not a folder boundary: {prefix!r} also matches {prefix + '-production/'!r}, "
            f"which is how a confined test stops being confined."
        )
    if len(prefix) < _MIN_PREFIX:
        raise UnsafeTestBucket(
            f"{PREFIX_VAR} is {prefix!r}, which is too short to be deliberate. Use something "
            f"that identifies this test, such as 'ci-erasure-checks/'."
        )

    return DisposableBucket(
        bucket=raw_bucket,
        prefix=prefix,
        endpoint=(env.get(ENDPOINT_VAR) or "").strip() or None,
        region=(env.get(REGION_VAR) or "").strip() or None,
        expected_owner=(env.get(OWNER_VAR) or "").strip() or None,
    )


def resolve_disposable_bucket() -> Optional[DisposableBucket]:
    """The validated target for this process, or ``None`` when the integration test must skip.

    Called at module import so the checks run before any fixture does, and therefore before
    anything can be written or deleted. pytest turns an exception here into a collection error
    naming this module, so a misconfiguration stops the run with a message rather than a
    casualty.
    """
    return check()
