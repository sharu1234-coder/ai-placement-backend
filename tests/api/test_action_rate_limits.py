"""D4: the action endpoints are rate limited, and nothing else about them changed.

VA-12 measured the defect rather than inferring it from a missing decorator: 150 consecutive
authenticated `POST /actions/preview` calls returned `{200: 150}` with zero 429s, because
`Limiter(key_func=user_or_ip_key)` carries no `default_limits` and `app/api/actions.py` declared
none of its own. Execute was already bounded by a single-use confirmation and so could not
multiply mutations; preview was the amplifier -- authenticated, unlimited, four SQL statements
per call.

The fix is a decorator and a `Request` parameter, which is why the second half of this file
matters more than the first: a limit that quietly broke confirmation binding, ownership,
entitlement or idempotency would be a worse defect than the one it closed. Those tests assert
the VA-7 guarantees still hold *through* the limiter.

The real limiter, the real confirmation store, the real `operation_claims` table. The only
double is the clock the limiter keeps, which is reset per test by the harness.
"""

import datetime
import logging
import uuid

import pytest

from app.api.actions import _RATE
from app.core.rate_limit import limiter
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.models.user import User
from app.tools.confirmation import MAX_PENDING_PER_USER
from app.tools.confirmation import store as process_store
from tests.fixtures import replay_harness as h

#: Parsed from the route's own policy rather than restated, so raising the limit cannot leave a
#: test asserting the old number and silently passing for the wrong reason.
LIMIT = int(_RATE.split("/")[0])


