"""Concurrent POST /jobs/{jd_id}/preparation-plan, through the route a student's phone calls.

Phase 25. Phase 24 made one *request* safe to retransmit: same Idempotency-Key, same fingerprint,
one mutation. This file is about the case that mechanism cannot reach -- two requests that are
genuinely different requests, with different keys, both asking for the one plan that belongs to
(this student, this job description):

  * the analyse flow posts (company, title) and the result screen's 404 recovery posts
    (null, null), so the two Android callers do not even send the same body;
  * a student whose first attempt timed out in transport reopens the job from their saved list;
  * two devices.

Neither request can see the other's key, so before this phase both ran the model and both
inserted. The student paid twice and the table kept a row no read path could reach.

What makes these tests able to fail. The database is a real file, never conftest's in-memory one:
that fixture uses StaticPool, which hands every session the SAME connection, so two "concurrent"
requests would share one transaction, each would see the other's uncommitted INSERT, no constraint
could fire and these tests would pass whatever the code did. Requests go through the app in
threads, so the sync route really does run twice at once in Starlette's threadpool.

And what makes the race deterministic rather than hopeful: the provider holds the first generation
open until the main thread has watched every request take its own request-level claim. That is the
exact interleaving the defect needs -- every request past the "does a plan exist yet?" read before
any of them has committed one -- so a run that reports one AI call is reporting that the guard
worked, not that the threads happened not to overlap.

raise_server_exceptions=False on purpose: a 500 is one of the things this phase removes, so it has
to be assertable as a status code with its body in the failure message rather than arriving as a
traceback from inside a worker thread.
"""

import os
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
from app.ai.schemas.jd import AIParsedJD
from app.ai.schemas.job_preparation_plan import GeneratedJobPreparationContent
from app.api.deps import get_db
from app.core.config import settings
from app.database.base import Base
from app.main import app
from tests.api.test_jobs import SAMPLE_PARSED_JD, SAMPLE_PREP_CONTENT

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

PASSWORD = "supersecret123"
JD_TEXT = "We need SQL, Excel and Power BI for dashboards."

#: How long a gated generation will wait to be released, and how long the main thread will wait
#: for the claims it is gating on. Generous, because this bounds a failing test rather than a
#: passing one: a deadlock here must end as a readable assertion, not as a hung suite.
GATE_TIMEOUT = 60


