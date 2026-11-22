import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.storage.identity import StorageIdentity
from app.storage.provider import StorageError, StorageProvider

logger = logging.getLogger(__name__)


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

    def save(self, key: str, data: bytes) -> str:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
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
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
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
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Failed to read '{key}' from S3-compatible storage: {exc}") from exc