@pytest.fixture()
def engine(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.database.base import Base
    from app.models.role import Role

    eng = h.make_engine(tmp_path / "d4_rate.db")
    Base.metadata.create_all(bind=eng)
    seed = sessionmaker(bind=eng)()
    seed.add(Role(name="SDE", category="Engineering", description="SDE"))
    seed.commit()
    seed.close()
    yield eng
    eng.dispose()


@pytest.fixture()
def client(engine, tmp_path, monkeypatch):
    made, sessions = h.make_client(engine, tmp_path, monkeypatch)
    made.sessions = sessions
    return made


@pytest.fixture()
def db(client):
    session = client.sessions()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def clean_limiter_and_store():
    """The limiter and the confirmation store are both process-wide.

    `h.make_client` already resets the limiter, but this file deliberately drives one account
    past its quota, so leaving either behind would spend the next test's budget before it sent
    a request.
    """
    limiter.reset()
    process_store.reset()
    yield
    limiter.reset()
    process_store.reset()


def listing_for(db) -> JobListing:
    row = JobListing(
        provider="test",
        provider_job_id=f"job-{uuid.uuid4().hex}",
        url="https://example.invalid/job",
        title="Backend Engineer",
        company="Acme",
        fetched_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def profile_for(db, email: str) -> StudentProfile:
    user = db.query(User).filter(User.email == email).one()
    return db.query(StudentProfile).filter(StudentProfile.user_id == user.id).one()


def preview(client, headers, listing_id: int):
    return client.post(
        "/actions/preview",
        json={"capability": "jobs.save", "arguments": {"listing_id": listing_id}},
        headers=headers,
    )


def execute(client, headers, listing_id: int, token: str, key: str):
    return client.post(
        "/actions/execute",
        json={
            "capability": "jobs.save",
            "arguments": {"listing_id": listing_id},
            "confirmation_token": token,
        },
        headers={**headers, "Idempotency-Key": key},
    )


# --------------------------------------------------------------------- the limit itself


class TestTheLimit:
    def test_requests_below_the_limit_succeed(self, client, db):
        headers = h.signup(client, email="d4-under@example.com")
        listing = listing_for(db)

        codes = {preview(client, headers, listing.id).status_code for _ in range(LIMIT - 1)}

        assert codes == {200}

    def test_requests_above_the_limit_are_refused_with_429(self, client, db):
        headers = h.signup(client, email="d4-over@example.com")
        listing = listing_for(db)
        for _ in range(LIMIT):
            assert preview(client, headers, listing.id).status_code == 200

        refused = preview(client, headers, listing.id)

        assert refused.status_code == 429

    def test_execute_is_limited_too_not_only_preview(self, client, db):
        """Execute is already bounded by a single-use confirmation, so this is about load
        rather than about duplicate mutations -- but an unlimited endpoint that does database
        work is the thing D4 was about, and execute does more of it than preview."""
        headers = h.signup(client, email="d4-exec@example.com")
        listing = listing_for(db)
        # Burn the shared quota with execute calls carrying a token that will be refused; the
        # refusal is a 409, which proves the request reached the handler rather than the limiter.
        for _ in range(LIMIT):
            assert execute(client, headers, listing.id, "bogus.token", "k-d4-x-0001").status_code == 409

        after = execute(client, headers, listing.id, "bogus.token", "k-d4-x-0002")

        assert after.status_code == 429

    def test_each_endpoint_has_its_own_per_account_budget(self, client, db):
        """slowapi keys a limit by route *and* identity, so the two endpoints do not share one
        bucket. Asserted because it is load-bearing for the student, not incidental: a client
        that browsed sixty job previews must still be able to confirm the save it already
        approved, rather than being told to wait an hour at the last step."""
        headers = h.signup(client, email="d4-separate@example.com")
        listing = listing_for(db)
        for _ in range(LIMIT - 1):
            preview(client, headers, listing.id)
        # The last *allowed* preview is the one whose token is kept. Minting it first instead
        # would have it evicted by the per-account grant quota long before the 60th call --
        # which is VA-10's bound doing its job, and cost this test a rewrite to notice.
        minted = preview(client, headers, listing.id)
        assert minted.status_code == 200, "the 60th preview should still be allowed"
        assert preview(client, headers, listing.id).status_code == 429, "the 61st should not"

        # Preview is exhausted; execute is not, so the approval can still be spent.
        done = execute(client, headers, listing.id, minted.json()["confirmation_token"], "k-d4-sep-001")

        assert done.status_code == 200, done.text
        assert done.json()["state"] == "succeeded"


class TestTheLimitIsScopedToTheAccount:
    def test_one_account_exhausting_its_quota_does_not_affect_another(self, client, db):
        """The property that makes a per-user limiter safe on a campus network.

        `user_or_ip_key` exists precisely because IP-keying put a whole hostel in one bucket.
        """
        loud = h.signup(client, email="d4-loud@example.com")
        quiet = h.signup(client, email="d4-quiet@example.com")
        listing = listing_for(db)
        for _ in range(LIMIT + 5):
            preview(client, loud, listing.id)

        assert preview(client, loud, listing.id).status_code == 429
        assert preview(client, quiet, listing.id).status_code == 200

    def test_changing_identity_headers_does_not_mint_a_fresh_quota(self, client, db):
        """The key is the authenticated user id, resolved server-side.

        A client that rewrites its own headers to look like somebody else either fails
        authentication or is still itself; neither produces a new budget.
        """
        headers = h.signup(client, email="d4-spoof@example.com")
        listing = listing_for(db)
        for _ in range(LIMIT):
            preview(client, headers, listing.id)

        spoofed = {
            **headers,
            "X-Forwarded-For": "203.0.113.7",
            "X-Real-IP": "203.0.113.8",
            "User-Agent": "something-else",
        }

        assert preview(client, spoofed, listing.id).status_code == 429

    def test_an_unauthenticated_request_is_refused_not_given_a_free_pass(self, client, db):
        listing = listing_for(db)

        anonymous = preview(client, {}, listing.id)
        forged = preview(client, {"Authorization": "Bearer not.a.jwt"}, listing.id)

        assert anonymous.status_code in (401, 403)
        assert forged.status_code == 401


# ------------------------------------------------- the limiter did not weaken anything


class TestTheVa7GuaranteesStillHold:
    def test_the_limit_does_not_bypass_confirmation_ownership_or_entitlement(self, client, db):
        """One pass through the real flow, asserting the guards that matter are still in force:
        execution without a confirmation is refused, another account's listing is undisclosed,
        and a confirmed save writes exactly one authoritative row."""
        headers = h.signup(client, email="d4-guards@example.com")
        other = h.signup(client, email="d4-guards-other@example.com")
        listing = listing_for(db)
        profile = profile_for(db, "d4-guards@example.com")

        # No confirmation -> refused, nothing written.
        assert execute(client, headers, listing.id, "nope.nope", "k-d4-g-0001").status_code == 409
        db.expire_all()
        assert (
            db.query(SavedJob)
            .filter(SavedJob.profile_id == profile.id, SavedJob.listing_id == listing.id)
            .first()
            is None
        )

        # A real preview and a real execution still work through the limiter.
        minted = preview(client, headers, listing.id)
        assert minted.status_code == 200
        done = execute(client, headers, listing.id, minted.json()["confirmation_token"], "k-d4-g-0002")
        assert done.status_code == 200, done.text
        body = done.json()
        assert body["state"] == "succeeded" and body["verified"] is True

        db.expire_all()
        rows = (
            db.query(SavedJob)
            .filter(SavedJob.profile_id == profile.id, SavedJob.listing_id == listing.id)
            .all()
        )
        assert len(rows) == 1

        # And the other account cannot reach that confirmation or that row.
        stolen = execute(client, other, listing.id, minted.json()["confirmation_token"], "k-d4-g-0003")
        assert stolen.status_code == 409

    def test_repeated_previews_do_not_create_unbounded_confirmation_state(self, client, db):
        """Two bounds now, and they are independent.

        The per-account grant quota (VA-10) is what keeps memory flat; the rate limit is what
        keeps the database quiet. Asserting both here so neither is mistaken for the other.
        """
        headers = h.signup(client, email="d4-bounded@example.com")
        listing = listing_for(db)

        for _ in range(LIMIT):
            preview(client, headers, listing.id)

        assert len(process_store) <= MAX_PENDING_PER_USER

    def test_a_replayed_execution_still_does_not_duplicate_the_action(self, client, db):
        headers = h.signup(client, email="d4-replay@example.com")
        listing = listing_for(db)
        profile = profile_for(db, "d4-replay@example.com")
        minted = preview(client, headers, listing.id)
        key = "k-d4-replay-001"

        first = execute(client, headers, listing.id, minted.json()["confirmation_token"], key)
        assert first.status_code == 200, first.text
        # The same key again, with a freshly minted confirmation: the idempotency claim, not the
        # confirmation, is what has to refuse the second durable effect.
        second_token = preview(client, headers, listing.id).json()["confirmation_token"]
        second = execute(client, headers, listing.id, second_token, key)

        assert second.status_code in (200, 409)
        db.expire_all()
        rows = (
            db.query(SavedJob)
            .filter(SavedJob.profile_id == profile.id, SavedJob.listing_id == listing.id)
            .all()
        )
        assert len(rows) == 1


class TestTheRefusalDisclosesNothing:
    def test_the_429_body_and_logs_carry_no_secret(self, client, db, caplog):
        headers = h.signup(client, email="d4-redact@example.com")
        token = headers["Authorization"].split(" ", 1)[1]
        listing = listing_for(db)
        minted = preview(client, headers, listing.id)
        confirmation = minted.json()["confirmation_token"]
        # Exhaust *execute's* own bucket, so the 429 below is the refusal under test and the
        # real confirmation is what the refused request was carrying.
        for _ in range(LIMIT):
            execute(client, headers, listing.id, "bogus.token", "k-d4-redact-00")

        with caplog.at_level(logging.DEBUG):
            refused = execute(client, headers, listing.id, confirmation, "k-d4-redact-01")

        assert refused.status_code == 429
        emitted = "\n".join(record.getMessage() for record in caplog.records)
        for secret in (token, confirmation, confirmation.split(".", 1)[1]):
            assert secret not in refused.text
            assert secret not in emitted
        # And the refusal says nothing about the student's data.
        assert "Backend Engineer" not in refused.text
        assert "Acme" not in refused.text
