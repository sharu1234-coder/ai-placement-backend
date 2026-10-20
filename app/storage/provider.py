from abc import ABC, abstractmethod
from typing import ClassVar, Optional


class StorageError(Exception):
    """Raised when a storage backend fails to save or delete a file.

    `code` is the backend's own short machine code for the failure -- an S3 error code such as
    `AccessDenied` or `SlowDown`, or an `errno` name such as `EACCES` -- and never the
    provider's message, which routinely carries the bucket name and the object key.

    It exists because every adapter wraps every failure in this one class, so the class name on
    its own tells an operator only that something went wrong. Triaging a stuck erasure means
    separating a refusal that will never succeed without a human -- a denied permission, a
    retention lock -- from one that clears by itself, like throttling or a 503. The code is the
    only non-sensitive thing that carries that distinction.
    """

    def __init__(self, message: str, *, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class StorageProvider(ABC):
    """Provider-independent file storage interface. Services depend only on this, never
    a vendor SDK or the local filesystem directly."""

    #: The provider's name, matching the STORAGE_PROVIDER value that selects it. Part of the
    #: contract rather than a label: a deletion job records the name it was enqueued under, and
    #: the worker refuses to erase against a different backend, because an object key is only
    #: meaningful to the backend that stored it.
    name: ClassVar[str]

    @abstractmethod
    def save(self, key: str, data: bytes) -> str:
        """Persist data under key (e.g. "resumes/<uuid>.pdf"). Returns the key actually
        stored -- callers persist this as the file's reference for a later delete()."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove the object at key.

        An object that is **already gone is success** -- deletion is idempotent, so a retry
        after a partial failure must not fail on the objects it already removed.

        Anything else raises `StorageError`. The distinction matters because this method used
        to be allowed to return normally no matter what happened: the S3 adapter caught every
        `BotoCoreError`/`ClientError`, logged a warning and returned, so a caller had no way at
        all to tell a deletion from a failure. `delete_account` then relied on that silence,
        committing the database rows -- the only record of the key -- while the object could
        still be in the bucket. The result was an unreachable orphan and a user who had been
        told their data was erased.

        A caller that genuinely wants best-effort cleanup must catch `StorageError` itself and
        say so, so that the decision is visible at the call site instead of buried in an
        adapter.
        """

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
