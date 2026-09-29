"""Concurrent POST /career-path/generate, through the route a student's phone actually calls.

tests/unit/test_career_path_uniqueness_concurrency.py races the service. These race the endpoint,
because F12-03 arrives as HTTP: a retry sent after a lost response (Phase 14's F12-02), a second
device, a stale client. What the route adds over the service is the part the student sees -- the
status code and the body -- and the pieces between: authentication, the profile lookup that
decides whose path this is, the rate limiter, and the response schema.

The database is a real file, never conftest's in-memory one. That fixture uses StaticPool, which
hands every session the SAME connection, so two "concurrent" requests would share one transaction,
each would see the other's uncommitted INSERT, the constraint would never fire, and these tests
would pass no matter what the code did. Requests go through the app in threads, so the sync route
really does run twice at once in Starlette's threadpool.

raise_server_exceptions=False on purpose: a 500 is what this phase exists to remove, so it must be
assertable as a status code with its body in the failure message, rather than arriving as a
traceback from inside a worker thread.
"""

import os
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.ai.provider import get_ai_provider
from app.api.deps import get_db
from app.database.base import Base
from app.main import app
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from tests.fixtures.career_path_helpers import RacingProvider

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

PASSWORD = "supersecret123"
CUSTOM_ROLE = "Quantum Data Wrangler"
CURATED_ROLE = "Data Analyst"


@pytest.fixture()
def engine(tmp_path):
    if _TEST_DATABASE_URL:
        engine = create_engine(
            _TEST_DATABASE_URL, poolclass=NullPool, connect_args={"options": "-c lock_timeout=15000"}
        )
        Base.metadata.drop_all(bind=engine)
    else:
        # check_same_thread=False because the route runs in Starlette's threadpool, so a session
        # is used by a different thread than created it -- the same reason app/database/session.py
        # passes it. timeout=30 keeps a slow machine's lock contention from being mistaken for the
        # "database is locked" failure these tests are about.
        engine = create_engine(
            f"sqlite:///{tmp_path / 'career_path_api_race.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    seed = sessionmaker(bind=engine)()
    role = Role(name=CURATED_ROLE, category="Data", description="Data Analyst")
    seed.add(role)
    seed.commit()
    seed.add(RoleSkillWeight(role_id=role.id, skill="SQL", weight=1.0, assessment_type="sql"))
    seed.commit()
    seed.close()
    try:
        yield engine
    finally:
        if _TEST_DATABASE_URL:
            Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(engine):
    """The real app on a real database, with one session per request exactly as production has."""
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # The limiter's counters live on the shared app object, so a previous test's requests would
    # otherwise count against this one's quota. Same reason, and the same call, as conftest.
    app.state.limiter.reset()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def provider():
    racing = RacingProvider()
    app.dependency_overrides[get_ai_provider] = lambda: racing
    yield racing
    app.dependency_overrides.pop(get_ai_provider, None)


def signup(client) -> dict:
    email = f"{uuid.uuid4().hex}@example.com"
    assert client.post("/auth/signup", json={"email": email, "password": PASSWORD}).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # A real student has a profile long before they reach Career Path; creating it here keeps
    # get_or_create_profile's own first-request behaviour out of what these tests measure.
    assert client.get("/users/me", headers=headers).status_code == 200
    return headers


def generate_together(client, requests):
    """Send every (headers, payload) in `requests` at once; return the responses in order."""
    responses = [None] * len(requests)

    def send(index):
        headers, payload = requests[index]
        responses[index] = client.post("/career-path/generate", headers=headers, json=payload)

    threads = [threading.Thread(target=send, args=(i,), name=f"request-{i}") for i in range(len(requests))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)
    assert not any(thread.is_alive() for thread in threads), "a request never came back"
    return responses


def rows(engine, profile_id=None):
    with engine.connect() as connection:
        sql = "SELECT id, profile_id, role_family_key, is_primary FROM career_paths"
        if profile_id is not None:
            sql += " WHERE profile_id = :pid"
        return connection.execute(text(sql + " ORDER BY id"), {"pid": profile_id}).all()


def duplicates(engine):
    """The invariant query, run against the real table."""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT profile_id, role_family_key, COUNT(*) FROM career_paths"
                " GROUP BY profile_id, role_family_key HAVING COUNT(*) > 1"
            )
        ).all()


def describe(responses):
    def body(response):
        json_response = response.headers.get("content-type", "").startswith("application/json")
        return response.json() if json_response else response.text

    return [(response.status_code, body(response)) for response in responses]


