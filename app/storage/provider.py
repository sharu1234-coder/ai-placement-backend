from abc import ABC, abstractmethod


class StorageError(Exception):
    """Raised when a storage backend fails to save or delete a file."""


class StorageProvider(ABC):
    """Provider-independent file storage interface. Services depend only on this, never
    a vendor SDK or the local filesystem directly."""

    @abstractmethod
    def save(self, key: str, data: bytes) -> str:
        """Persist data under key (e.g. "resumes/<uuid>.pdf"). Returns the key actually
        stored -- callers persist this as the file's reference for a later delete()."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the object at key. Must not raise if the key is already gone."""

    @abstractmethod
    def load(self, key: str) -> bytes:
        """Read back the bytes stored under key. Raises StorageError if key doesn't exist
        or the read fails -- callers must treat this as a possible-failure path (e.g. the
        ATS scoring engine falls back to text-only checks rather than crashing)."""


def get_storage_provider() -> StorageProvider:
    from app.core.config import settings

    if settings.storage_provider == "local":
        from app.storage.local_provider import LocalStorageProvider

        return LocalStorageProvider()

    if settings.storage_provider == "s3":
        from app.storage.s3_provider import S3StorageProvider

        return S3StorageProvider()

    raise NotImplementedError(f"Storage provider '{settings.storage_provider}' is not implemented yet")
