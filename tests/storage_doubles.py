"""Shared pieces for the storage doubles the deletion tests use.

Not a test module -- no `test_` prefix, so pytest does not collect it. It sits beside
`tests/db_safety.py` for the same reason: one rule, stated once, that several test modules
depend on being right.
"""

from app.storage.erasure import (
    VERIFIED_BY_ABSENCE_WITHOUT_HISTORY,
    VERSIONING_NONE,
    ErasureOutcome,
)


class ErasesWithoutHistory:
    """Gives a storage double the `erase()` the worker actually calls.

    The worker finishes a job through `erase`, not `delete`, because on a versioned bucket a
    delete only inserts a marker and erases nothing. `StorageProvider.erase` therefore refuses
    by default, so that an adapter which has not implemented version-aware erasure holds its
    jobs for an operator rather than quietly leaving the data in place.

    A double that inherited that refusal would hold every job in every test, which is why this
    exists -- and why it is a mixin rather than a default on the base class. Stating it per
    double keeps the refusal real for anything that has not stated it.

    What it promises is narrow and true of these doubles: they keep no version history at all,
    so deleting is erasing, and absence is sufficient evidence of it. That is the same
    reasoning `LocalStorageProvider.erase` relies on, and the outcome names it with
    `VERIFIED_BY_ABSENCE_WITHOUT_HISTORY` so it can never be read as an enumeration.

    The counts are left at zero deliberately. These doubles do not model versions, so a count
    would be a number invented to look plausible; nothing asserts on them, and the worker only
    logs them.
    """

    def erase(self, key: str) -> ErasureOutcome:
        # Delegates, so a subclass that overrides `delete` to fail or to swallow still behaves
        # that way through `erase` -- the doubles are written against `delete`.
        self.delete(key)
        return ErasureOutcome(
            versioning=VERSIONING_NONE, verified_by=VERIFIED_BY_ABSENCE_WITHOUT_HISTORY
        )