class GatedCountingProvider(AIProvider):
    """Counts preparation-plan generations, and can hold them open.

    The count is the measurement this phase is really about. Two plan rows are a visible symptom;
    two model calls are the cost, and they are spent before either row is inserted, so a test that
    only counted rows would pass against a fix that still paid twice.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.plan_calls = 0
        self.jd_calls = 0
        self.prompts: list[str] = []
        self.gate = threading.Event()
        self.gated = False
        self.fail_plans = False

    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            with self.lock:
                self.jd_calls += 1
            return SAMPLE_PARSED_JD
        if schema is GeneratedJobPreparationContent:
            with self.lock:
                self.plan_calls += 1
                self.prompts.append(prompt)
            if self.gated:
                assert self.gate.wait(timeout=GATE_TIMEOUT), "the generation gate was never released"
            if self.fail_plans:
                from app.ai.provider import AIGenerationError

                raise AIGenerationError("provider unavailable")
            return SAMPLE_PREP_CONTENT
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


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
        # "database is locked" failure this design exists to avoid.
        engine = create_engine(
            f"sqlite:///{tmp_path / 'prep_plan_race.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        if _TEST_DATABASE_URL:
            Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(engine, tmp_path, monkeypatch):
    """The real app on a real database, with one session per request exactly as production has."""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
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
    counting = GatedCountingProvider()
    app.dependency_overrides[get_ai_provider] = lambda: counting
    yield counting
    app.dependency_overrides.pop(get_ai_provider, None)


def signup(client) -> dict:
    email = f"{uuid.uuid4().hex}@example.com"
    assert client.post("/auth/signup", json={"email": email, "password": PASSWORD}).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # A real student has a profile long before they reach a job description; creating it here keeps
    # get_or_create_profile's own first-request behaviour out of what these tests measure.
    assert client.get("/users/me", headers=headers).status_code == 200
    return headers


def parse_jd(client, headers, text_body=JD_TEXT) -> int:
    response = client.post("/jobs", json={"raw_text": text_body}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


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
                " WHERE operation = 'jd.preparation_plan' AND idempotency_key NOT LIKE 'biz@%'"
            )
        ).scalar_one()


def plans(engine, jd_id=None):
    with engine.connect() as connection:
        sql = "SELECT id, profile_id, jd_id, company_name FROM job_preparation_plans"
        if jd_id is not None:
            sql += " WHERE jd_id = :jd"
        return connection.execute(text(sql + " ORDER BY id"), {"jd": jd_id}).all()


def duplicates(engine):
    """The invariant query, run against the real table."""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT profile_id, jd_id, COUNT(*) FROM job_preparation_plans"
                " GROUP BY profile_id, jd_id HAVING COUNT(*) > 1"
            )
        ).all()


def post_together(client, requests):
    """Send every (jd_id, headers, payload, key) at once; return the responses in order."""
    responses = [None] * len(requests)

    def send(index):
        jd_id, headers, payload, request_key = requests[index]
        responses[index] = client.post(
            f"/jobs/{jd_id}/preparation-plan",
            json=payload,
            headers={**headers, "Idempotency-Key": request_key},
        )

    threads = [
        threading.Thread(target=send, args=(i,), name=f"prep-request-{i}")
        for i in range(len(requests))
    ]
    for thread in threads:
        thread.start()
    return threads, responses


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


def join_all(threads, responses):
    for thread in threads:
        thread.join(timeout=GATE_TIMEOUT + 30)
    assert not any(thread.is_alive() for thread in threads), "a request never came back"
    return responses


def describe(responses):
    def body(response):
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return response.json() if is_json else response.text

    return [(response.status_code, body(response)) for response in responses]


def race(client, engine, provider, jd_id, headers, payloads):
    """Fire one request per payload at the same job description, with distinct keys."""
    provider.gated = True
    requests = [(jd_id, headers, payload, key()) for payload in payloads]
    threads, responses = post_together(client, requests)
    try:
        await_claims(engine, len(requests))
    finally:
        provider.gate.set()
    return join_all(threads, responses)


# --- the central invariant ------------------------------------------------------------------


@pytest.mark.parametrize("count", [2, 5])
def test_concurrent_requests_for_one_jd_produce_one_plan_and_one_generation(
    client, engine, provider, count
):
    """Different keys, one business identity: one model call and one plan.

    The AI count is the assertion that matters. Before the business claim every one of these
    requests ran the model -- the rows were the symptom, the generations were the bill.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)
    payloads = [{"company_name": f"Company {i}", "job_title": f"Role {i}"} for i in range(count)]

    responses = race(client, engine, provider, jd_id, headers, payloads)

    stored = plans(engine, jd_id)
    # Both numbers in one message, deliberately. The model count is what the defect costs and the
    # row count is what it leaves behind, and an assertion that reported only the first would make
    # the second invisible on exactly the run where it matters.
    assert (provider.plan_calls, len(stored)) == (1, 1), (
        f"{count} concurrent requests ran the model {provider.plan_calls} time(s) and stored "
        f"{len(stored)} plan(s) {[row.id for row in stored]}; responses were {describe(responses)}"
    )
    assert duplicates(engine) == []
    assert all(r.status_code == 201 for r in responses), describe(responses)
    returned = {r.json()["id"] for r in responses}
    assert returned == {stored[0].id}, (
        f"every request must be answered with the one authoritative plan; got {returned}"
    )


def test_ten_concurrent_requests_still_produce_one_plan_and_one_generation(
    client, engine, provider
):
    """The same invariant under the heaviest load the brief asks for.

    Ten is also where a design that merely retried on conflict would show itself, by spending ten
    generations to keep one.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)

    responses = race(client, engine, provider, jd_id, headers, [{} for _ in range(10)])

    stored = plans(engine, jd_id)
    assert (provider.plan_calls, len(stored)) == (1, 1), (
        f"ten requests ran the model {provider.plan_calls} time(s) and stored {len(stored)} plan(s)"
    )
    assert all(r.status_code == 201 for r in responses), describe(responses)


def test_the_winning_plan_is_the_one_every_request_is_answered_with(client, engine, provider):
    """Whichever request wins, no request is answered with a row that is not the stored one.

    The read path takes the newest plan for a (profile, jd). A design that let a loser insert and
    then answered from its own in-memory result would hand some students a plan the next GET would
    not return -- the kind of divergence that looks fine in the response and wrong on reopen.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)

    responses = race(
        client, engine, provider, jd_id, headers,
        [{"company_name": "Acme"}, {"company_name": "Globex"}],
    )

    stored = plans(engine, jd_id)
    assert len(stored) == 1
    reread = client.get(f"/jobs/{jd_id}/preparation-plan", headers=headers).json()
    assert reread["id"] == stored[0].id
    assert {r.json()["id"] for r in responses} == {reread["id"]}
    assert reread["company_name"] == stored[0].company_name


# --- the Phase 24 layer must still work exactly as it did -----------------------------------