@pytest.mark.parametrize("count", [2, 5])
def test_concurrent_generate_requests_return_the_same_path(client, engine, provider, count):
    """The whole point, over HTTP: every overlapping request is answered 201 with the same path,
    and the database holds one row.

    Before the constraint this produced a row per request on PostgreSQL, and on SQLite the second
    request failed with sqlite3.OperationalError: database is locked -- a 500 on a student who had
    simply tapped a role again after their connection dropped.
    """
    provider.barrier = threading.Barrier(count)
    headers = signup(client)

    responses = generate_together(client, [(headers, {"custom_role": CUSTOM_ROLE})] * count)

    assert [r.status_code for r in responses] == [201] * count, f"not every request succeeded: {describe(responses)}"
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1, f"requests were answered with different paths: {describe(responses)}"
    stored = rows(engine)
    assert len(stored) == 1, f"{count} concurrent requests stored {len(stored)} rows: {stored}"
    assert duplicates(engine) == []
    assert stored[0].id == ids.pop()
    assert bool(stored[0].is_primary) is True
    # The body is a complete path, not a stub: same schema as a single request returns.
    for response in responses:
        body = response.json()
        assert len(body["phases"]) == 5
        assert body["is_primary"] is True
        assert body["role_family_key"] == "custom:quantum_data_wrangler"
        assert sum(len(phase["skill_items"]) for phase in body["phases"]) > 0


def test_concurrent_generate_keeps_a_completed_skill(client, engine, provider):
    """The student's own work, across the race, through the API only.

    This is the audit's failure model end to end: complete a skill, then have two selections of
    that role overlap. The completed skill used to end up on a row that GET /career-path/current no
    longer returned -- still stored, and gone as far as the student could tell.
    """
    headers = signup(client)
    first = client.post("/career-path/generate", headers=headers, json={"custom_role": CURATED_ROLE})
    assert first.status_code == 201, first.text
    path_id = first.json()["id"]
    item = first.json()["phases"][0]["skill_items"][0]
    done = client.patch(f"/career-path/skill-items/{item['id']}", headers=headers, json={"status": "completed"})
    assert done.status_code == 200 and done.json()["status"] == "completed"

    provider.barrier = threading.Barrier(2)
    responses = generate_together(client, [(headers, {"custom_role": CURATED_ROLE})] * 2)

    assert [r.status_code for r in responses] == [201, 201], describe(responses)
    assert {r.json()["id"] for r in responses} == {path_id}
    current = client.get("/career-path/current", headers=headers)
    assert current.status_code == 200
    assert current.json()["id"] == path_id, "the current path changed identity across the race"
    statuses = {i["skill_key"]: i["status"] for phase in current.json()["phases"] for i in phase["skill_items"]}
    assert statuses[item["skill_key"]] == "completed", f"{item['skill_key']} came back as {statuses[item['skill_key']]}"
    listed = client.get("/career-path/all", headers=headers).json()
    assert len(listed) == 1, f"the role appears more than once in saved paths: {[p['display_role_name'] for p in listed]}"
    assert len(rows(engine)) == 1


def test_two_students_requesting_the_same_role_are_not_affected_by_each_other(client, engine, provider):
    """Ownership: the constraint is per profile, and the row each student gets is their own.

    If it were ever narrowed to the role alone, one student's request would be refused because
    another student had chosen the same role -- and the recovery would hand them that student's
    path, which is the worst possible outcome of getting this wrong.
    """
    provider.barrier = threading.Barrier(2)
    first, second = signup(client), signup(client)

    responses = generate_together(
        client, [(first, {"custom_role": CUSTOM_ROLE}), (second, {"custom_role": CUSTOM_ROLE})]
    )

    assert [r.status_code for r in responses] == [201, 201], describe(responses)
    mine, theirs = responses[0].json(), responses[1].json()
    assert mine["id"] != theirs["id"], "two students were handed the same career path row"
    stored = rows(engine)
    assert len(stored) == 2
    assert {row.profile_id for row in stored} == {1, 2}
    assert duplicates(engine) == []
    # Each student reads back their own, and cannot read the other's.
    assert client.get("/career-path/current", headers=first).json()["id"] == mine["id"]
    assert client.get("/career-path/current", headers=second).json()["id"] == theirs["id"]
    assert client.get(f"/career-path/{theirs['id']}", headers=first).status_code == 404


def test_the_generation_rate_limit_still_applies(client, engine, provider):
    """The limiter is untouched by any of this: ten generations an hour, then 429.

    Worth pinning here because the obvious way to make a concurrency test pass is to loosen the
    limit, and because a request that converges on an existing row is still a request -- it must
    count, or the limit could be sidestepped by racing.
    """
    headers = signup(client)
    for attempt in range(10):
        response = client.post("/career-path/generate", headers=headers, json={"custom_role": CURATED_ROLE})
        assert response.status_code == 201, f"generation {attempt + 1} failed: {response.text}"

    refused = client.post("/career-path/generate", headers=headers, json={"custom_role": CURATED_ROLE})

    assert refused.status_code == 429
    assert len(rows(engine)) == 1
