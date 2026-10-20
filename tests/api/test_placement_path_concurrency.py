"""Concurrent POST /placement-path/generate, through the route the student's phone calls.

Phase 26. This endpoint is the most expensive one in the app -- a generation was measured at
~163 seconds against a local model -- and it is also a *regenerate*: its own docstring says it
"creates the student's first placement path, or regenerates it (superseding the previous one,
never deleting it) -- the same operation either way".

That combination is what makes it interesting. Phase 25's preparation plan could be protected by
reading first and returning what exists, because a job description has exactly one plan. Here
that would delete the feature. What has to be collapsed is *overlap*, not repetition:

  * two requests in flight at once are one generation;
  * a request that arrives after the previous one finished is a new one and may supersede.

The real client produces the first case without any contrivance. Onboarding generates the path
with its own key; if that request is lost in transport, the Placement Path screen finds no
current path and its Generate button sends a second one with a different key. Neither request can
see the other's key, so before this phase both ran the model and both left an active path -- and
`get_current_placement_path` then chose between them with an unordered `.first()`, so the
*database* decided which roadmap the student opened.

What makes these tests able to fail. The database is a real file, never conftest's in-memory one:
that fixture uses StaticPool, which hands every session the SAME connection, so two "concurrent"
requests would share one transaction, each would see the other's uncommitted INSERT, no
constraint could fire and these tests would pass whatever the code did. Requests go through the
app in threads, so the sync route really does run twice at once in Starlette's threadpool.

And what makes the race deterministic rather than hopeful: the provider holds the first
generation open until the main thread has watched every request take its own request-level claim.
That is the exact interleaving the defect needs -- every request past the "is another path
active?" read before any of them has committed one.

raise_server_exceptions=False on purpose: a 500 is one of the things this phase removes, so it
has to be assertable as a status code with its body in the failure message rather than arriving
as a traceback from inside a worker thread.
"""

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.placement_path import (
    GeneratedPlacementPathExplanations,
    GeneratedTopicExplanation,
)
from app.api.deps import get_db
from app.core.config import settings
from app.database.base import Base
from app.main import app
from app.models.profile import StudentProfile
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.user import User
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()

PASSWORD = "supersecret123"

#: Placement-relevant skills, so rules.match_domain_tracks produces tracks and the service does
#: not refuse with NoPlacementRelevantSkillsError before reaching anything under test.
SKILLS = ["SQL", "Python", "Data Structures", "Leadership"]

#: How long a gated generation will wait to be released, and how long the main thread will wait
#: for the claims it is gating on. Generous, because this bounds a *failing* test rather than a
#: passing one: a deadlock here must end as a readable assertion, not as a hung suite.
GATE_TIMEOUT = 60


