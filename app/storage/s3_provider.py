import logging

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
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
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.storage_s3_endpoint_url,
            region_name=settings.storage_s3_region,
            aws_access_key_id=settings.storage_s3_access_key_id,
            aws_secret_access_key=settings.storage_s3_secret_access_key,
        )

    def save(self, key: str, data: bytes) -> str:
        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(f"Failed to upload '{key}' to S3-compatible storage: {exc}") from exc
        return key

    #: Error codes that mean "it is not there", which is the success case for an idempotent
    #: delete. S3 itself returns 204 for a missing key, but S3-compatible implementations are
    #: not consistent about that, so the codes are handled explicitly.
    _ALREADY_GONE = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})

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
