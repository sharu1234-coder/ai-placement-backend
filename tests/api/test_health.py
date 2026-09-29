"""Liveness and readiness.

These use the conftest `client` fixture rather than a module-level TestClient, which is what
readiness_check's own docstring says to do ("testable via the same dependency override every
other test already uses"). The distinction is not cosmetic: a bare TestClient(app) applies no
get_db override, so /health/ready queried the developer's real app.db and the test passed or
failed according to whether that local database happened to be at Alembic head. Phase 2G added
a migration and the test went red on a codebase that was entirely correct -- the check was
truthfully reporting an un-upgraded local database, which is exactly its job.

app/core/schema_check.py already documents the intended arrangement: test databases are built
from SQLAlchemy metadata and have no alembic_version table at all, and outside production that
means "can't tell, don't block" rather than a fault.
"""

from sqlalchemy import text

from app.api.deps import get_db
from app.main import app


def test_health_check(client) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "app" in body


def test_readiness_check_reports_database_reachable(client) -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "reachable"


def test_readiness_reports_503_when_the_schema_is_behind_the_code(client) -> None:
    """The failure this endpoint exists to catch, exercised deliberately.

    Before Phase 2G this path was only ever reached by accident -- when a developer's local
    app.db lagged head -- which made it look like a broken test rather than a working check.
    Pinning it here means the next migration cannot quietly disable the guard.
    """
    session = next(app.dependency_overrides[get_db]())
    try:
        session.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        session.execute(text("DELETE FROM alembic_version"))
        session.execute(text("INSERT INTO alembic_version (version_num) VALUES ('deadbeefcafe')"))
        session.commit()

        response = client.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["database"] == "reachable", "the database is fine -- it is the schema that is stale"
        assert body["schema"] == "out_of_date"
        assert body["current_revision"] == "deadbeefcafe"
        assert body["expected_revision"] != "deadbeefcafe"
    finally:
        session.execute(text("DROP TABLE IF EXISTS alembic_version"))
        session.commit()
        session.close()


def test_a_missing_alembic_version_does_not_block_readiness_outside_production(client) -> None:
    """The conftest database is built from metadata and never stamped, so this is the normal
    case for the whole test suite -- and it must not read as a fault."""
    response = client.get("/health/ready")

    assert response.status_code == 200