class GatedCountingProvider(AIProvider):
    """Counts placement-path explanation generations, and can hold them open.

    The count is the measurement this phase is really about. Two active paths are a visible
    symptom; two ~163-second model calls are the cost, and they are spent before either row is
    inserted -- so a test that only counted rows would pass against a fix that still paid twice.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.calls = 0
        self.gate = threading.Event()
        self.gated = False

    def generate_structured(self, prompt, schema):
        if schema is GeneratedPlacementPathExplanations:
            with self.lock:
                self.calls += 1
                number = self.calls
            if self.gated:
                assert self.gate.wait(timeout=GATE_TIMEOUT), "the generation gate was never released"
            return GeneratedPlacementPathExplanations(
                explanations=[
                    GeneratedTopicExplanation(
                        topic_key="sql_basics",
                        why_it_matters=f"Generation #{number} says SQL matters.",
                    )
                ]
            )
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def engine(tmp_path):
    if _TEST_DATABASE_URL:
        eng = create_engine(
            _TEST_DATABASE_URL, poolclass=NullPool, connect_args={"options": "-c lock_timeout=15000"}
        )
        db_safety.drop_all_safely(eng)
    else:
        # check_same_thread=False because the route runs in Starlette's threadpool, so a session
        # is used by a different thread than created it -- the same reason app/database/session.py
        # passes it. timeout=30 keeps a slow machine's lock contention from being mistaken for the
        # "database is locked" failure this design exists to avoid.
        eng = create_engine(
            f"sqlite:///{tmp_path / 'placement_path_race.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(eng, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        if _TEST_DATABASE_URL:
            db_safety.drop_all_safely(eng)
        eng.dispose()


@pytest.fixture()
def sessions(engine):
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def client(engine, sessions, tmp_path, monkeypatch):
    """The real app on a real database, with one session per request exactly as production has."""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))

    def override_get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # The limiter's counters live on the shared app object, so a previous test's requests would
    # otherwise count against this one's quota -- and generate is capped at 10/hour, which the
    # ten-concurrent test sits exactly on. Same reason, and the same call, as conftest.
    app.state.limiter.reset()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture()
def provider():
    counting = GatedCountingProvider()
    app.dependency_overrides[get_ai_provider] = lambda: counting
    yield counting
    app.dependency_overrides.pop(get_ai_provider, None)


def signup(client, sessions, skills=SKILLS) -> dict:
    """A student with a parsed resume, which is the precondition for generating a path at all."""
    email = f"{uuid.uuid4().hex}@example.com"
    assert client.post("/auth/signup", json={"email": email, "password": PASSWORD}).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # A real student has a profile long before they reach a placement path; creating it here keeps
    # get_or_create_profile's own first-request behaviour out of what these tests measure.
    assert client.get("/users/me", headers=headers).status_code == 200

    db = sessions()
    try:
        user = db.query(User).filter(User.email == email).one()
        profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).one()
        resume = Resume(
            profile_id=profile.id, file_path="x.pdf", raw_text="text", parse_status="completed"
        )
        db.add(resume)
        db.commit()
        db.refresh(resume)
        db.add(
            ResumeAnalysis(
                resume_id=resume.id,
                extracted_skills=skills,
                extracted_education=[],
                extracted_experience=[],
                extracted_projects=[],
                extracted_certifications=[],
                ats_score=70.0,
                weak_sections=[],
                extraction_confidence=0.9,
            )
        )
        db.commit()
        return headers, profile.id
    finally:
        db.close()


def key() -> str:
    """A distinct client key per request -- the whole premise of this file.

    Phase 24 already collapses two transmissions that share one. These are different requests, so
    they carry different keys, and the request layer is right to let both through.
    """
    return f"and-{uuid.uuid4().hex[:16]}"


def request_claims(engine) -> int:
    """How many requests have taken a request-level claim for this operation.

    Excludes the server's own derived business claim, whose key cannot start any other way -- '@'
    is not a character a client key may contain, which is what the forged-key test pins down.
    """
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT COUNT(*) FROM operation_claims"
                " WHERE operation = 'placement_path.generate'"
                " AND idempotency_key NOT LIKE 'biz@%'"
            )
        ).scalar_one()


def paths(engine, profile_id=None):
    with engine.connect() as connection:
        sql = "SELECT id, profile_id, status FROM placement_paths"
        if profile_id is not None:
            sql += " WHERE profile_id = :pid"
        return connection.execute(text(sql + " ORDER BY id"), {"pid": profile_id}).all()


def active(engine, profile_id=None):
    return [row for row in paths(engine, profile_id) if row.status == "active"]


def duplicates(engine):
    """The invariant query, run against the real table."""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT profile_id, COUNT(*) FROM placement_paths WHERE status = 'active'"
                " GROUP BY profile_id HAVING COUNT(*) > 1"
            )
        ).all()


def business_claims(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT idempotency_key, status, result_id FROM operation_claims"
                " WHERE idempotency_key LIKE 'biz@%' ORDER BY id"
            )
        ).all()


def await_claims(engine, expected, deadline=GATE_TIMEOUT) -> int:
    """Block until `expected` requests have each taken their own request claim.

    This is the gate that makes the race real. Counting claims rather than sleeping means the
    generation is held open until every request has provably entered the endpoint and passed the
    request-idempotency layer, which is the only interleaving in which the business guard is the
    thing under test.
    """
    limit = time.monotonic() + deadline
    seen = 0
    while time.monotonic() < limit:
        seen = request_claims(engine)
        if seen >= expected:
            return seen
        time.sleep(0.05)
    raise AssertionError(f"only {seen} of {expected} requests ever claimed")


def describe(responses):
    def body(response):
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return response.json() if is_json else response.text

    return [(response.status_code, body(response)) for response in responses]


def race(client, engine, provider, headers, count):
    """Fire `count` generate requests at once, each with its own client key."""
    provider.gated = True
    responses = [None] * count
    keys = [key() for _ in range(count)]

    def send(index):
        responses[index] = client.post(
            "/placement-path/generate",
            headers={**headers, "Idempotency-Key": keys[index]},
        )

    threads = [threading.Thread(target=send, args=(i,), name=f"generate-{i}") for i in range(count)]
    for thread in threads:
        thread.start()
    try:
        await_claims(engine, count)
    finally:
        provider.gate.set()
    for thread in threads:
        thread.join(timeout=GATE_TIMEOUT + 30)
    assert not any(thread.is_alive() for thread in threads), "a request never came back"
    return responses


# --- the central invariant ------------------------------------------------------------------


@pytest.mark.parametrize("count", [2, 5])
def test_concurrent_generations_produce_one_path_and_one_generation(
    client, engine, sessions, provider, count
):
    """Different keys, one profile: one model call and one active path.

    The model count is the assertion that matters. Before the business claim every one of these
    requests ran the longest AI call in the app -- the rows were the symptom, the generations were
    the bill.
    """
    headers, profile_id = signup(client, sessions)

    responses = race(client, engine, provider, headers, count)

    stored_active = active(engine, profile_id)
    # Both numbers in one message, deliberately. The model count is what the defect costs and the
    # row count is what it leaves behind, and an assertion that reported only the first would make
    # the second invisible on exactly the run where it matters.
    assert (provider.calls, len(stored_active)) == (1, 1), (
        f"{count} concurrent requests ran the model {provider.calls} time(s) and left "
        f"{len(stored_active)} active path(s) {[r.id for r in stored_active]}; "
        f"responses were {describe(responses)}"
    )
    assert duplicates(engine) == []
    assert all(r.status_code == 201 for r in responses), describe(responses)
    returned = {r.json()["id"] for r in responses}
    assert returned == {stored_active[0].id}, (
        f"every request must be answered with the one authoritative path; got {returned}"
    )
    assert len(business_claims(engine)) == 1, business_claims(engine)


def test_ten_concurrent_generations_still_produce_one_path_and_one_generation(
    client, engine, sessions, provider
):
    """The same invariant under the heaviest load the brief asks for.

    Ten is also where a design that merely retried on conflict would show itself, by spending ten
    of the most expensive generations in the app to keep one.
    """
    headers, profile_id = signup(client, sessions)

    responses = race(client, engine, provider, headers, 10)

    stored_active = active(engine, profile_id)
    assert (provider.calls, len(stored_active)) == (1, 1), (
        f"ten requests ran the model {provider.calls} time(s) and left "
        f"{len(stored_active)} active path(s)"
    )
    assert all(r.status_code == 201 for r in responses), describe(responses)


def test_the_read_path_returns_the_authoritative_active_path(
    client, engine, sessions, provider
):
    """Whichever request wins, no request is answered with a path the GET would not return.

    This is the half a row count alone misses. With two active rows the old read selected between
    them with an unordered .first(), so some students were handed a roadmap that the next request
    might not return -- divergence that looks fine in the response and wrong on reopen.
    """
    headers, profile_id = signup(client, sessions)

    responses = race(client, engine, provider, headers, 3)

    current = client.get("/placement-path/current", headers=headers).json()
    stored_active = active(engine, profile_id)
    assert len(stored_active) == 1
    assert current["id"] == stored_active[0].id
    assert {r.json()["id"] for r in responses} == {current["id"]}


# --- regeneration, which this endpoint exists to do ----------------------------------------


def test_a_later_deliberate_regeneration_still_generates_and_supersedes(
    client, engine, sessions, provider
):
    """The feature a read-first guard would have deleted.

    POST /placement-path/generate is a regenerate. A second deliberate request -- one that arrives
    after the first finished, rather than overlapping it -- must run the model again, supersede
    the old path and return a new one. If this ever returns the first path, the Generate button in
    the app silently does nothing.
    """
    headers, profile_id = signup(client, sessions)
    provider.gated = False

    first = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})
    assert first.status_code == 201, first.text
    second = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})

    assert second.status_code == 201, second.text
    assert provider.calls == 2, "a deliberate regeneration is a second operation"
    assert second.json()["id"] != first.json()["id"]
    all_paths = paths(engine, profile_id)
    assert len(all_paths) == 2, all_paths
    assert [r.status for r in all_paths] == ["superseded", "active"], (
        f"the old path is superseded, never deleted: {all_paths}"
    )
    assert active(engine, profile_id)[0].id == second.json()["id"]


def test_regeneration_still_works_after_a_concurrent_collapse(
    client, engine, sessions, provider
):
    """The combination, which is where a claim-reuse bug would hide.

    The business claim is one row per profile that gets taken over again and again. After it has
    been settled by a collapsed race, the next deliberate request has to be able to reclaim it --
    otherwise the race permanently freezes the student's roadmap.
    """
    headers, profile_id = signup(client, sessions)
    race(client, engine, provider, headers, 3)
    assert provider.calls == 1

    provider.gated = False
    provider.gate.clear()
    again = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})

    assert again.status_code == 201, again.text
    assert provider.calls == 2
    assert len(active(engine, profile_id)) == 1
    assert active(engine, profile_id)[0].id == again.json()["id"]
    assert len(paths(engine, profile_id)) == 2
    claims = business_claims(engine)
    assert len(claims) == 1, f"the claim is reused, not accumulated: {claims}"
    assert claims[0].status == "succeeded"


# --- the Phase 24 layer must still work exactly as it did -----------------------------------


def test_a_transport_replay_of_one_request_still_generates_once(
    client, engine, sessions, provider
):
    """Window E, unchanged: the same key arriving twice is one generation and one path.

    Phase 26 adds a layer underneath this one; it must not have moved it. The replay is answered
    from the request claim, so it never reaches the business guard at all -- and because this
    endpoint is a regenerate, a broken replay here would cost 163 seconds *and* retire the path
    the student had just been given.
    """
    headers, profile_id = signup(client, sessions)
    provider.gated = False
    same_key = key()

    first = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": same_key})
    second = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": same_key})

    assert (first.status_code, second.status_code) == (201, 201), describe([first, second])
    assert provider.calls == 1, "a retransmission must not buy a second generation"
    assert first.json()["id"] == second.json()["id"]
    assert len(paths(engine, profile_id)) == 1, "and must not retire the path it just produced"


def test_a_client_cannot_send_the_servers_own_business_claim_key(
    client, engine, sessions, provider
):
    """The derived key is unforgeable, so no request can hijack or be hijacked by a business claim.

    It is built with '@', which `_KEY_PATTERN` rejects, so a client that guesses the format
    exactly is refused as malformed before the key is ever looked up. Without this the guard would
    be reachable from outside: a client could take the business claim for its own profile and
    every later request would wait on a generation nobody was running.
    """
    headers, profile_id = signup(client, sessions)

    forged = client.post(
        "/placement-path/generate",
        headers={**headers, "Idempotency-Key": f"biz@placement_path.generate@profile_id={profile_id}"},
    )

    assert forged.status_code == 400, describe([forged])
    assert provider.calls == 0, "a forged key must not reach the model"
    assert paths(engine, profile_id) == []


# --- independent identities, and ownership --------------------------------------------------


def test_two_students_generate_independently(client, engine, sessions, provider):
    """Different profile: two paths, two generations, and neither sees the other.

    The claim key is only unique within `UNIQUE (profile_id, idempotency_key)`, so two students
    deriving the identical business key is expected and must not collapse them into one operation.
    """
    first_headers, first_profile = signup(client, sessions)
    second_headers, second_profile = signup(client, sessions)
    provider.gated = False

    first = client.post("/placement-path/generate", headers={**first_headers, "Idempotency-Key": key()})
    second = client.post("/placement-path/generate", headers={**second_headers, "Idempotency-Key": key()})

    assert (first.status_code, second.status_code) == (201, 201), describe([first, second])
    assert provider.calls == 2, "two students are two business operations"
    assert len(active(engine, first_profile)) == 1
    assert len(active(engine, second_profile)) == 1
    assert first.json()["id"] != second.json()["id"]
    claims = business_claims(engine)
    assert len({c.idempotency_key for c in claims}) == 2, (
        f"each student must derive their own business claim; got {claims}. One shared key would "
        f"make every student queue behind the others."
    )


def test_one_students_generation_never_returns_another_students_path(
    client, engine, sessions, provider
):
    """Ownership, raced rather than only asserted.

    The business guard is the first thing in this endpoint keyed on something other than the
    caller's key, so it is exactly where a profile-blind lookup would hand one student another's
    roadmap.
    """
    first_headers, first_profile = signup(client, sessions)
    second_headers, second_profile = signup(client, sessions)
    provider.gated = False

    first = client.post("/placement-path/generate", headers={**first_headers, "Idempotency-Key": key()})
    second = client.post("/placement-path/generate", headers={**second_headers, "Idempotency-Key": key()})

    first_current = client.get("/placement-path/current", headers=first_headers).json()
    second_current = client.get("/placement-path/current", headers=second_headers).json()

    assert first_current["id"] == first.json()["id"]
    assert second_current["id"] == second.json()["id"]
    assert first_current["id"] != second_current["id"]
    assert active(engine, first_profile)[0].id != active(engine, second_profile)[0].id


def test_two_students_racing_at_once_each_get_their_own_path(
    client, engine, sessions, provider
):
    """Both students' duplicates collapse, and they collapse separately."""
    first_headers, first_profile = signup(client, sessions)
    second_headers, second_profile = signup(client, sessions)
    provider.gated = True
    responses = {}

    def send(label, headers):
        responses[label] = client.post(
            "/placement-path/generate", headers={**headers, "Idempotency-Key": key()}
        )

    threads = [
        threading.Thread(target=send, args=(f"{label}-{i}", headers))
        for label, headers in (("first", first_headers), ("second", second_headers))
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    try:
        await_claims(engine, 4)
    finally:
        provider.gate.set()
    for thread in threads:
        thread.join(timeout=GATE_TIMEOUT + 30)

    assert provider.calls == 2, f"one generation per student, not {provider.calls}"
    assert len(active(engine, first_profile)) == 1
    assert len(active(engine, second_profile)) == 1
    assert duplicates(engine) == []


# --- the constraint, and failure ------------------------------------------------------------


def test_the_table_itself_refuses_a_second_active_path(client, engine, sessions, provider):
    """Two mechanisms, because neither is sufficient alone.

    The claim is what stops the second 163-second generation from starting. The index is what
    makes "one active path" true of the table, including down a path that never consults the
    guard -- and, unlike the claim, it is not swept after RETENTION.
    """
    headers, profile_id = signup(client, sessions)
    provider.gated = False
    created = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})
    assert created.status_code == 201, created.text

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO placement_paths (profile_id, status, created_at)"
                    " VALUES (:pid, 'active', :now)"
                ),
                {"pid": profile_id, "now": "2026-10-04 12:00:00"},
            )

    # A superseded row is history and must still be insertable, or the index would forbid the
    # regeneration this endpoint exists to perform.
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO placement_paths (profile_id, status, created_at)"
                " VALUES (:pid, 'superseded', :now)"
            ),
            {"pid": profile_id, "now": "2026-10-04 12:00:00"},
        )
    assert len(active(engine, profile_id)) == 1


