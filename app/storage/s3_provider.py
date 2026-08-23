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

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Could not delete '%s' from S3-compatible storage: %s", key, exc)
