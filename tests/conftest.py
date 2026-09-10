import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.database.base import Base
from app.main import app
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.models.user import User  # noqa: F401  (registers the table with Base.metadata)


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite ignores foreign keys unless asked, per connection. Production is PostgreSQL,
    # which enforces them on every statement -- so without this the suite is structurally
    # unable to see a whole class of bug: deleting a row something still points at.
    #
    # It was hiding two, both of which produced a 500 in production and a pass here: deleting
    # a saved search that had an alert or a tracked job attached, and the placement-alarm
    # recovery path that deletes a challenge whose question-attempt rows were already
    # committed. Turning the pragma on was measured before being adopted -- those two were the
    # only failures across the whole suite, and both were real defects rather than fixture
    # artefacts.
    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # Minimal role + skill-weight seed for tests (mirrors the real Alembic seed migrations,
    # not the full list -- see alembic/versions/*_seed_default_role_skill_weights.py).
    seed_session = testing_session_local()
    sde = Role(name="SDE", category="Engineering", description="Software Development Engineer")
    data_analyst = Role(name="Data Analyst", category="Data", description="Data Analyst")
    seed_session.add_all([sde, data_analyst])
    seed_session.commit()
    seed_session.refresh(sde)
    seed_session.add_all(
        [
            RoleSkillWeight(role_id=sde.id, skill="Aptitude", weight=0.5, assessment_type="aptitude"),
            RoleSkillWeight(role_id=sde.id, skill="DSA", weight=1.0, assessment_type="coding"),
            RoleSkillWeight(role_id=sde.id, skill="OOP", weight=0.7, assessment_type="technical"),
            RoleSkillWeight(role_id=sde.id, skill="DBMS", weight=0.7, assessment_type="sql"),
            RoleSkillWeight(role_id=sde.id, skill="OS", weight=0.5, assessment_type="technical"),
            RoleSkillWeight(role_id=sde.id, skill="Networks", weight=0.5, assessment_type="technical"),
        ]
    )
    seed_session.commit()
    seed_session.close()

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # app is a single module-level object shared by every test in the session (imported
    # once), so the rate limiter's in-memory counters (app/core/rate_limit.py) would
    # otherwise leak across tests -- a rate-limit test consuming its quota could then cause
    # an unrelated later test hitting the same endpoint from the same client to fail.
    app.state.limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
