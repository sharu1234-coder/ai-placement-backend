import logging
from typing import Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.storage.erasure import (
    ERASURE_INVENTORY_BROKEN,
    ERASURE_INVENTORY_UNSUPPORTED,
    ERASURE_VERSIONING_UNKNOWN,
    VERSIONING_ENABLED,
    VERSIONING_NONE,
    VERSIONING_SUSPENDED,
    StoredVersion,
)
from app.storage.identity import StorageIdentity
from app.storage.provider import StorageError, StorageProvider

logger = logging.getLogger(__name__)


def _error_code(exc: ClientError) -> str:
    """The service own short code for a failure, or "" when the response carries none.

    Pulled out because every path here classifies on it and none of them may fall back to the
    exception message, which routinely quotes the bucket and the object key.
    """
    return str(exc.response.get("Error", {}).get("Code", ""))


class S3StorageProvider(StorageProvider):
    """Production object storage (STORAGE_PROVIDER=s3). Speaks the S3 API, so this works
    against real AWS S3 as well as any S3-compatible service -- Cloudflare R2, MinIO,
    Backblaze B2, DigitalOcean Spaces, etc. -- by pointing STORAGE_S3_ENDPOINT_URL at that
    provider's endpoint. No vendor has been chosen for production yet; this class only
    needs valid S3-compatible credentials to work with whichever one is picked later."""

    name = "s3"

    def __init__(self) -> None:
        if not settings.storage_s3_bucket:
            raise StorageError("STORAGE_S3_BUCKET is not set -- required when STORAGE_PROVIDER=s3.")
        self.bucket = settings.storage_s3_bucket
        # Held on the instance rather than re-read later, so `identity()` reports the
        # destination this client was actually built for. Reading settings again at
        # verification time would compare today's configuration with itself.
        self.endpoint = settings.storage_s3_endpoint_url
        self.region = settings.storage_s3_region
        # Sent as ExpectedBucketOwner on every call when configured, so the service refuses a
        # request aimed at a bucket this account does not own instead of this process trying to
        # detect that itself. None means no guard, which is the default and is supported.
        self.expected_owner = settings.storage_s3_expected_bucket_owner
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.storage_s3_endpoint_url,
            region_name=settings.storage_s3_region,
            aws_access_key_id=settings.storage_s3_access_key_id,
            aws_secret_access_key=settings.storage_s3_secret_access_key,
        )

    def identity(self) -> StorageIdentity:
        """Where this client points. The bucket is the part the provider name cannot carry:
        every S3-compatible vendor is `s3` here, so bucket and endpoint are what separate one
        destination from another. No credential is included.

        `expected_owner` rides along as the owner guard this client would assert. It is not
        part of the destination and does not take part in identity equality -- see
        `StorageIdentity.expected_owner`."""
        return StorageIdentity(
            provider=self.name,
            bucket=self.bucket,
            endpoint=self.endpoint,
            region=self.region,
            expected_owner=self.expected_owner,
        )

    def _guard(self) -> dict:
        """`ExpectedBucketOwner` for every call, when an owner guard is configured.

        Empty when it is not, so a deployment without the setting sends exactly the request it
        sent before this existed. Applied to reads and writes as well as deletes on purpose: a
        guard that only covered deletion would still let an upload write a student file into an
        account nobody here owns.
        """
        return {"ExpectedBucketOwner": self.expected_owner} if self.expected_owner else {}

    #: Entries per `ListObjectVersions` page. AWS documents the default as 1,000 -- "By
    #: default, the action returns up to 1,000 key names" -- and it is sent explicitly rather
    #: than relied on, because the default is the vendor choice and this code pages on it.
    _LIST_PAGE = 1000

    #: Codes an S3-compatible service returns when it will not enumerate versions at all.
    #: Anything outside this set is passed through unchanged rather than guessed at: an
    #: `AccessDenied` on the listing is a permission to grant, not a missing feature.
    _INVENTORY_UNSUPPORTED = frozenset({"NotImplemented", "NotSupported", "MethodNotAllowed"})

    def versioning_status(self) -> str:
        """Which deletion semantics this bucket has, asked of the service rather than assumed.

        This is the fact everything else depends on. On an enabled or suspended bucket a
        version-less delete does not erase; on a bucket that has never been versioned it does,
        and there is no history that could be missed. Guessing either way means the data
        survives a deletion, or an unversioned bucket becomes unerasable -- so an unrecognised
        answer is a refusal rather than a default.

        The message names no bucket: an operator gets the error code and the job id, and the
        rest of this module withholds bucket names for the same reason.
        """
        try:
            response = self._client.get_bucket_versioning(Bucket=self.bucket, **self._guard())
        except ClientError as exc:
            raise StorageError(
                f"Could not read the bucket versioning state: {exc}",
                code=_error_code(exc) or None,
            ) from exc
        except BotoCoreError as exc:
            raise StorageError(
                f"Could not read the bucket versioning state: {exc}", code=type(exc).__name__
            ) from exc

        status = response.get("Status")
        if not status:
            # A bucket that has never had versioning returns no Status member at all.
            return VERSIONING_NONE
        if status == "Enabled":
            return VERSIONING_ENABLED
        if status == "Suspended":
            return VERSIONING_SUSPENDED
        raise StorageError(
            f"Unrecognised bucket versioning status {status!r}; refusing to guess what a "
            "delete would mean here",
            code=ERASURE_VERSIONING_UNKNOWN,
        )

    def inventory(self, key: str) -> list[StoredVersion]:
        """Every version and delete marker stored under exactly this key.

        The only honest way to ask whether an object is still there on a versioned bucket. HEAD
        and GET answer 404 the moment a delete marker exists, which is the state in which
        nothing has been erased, so they cannot serve as evidence; this enumerates the history
        instead.

        Three details decide whether the answer can be trusted.

        `Prefix` narrows a listing, it does not select a key: "Use this parameter to select only
        those keys that begin with the specified prefix." So asking for `resumes/42/cv.pdf` also
        returns `resumes/42/cv.pdf.bak`, and every entry is therefore filtered on an exact key
        match. A version belonging to a neighbouring key must never be deleted -- and must never
        be counted as a survivor of this one either, which would strand a finished job.

        Pagination carries two markers, not one. A truncated page is continued with
        `NextKeyMarker` *and* `NextVersionIdMarker`, because one key can have more versions than
        fit in a page and the key marker alone would restart that key from the top. The version
        marker is only ever sent together with the key marker, which is how AWS pairs them in
        every documented continuation.

        A listing that cannot be continued is an error, never an ending. If a truncated page
        arrives without a key marker, or the markers stop advancing, this raises: a partial
        enumeration that returned quietly would read as "nothing left" and complete an erasure
        over data that is still there.
        """
        found: list[StoredVersion] = []
        key_marker: Optional[str] = None
        version_marker: Optional[str] = None
        seen: set = set()

        while True:
            params = {"Bucket": self.bucket, "Prefix": key, "MaxKeys": self._LIST_PAGE}
            params.update(self._guard())
            if key_marker is not None:
                params["KeyMarker"] = key_marker
                if version_marker is not None:
                    params["VersionIdMarker"] = version_marker
            try:
                response = self._client.list_object_versions(**params)
            except ClientError as exc:
                code = _error_code(exc)
                if code in self._INVENTORY_UNSUPPORTED:
                    raise StorageError(
                        f"This storage will not enumerate object versions ({code}), so an "
                        "erasure cannot be proved against it",
                        code=ERASURE_INVENTORY_UNSUPPORTED,
                    ) from exc
                raise StorageError(
                    f"Failed to list the version history of '{key}': {exc}",
                    code=code or None,
                ) from exc
            except BotoCoreError as exc:
                raise StorageError(
                    f"Failed to list the version history of '{key}': {exc}",
                    code=type(exc).__name__,
                ) from exc

            for entry in response.get("Versions") or ():
                if entry.get("Key") == key and entry.get("VersionId"):
                    found.append(StoredVersion(version_id=entry["VersionId"]))
            for entry in response.get("DeleteMarkers") or ():
                if entry.get("Key") == key and entry.get("VersionId"):
                    found.append(
                        StoredVersion(version_id=entry["VersionId"], is_delete_marker=True)
                    )

            if not response.get("IsTruncated"):
                return found

            next_key = response.get("NextKeyMarker")
            next_version = response.get("NextVersionIdMarker")
            if not next_key:
                raise StorageError(
                    "A truncated version listing arrived with no key marker to continue from, "
                    "so the history cannot be enumerated completely",
                    code=ERASURE_INVENTORY_BROKEN,
                )
            if (next_key, next_version) in seen:
                raise StorageError(
                    "The version listing markers stopped advancing, so the history cannot be "
                    "enumerated completely",
                    code=ERASURE_INVENTORY_BROKEN,
                )
            seen.add((next_key, next_version))
            key_marker, version_marker = next_key, next_version

    def save(self, key: str, data: bytes) -> str:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, **self._guard())
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Failed to upload '{key}' to S3-compatible storage: {exc}") from exc
        return key

    #: Error codes that mean "the object is not there", which is the success case for an
    #: idempotent delete. S3 itself returns 204 for a missing key, but S3-compatible
    #: implementations are not consistent about that, so the codes are handled explicitly.
    #:
    #: `NoSuchBucket` was here and has been removed, which is a behaviour change worth stating.
    #: A missing bucket says nothing whatsoever about the object: it says this process is
    #: pointed somewhere that does not exist. Counting it as an idempotent success meant a
    #: mistyped or decommissioned bucket produced a completed erasure for every queued job,
    #: and -- because closing a deletion request deletes the last row holding the key -- left
    #: no record that anything had been missed. It now raises, and the worker treats it as a
    #: configuration fault that needs a human rather than something to retry.
    _ALREADY_GONE = frozenset({"NoSuchKey", "404", "NotFound"})

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key, **self._guard())
        except ClientError as exc:
            code = _error_code(exc)
            if code in self._ALREADY_GONE:
                # Already absent. Idempotent success, not a failure.
                return
            # Previously this was a warning and a normal return, which told the caller the
            # object was gone when it was not. Raising is what makes the failure recoverable:
            # `delete_account` can now keep the database row that points at this key instead
            # of committing an erasure that did not happen.
            raise StorageError(
                f"Failed to delete '{key}' from S3-compatible storage: {exc}", code=code or None
            ) from exc
        except BotoCoreError as exc:
            # Transport-level: no response, so no error code to classify. Treated as a genuine
            # failure, because a delete that never reached the service did not happen.
            raise StorageError(
                f"Failed to delete '{key}' from S3-compatible storage: {exc}",
                # A botocore class name (EndpointConnectionError, ConnectTimeoutError, ...).
                # It names the transport fault without quoting the endpoint or the key.
                code=type(exc).__name__,
            ) from exc

    def load(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key, **self._guard())
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Failed to read '{key}' from S3-compatible storage: {exc}") from exc