def test_a_transport_replay_of_one_request_still_runs_the_model_once(client, engine, provider):
    """Window E, unchanged: same key arriving twice is one generation and one plan.

    Phase 25 adds a layer underneath this one; it must not have moved it. The replay is answered
    from the request claim, so it never reaches the business guard at all.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)
    same_key = key()

    first = client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": same_key}
    )
    second = client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": same_key}
    )

    assert (first.status_code, second.status_code) == (201, 201), describe([first, second])
    assert provider.plan_calls == 1, "a retransmission must not buy a second generation"
    assert len(plans(engine, jd_id)) == 1
    assert first.json()["id"] == second.json()["id"]


def test_the_same_key_with_a_different_body_is_still_a_conflict(client, engine, provider):
    """Phase 24's fingerprint check, which the business layer must not have softened.

    A key is a re-entry token for one request. Reusing it for a different one has to be refused,
    or a client could be handed an unrelated operation's result -- and "they are both about job
    description 7" is not a reason to answer the second with the first.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)
    same_key = key()

    client.post(
        f"/jobs/{jd_id}/preparation-plan",
        json={"company_name": "Acme"},
        headers={**headers, "Idempotency-Key": same_key},
    )
    conflict = client.post(
        f"/jobs/{jd_id}/preparation-plan",
        json={"company_name": "Globex"},
        headers={**headers, "Idempotency-Key": same_key},
    )

    assert conflict.status_code == 409, describe([conflict])


def test_a_client_cannot_send_the_servers_own_business_claim_key(client, engine, provider):
    """The derived key is unforgeable, so no request can hijack or be hijacked by a business claim.

    It is built with '@', which `_KEY_PATTERN` rejects, so a client that guesses the format exactly
    is refused as malformed before the key is ever looked up. Without this the whole guard would be
    reachable from outside: a client could take the business claim for a job description and every
    later request would wait on an operation nobody was running.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)

    forged = client.post(
        f"/jobs/{jd_id}/preparation-plan",
        json={},
        headers={**headers, "Idempotency-Key": f"biz@jd.preparation_plan@jd_id={jd_id}"},
    )

    assert forged.status_code == 400, describe([forged])
    assert provider.plan_calls == 0, "a forged key must not reach the model"
    assert plans(engine, jd_id) == []


# --- independent business identities stay independent ---------------------------------------


def test_two_job_descriptions_generate_independently(client, engine, provider):
    """Different JD, same student: two plans, two generations. The guard is per resource."""
    headers = signup(client)
    first_jd = parse_jd(client, headers, "First posting: SQL and Excel.")
    second_jd = parse_jd(client, headers, "Second posting: Python and Airflow.")

    provider.gated = False
    first = client.post(
        f"/jobs/{first_jd}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": key()}
    )
    second = client.post(
        f"/jobs/{second_jd}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": key()}
    )

    assert (first.status_code, second.status_code) == (201, 201), describe([first, second])
    assert provider.plan_calls == 2, "two job descriptions are two business operations"
    assert len(plans(engine)) == 2
    assert first.json()["id"] != second.json()["id"]
    with engine.connect() as connection:
        derived = connection.execute(
            text("SELECT idempotency_key FROM operation_claims WHERE idempotency_key LIKE 'biz@%'")
        ).scalars().all()
    assert len(set(derived)) == 2, (
        f"each job description must derive its own business claim; got {derived}. One shared key "
        f"would make every posting a student saved queue behind the others."
    )


def test_two_students_with_their_own_job_descriptions_generate_independently(
    client, engine, provider
):
    """Different profile: two plans, two generations, and neither sees the other.

    The claim key is only unique within `UNIQUE (profile_id, idempotency_key)`, so two students
    deriving the identical business key is expected and must not collapse them into one operation.
    """
    first_headers = signup(client)
    second_headers = signup(client)
    first_jd = parse_jd(client, first_headers)
    second_jd = parse_jd(client, second_headers)

    provider.gated = False
    first = client.post(
        f"/jobs/{first_jd}/preparation-plan",
        json={"company_name": "Acme"},
        headers={**first_headers, "Idempotency-Key": key()},
    )
    second = client.post(
        f"/jobs/{second_jd}/preparation-plan",
        json={"company_name": "Globex"},
        headers={**second_headers, "Idempotency-Key": key()},
    )

    assert (first.status_code, second.status_code) == (201, 201), describe([first, second])
    assert provider.plan_calls == 2
    stored = plans(engine)
    assert len({row.profile_id for row in stored}) == 2
    assert first.json()["company_name"] == "Acme"
    assert second.json()["company_name"] == "Globex"


def test_one_students_request_never_receives_another_students_plan(client, engine, provider):
    """Ownership, unchanged: a job description that is not yours is a 404, not someone's plan.

    Worth racing as well as asserting, because the business guard is the first thing in this
    endpoint that is keyed on a resource rather than on the caller -- a guard scoped to the job
    description alone would have handed the second student the first student's plan.
    """
    owner = signup(client)
    stranger = signup(client)
    jd_id = parse_jd(client, owner)

    provider.gated = False
    assert client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**owner, "Idempotency-Key": key()}
    ).status_code == 201
    intruder = client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**stranger, "Idempotency-Key": key()}
    )

    assert intruder.status_code == 404, describe([intruder])
    assert client.get(f"/jobs/{jd_id}/preparation-plan", headers=stranger).status_code == 404
    assert len(plans(engine, jd_id)) == 1


# --- a completed plan is cheap, and a failed one is retryable -------------------------------


def test_a_later_request_returns_the_stored_plan_without_running_the_model(
    client, engine, provider
):
    """The common case after this phase, and the cheap one (section 35).

    A second deliberate request -- a new key, a new body -- is answered from the stored plan with
    no claim insert and no model call. This is also what makes the invariant survive claim
    retention: the answer comes from the plan, which is permanent, not from the claim, which is
    swept.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)
    provider.gated = False
    first = client.post(
        f"/jobs/{jd_id}/preparation-plan",
        json={"company_name": "Acme"},
        headers={**headers, "Idempotency-Key": key()},
    )
    assert first.status_code == 201, first.text

    again = client.post(
        f"/jobs/{jd_id}/preparation-plan",
        json={"company_name": "Globex", "job_title": "Analyst"},
        headers={**headers, "Idempotency-Key": key()},
    )

    assert again.status_code == 201, again.text
    assert provider.plan_calls == 1, "the plan already existed; nothing should have been generated"
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["company_name"] == "Acme", "the first generation stays authoritative"
    assert len(plans(engine, jd_id)) == 1


