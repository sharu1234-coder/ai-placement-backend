from app.core.schema_check import get_expected_head_revision, is_schema_current


def test_is_schema_current_matches() -> None:
    assert is_schema_current("abc123", "abc123") is True


def test_is_schema_current_mismatch() -> None:
    assert is_schema_current("old-revision", "new-revision") is False


def test_is_schema_current_untracked_is_treated_as_current() -> None:
    # None means the alembic_version table doesn't exist at all -- e.g. this project's own
    # test databases, created directly from SQLAlchemy metadata (tests/conftest.py) rather
    # than through Alembic. Not the "forgot to migrate" failure this check exists to catch.
    assert is_schema_current(None, "some-revision") is True


def test_get_expected_head_revision_reads_a_real_revision_from_the_versions_directory() -> None:
    head = get_expected_head_revision()
    assert isinstance(head, str)
    assert len(head) > 0
