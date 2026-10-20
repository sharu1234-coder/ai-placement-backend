import errno
from pathlib import Path

from app.core.config import settings
from app.storage.provider import StorageError, StorageProvider


class LocalStorageProvider(StorageProvider):
    """Development default -- stores files on local disk under settings.upload_dir. Not
    suitable for production: files vanish on redeploy/container restart and aren't shared
    across multiple app instances (use S3StorageProvider via STORAGE_PROVIDER=s3 there)."""

    name = "local"

    def save(self, key: str, data: bytes) -> str:
        path = Path(settings.upload_dir) / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def delete(self, key: str) -> None:
        path = Path(settings.upload_dir) / key
        try:
            # missing_ok: absent is idempotent success, per the StorageProvider contract.
            path.unlink(missing_ok=True)
        except OSError as exc:
            # A permission error or a read-only mount is a real failure, and it used to
            # propagate as a bare OSError that `delete_account` swallowed with
            # `except Exception`. Typed, so the caller can distinguish it from a bug.
            raise StorageError(
                f"Failed to delete '{key}' from local storage: {exc}",
                # The errno name (EACCES, EROFS, EBUSY) -- the same triage signal the S3
                # adapter gets from an error code, and equally free of the path.
                code=errno.errorcode.get(exc.errno) if exc.errno else None,
            ) from exc

    def load(self, key: str) -> bytes:
        path = Path(settings.upload_dir) / key
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Failed to read '{key}' from local storage: {exc}") from exc
