import errno
from pathlib import Path

from app.core.config import settings
from app.storage.erasure import (
    ERASURE_VERSIONS_REMAIN,
    VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
    VERSIONING_NONE,
    ErasureOutcome,
)
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

    def erase(self, key: str) -> ErasureOutcome:
        """Delete the file and confirm it is gone.

        A filesystem keeps no version history, so unlinking a file is already permanent and
        there is nothing to enumerate. That makes the file simply not being there sufficient
        evidence here -- which it never is on a versioned bucket, and the outcome says which of
        the two kinds of evidence was used rather than letting them look alike.

        The existence check before the delete is what makes the count honest and distinguishes
        "erased" from "was never there", which is the normal result of a retry.
        """
        path = Path(settings.upload_dir) / key
        existed = path.exists()
        self.delete(key)
        if path.exists():
            # unlink() returned without error and the file is still readable -- a replica, a
            # race, or a mount that silently discards deletes. Never reported as an erasure.
            raise StorageError(
                f"'{key}' is still present after being deleted from local storage",
                code=ERASURE_VERSIONS_REMAIN,
            )
        return ErasureOutcome(
            versioning=VERSIONING_NONE,
            verified_by=VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
            versions_deleted=1 if existed else 0,
            already_absent=not existed,
        )

    def load(self, key: str) -> bytes:
        path = Path(settings.upload_dir) / key
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Failed to read '{key}' from local storage: {exc}") from exc
