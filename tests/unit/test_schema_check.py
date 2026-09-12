from app.core.config import settings
from app.core.schema_check import get_expected_head_revision, is_schema_current


def test_is_schema_current_matches() -> None:
    assert is_schema_current("abc123", "abc123") is True


def test_is_schema_current_mismatch() -> None:
    assert is_schema_current("old-revision", "new-revision") is False


def test_is_schema_current_untracked_is_treated_as_current_in_development() -> None:
    # None means the alembic_version table doesn't exist at all -- e.g. this project's own
    # test databases, created directly from SQLAlchemy metadata (tests/conftest.py) rather
    # than through Alembic. Not the "forgot to migrate" failure this check exists to catch.
    assert is_schema_current(None, "some-revision") is True


def test_get_expected_head_revision_reads_a_real_revision_from_the_versions_directory() -> None:
    head = get_expected_head_revision()
    assert isinstance(head, str)
    assert len(head) > 0


def test_an_absent_schema_is_not_ready_in_production(monkeypatch) -> None:
    """The same None, read the other way, because in production it can only mean one thing.

    Nothing in production builds tables from SQLAlchemy metadata -- Alembic is the only thing
    that ever creates the schema -- so a missing alembic_version table there means migrations
    have never been run at all.

    Answering "ready" to that is worse than answering it to a merely stale schema: the platform
    routes traffic to an instance with no tables, and every request fails on a missing relation.
    It is the exact failure this check exists to catch, arriving in its most severe form.
    """
    monkeypatch.setattr(settings, "environment", "production")

    assert is_schema_current(None, "some-revision") is False


def test_a_matching_revision_is_still_ready_in_production(monkeypatch) -> None:
    """The narrowing applies only to the absent case -- a correctly migrated production
    database must not be held back by it."""
    monkeypatch.setattr(settings, "environment", "production")

    assert is_schema_current("abc123", "abc123") is True
    assert is_schema_current("old", "new") is False
