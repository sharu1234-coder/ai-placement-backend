import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.database.base import Base
from app.main import app
from app.models.role import Role
from app.models.user import User  # noqa: F401  (registers the table with Base.metadata)


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    # Minimal role seed for tests (mirrors the real Alembic seed migration, not the full list).
    seed_session = testing_session_local()
    seed_session.add_all(
        [
            Role(name="SDE", category="Engineering", description="Software Development Engineer"),
            Role(name="Data Analyst", category="Data", description="Data Analyst"),
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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