def test_a_generation_that_fails_outright_leaves_the_plan_generatable(client, engine, provider):
    """A failed attempt must not leave the job description permanently claimed.

    The provider failing inside generation is already handled -- the plan falls back to its
    deterministic half -- so the failure injected here is the harder one: the whole operation
    raising, which must release the business claim rather than settle it.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)
    provider.gated = False

    import app.services.jd_service as jd_service

    original = jd_service.generate_preparation_plan
    calls = {"n": 0}

    def exploding(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("the database went away mid-generation")

    jd_service.generate_preparation_plan = exploding
    try:
        broken = client.post(
            f"/jobs/{jd_id}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": key()}
        )
    finally:
        jd_service.generate_preparation_plan = original

    assert broken.status_code == 500, describe([broken])
    assert calls["n"] == 1

    retry = client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": key()}
    )

    assert retry.status_code == 201, f"a failed generation must stay retryable: {retry.text}"
    assert len(plans(engine, jd_id)) == 1
    with engine.connect() as connection:
        released = connection.execute(
            text(
                "SELECT status FROM operation_claims"
                " WHERE idempotency_key LIKE 'biz@%' ORDER BY id"
            )
        ).scalars().all()
    assert released == ["succeeded"], f"the business claim was reused, not duplicated: {released}"
def test_the_invariant_outlives_the_claim_that_enforced_it(client, engine, provider):
    """Two mechanisms, because neither is sufficient alone.

    Settled claims are swept after RETENTION, so the row that proves "this job description has
    been generated" is deleted after seven days while the plan itself is kept forever. If the
    guard depended on the claim, a POST after the sweep would claim afresh and generate a second
    plan. It does not: it reads the plan.

    And the constraint is what makes that structural rather than conditional. The claim stops the
    second expensive generation from starting; `uq_job_preparation_plan_per_jd` stops a second row
    existing at all, including down any path that never consults the guard.
    """
    headers = signup(client)
    jd_id = parse_jd(client, headers)
    provider.gated = False
    first = client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": key()}
    )
    assert first.status_code == 201, first.text

    # Exactly what the retention sweep does to a settled claim.
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM operation_claims WHERE idempotency_key LIKE 'biz@%'"))

    after_sweep = client.post(
        f"/jobs/{jd_id}/preparation-plan", json={}, headers={**headers, "Idempotency-Key": key()}
    )

    assert after_sweep.status_code == 201, after_sweep.text
    assert provider.plan_calls == 1, "the swept claim must not have licensed a second generation"
    assert after_sweep.json()["id"] == first.json()["id"]
    assert len(plans(engine, jd_id)) == 1

    # And the table refuses one directly, with no guard involved at all.
    row = plans(engine, jd_id)[0]
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO job_preparation_plans"
                    " (jd_id, profile_id, job_summary, required_skills, preparation_gaps,"
                    "  preparation_plan, interview_prep, created_at)"
                    " VALUES (:jd, :pid, '{}', '[]', '[]', '[]', '[]', :now)"
                ),
                {"jd": row.jd_id, "pid": row.profile_id, "now": "2026-10-01 12:00:00"},
            )
