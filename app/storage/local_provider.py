from pathlib import Path

from app.core.config import settings
from app.storage.provider import StorageError, StorageProvider


class LocalStorageProvider(StorageProvider):
    """Development default -- stores files on local disk under settings.upload_dir. Not
    suitable for production: files vanish on redeploy/container restart and aren't shared
    across multiple app instances (use S3StorageProvider via STORAGE_PROVIDER=s3 there)."""

    def save(self, key: str, data: bytes) -> str:
        path = Path(settings.upload_dir) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def delete(self, key: str) -> None:
        path = Path(settings.upload_dir) / key
        path.unlink(missing_ok=True)

    def load(self, key: str) -> bytes:
        path = Path(settings.upload_dir) / key
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Failed to read '{key}' from local storage: {exc}") from exc
