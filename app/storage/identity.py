"""Which storage a deletion job belongs to, captured when the job is created.

An object key is only meaningful to the storage that holds it. `app/workers/deletion_worker.py`
already refuses to erase a job against a different *provider*, and the comment there says why:
deleting a key from the wrong backend almost always **succeeds**, because the key is absent
there, absence is idempotent success, the job is marked done, and closing the request deletes
the last row that held the key. That is not a failed erasure, it is a silent one.

The same reasoning applies one level down and was not applied there. `S3StorageProvider` reads
its bucket from configuration when it is constructed, so a worker erases from *today's*
configured bucket rather than the bucket the object was written to. Change
`STORAGE_S3_BUCKET` while jobs are queued and every one of them reports success having deleted
nothing. The provider name cannot catch it: AWS S3, Cloudflare R2, MinIO, Backblaze B2 and
DigitalOcean Spaces are all `"s3"` to this codebase, by design (see `S3StorageProvider`).

So the identity is recorded per job and verified before anything destructive runs.

**No credential is part of an identity.** Access key ids and secrets are deliberately excluded:
an identity exists to be compared and reported on, and a field that may be written to a row or
an operator's console must not be able to carry a secret. The fields here are the ones that say
*where* an object is, never *how* to reach it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

#: Providers for which a bucket is part of the identity rather than optional. `local` stores on
#: the filesystem and has no bucket, so an identity without one is complete for it.
_BUCKET_REQUIRED = frozenset({"s3"})

#: Separator for the fingerprint payload. A unit separator rather than a character that can
#: appear in a bucket name or a URL, so ("a", "b") and ("a\x1fb",) cannot fingerprint alike.
_SEP = "\x1f"


@dataclass(frozen=True)
class StorageIdentity:
    """Where an object lives, as a value. Frozen: a captured identity is a historical fact.

    `region` and `endpoint` are both optional and both meaningful. A same-named bucket in
    another region is a different bucket; an endpoint distinguishes one S3-compatible vendor
    (or account namespace) from another when the provider name cannot.
    """

    provider: str
    bucket: Optional[str] = None
    endpoint: Optional[str] = None
    region: Optional[str] = None

    @property
    def revision(self) -> str:
        """A short, non-secret fingerprint of the fields above.

        Two uses, and it is worth being precise about which. It is **not** what equality is
        decided on -- that is field-by-field, so that changing how this is derived can never
        turn an already-recorded identity into a mismatch. What it is for is reporting: bucket
        names and endpoints are withheld from logs throughout this codebase, and an operator
        still needs to be able to say "these jobs were created under one configuration and
        those under another". A fingerprint says that while naming nothing.

        It also serves as the marker that an identity was captured at all. A job whose stored
        revision is NULL predates identity capture, which is a different situation from a job
        whose identity does not match, and the worker treats the two separately.
        """
        payload = _SEP.join(
            (self.provider, self.bucket or "", self.endpoint or "", self.region or "")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def is_complete(self) -> bool:
        """Whether this identity says enough to target an object safely.

        Provider-dependent on purpose: demanding a bucket from `local` would make every
        filesystem job unverifiable, and demanding nothing from `s3` would leave the gap this
        module exists to close.
        """
        if not self.provider:
            return False
        if self.provider in _BUCKET_REQUIRED and not self.bucket:
            return False
        return True

    def describe(self) -> str:
        """A log-safe description: the provider and the fingerprint, never the bucket.

        Used in operator-facing messages. `deletion_worker` withholds keys, buckets and
        provider messages from logs, and this keeps that property while still being specific
        enough to act on.
        """
        return f"{self.provider}@{self.revision}"

    @classmethod
    def from_settings(cls) -> "StorageIdentity":
        """The identity of the storage this process is configured for.

        Read at the moment a deletion job is created, which is the only moment at which the
        configuration and the object are known to agree.
        """
        from app.core.config import settings

        provider = settings.storage_provider
        if provider == "s3":
            return cls(
                provider=provider,
                bucket=settings.storage_s3_bucket,
                endpoint=settings.storage_s3_endpoint_url,
                region=settings.storage_s3_region,
            )
        return cls(provider=provider)