def test_a_generation_that_fails_outright_leaves_the_path_generatable(
    client, engine, sessions, provider
):
    """A failed attempt must not leave the profile permanently claimed.

    The provider failing inside generation is already handled -- explanations fall back to a
    deterministic sentence -- so the failure injected here is the harder one: the whole operation
    raising, which must release the business claim rather than settle it, and must leave the
    student's existing path alone.
    """
    headers, profile_id = signup(client, sessions)
    provider.gated = False

    import app.services.placement_path_service as service

    original = service.generate_placement_path
    calls = {"n": 0}

    def exploding(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("the database went away mid-generation")

    service.generate_placement_path = exploding
    try:
        broken = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})
    finally:
        service.generate_placement_path = original

    assert broken.status_code == 500, describe([broken])
    assert calls["n"] == 1
    assert paths(engine, profile_id) == [], "a failure must leave no path behind"

    retry = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})

    assert retry.status_code == 201, f"a failed generation must stay retryable: {retry.text}"
    assert len(active(engine, profile_id)) == 1
    claims = business_claims(engine)
    assert [c.status for c in claims] == ["succeeded"], (
        f"the business claim was reused, not duplicated: {claims}"
    )


def test_a_student_with_no_usable_resume_is_refused_without_holding_a_claim(
    client, engine, sessions, provider
):
    """The 422 paths raise before the model runs, and must not leave a claim behind.

    Worth pinning: both refusals happen inside the business claim, so a release that did not fire
    would lock the student out of generating a path once they *did* upload a resume.
    """
    headers, profile_id = signup(client, sessions, skills=["Cooking", "Photography"])
    provider.gated = False

    refused = client.post("/placement-path/generate", headers={**headers, "Idempotency-Key": key()})

    assert refused.status_code == 422, describe([refused])
    assert provider.calls == 0
    claims = business_claims(engine)
    assert [c.status for c in claims] == ["failed"], claims
    assert paths(engine, profile_id) == []
