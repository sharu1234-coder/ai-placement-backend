"""VA-7: the confirmation store on its own -- no database, no credentials, no app.

The registry-level tests in `tests/api/test_safe_actions.py` prove that a mutation cannot run
without a valid grant. These prove the properties of the grant itself, which are the ones a
reader has to take on trust otherwise: that the token is not stored, that a near-miss is
compared in constant time, that every binding is checked, and that "single use" means the
record is gone rather than flagged.
"""

import datetime
import hashlib
import threading

import pytest

from app.tools.confirmation import DEFAULT_TTL_SECONDS, ConfirmationStore
from app.tools.contracts import RiskClass, ToolConfirmationInvalid

NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)

FIELDS = dict(
    user_id=7,
    capability="jobs.save",
    argument_fingerprint="a" * 64,
    resource_key="job_listing:41",
    resource_label="Backend Engineer at Acme",
    description="Save Backend Engineer at Acme to your tracker.",
    risk_class=RiskClass.LOW,
)


@pytest.fixture()
def store() -> ConfirmationStore:
    return ConfirmationStore()


def issue(store, **overrides):
    return store.issue(**{**FIELDS, **overrides})


def consume(store, token, **overrides):
    keys = {k: FIELDS[k] for k in ("user_id", "capability", "argument_fingerprint")}
    keys.update(overrides)
    return store.consume(token, **keys)


class TestTheTokenIsNotStored:
    def test_only_a_digest_of_the_secret_is_kept(self, store):
        token, grant = issue(store)
        secret = token.split(".", 1)[1]
        assert grant.secret_digest == hashlib.sha256(secret.encode()).hexdigest()
        assert secret not in grant.secret_digest
        # A dump of this store must not yield a usable approval.
        assert secret not in repr(grant)

    def test_the_payload_a_client_may_see_carries_no_secret(self, store):
        token, grant = issue(store)
        payload = grant.to_payload()
        assert token not in str(payload)
        assert "secret" not in payload and "token" not in payload
        assert payload["confirmation_id"] == grant.confirmation_id

    def test_two_grants_never_share_a_secret(self, store):
        first, _ = issue(store)
        second, _ = issue(store)
        assert first != second
        assert first.split(".", 1)[1] != second.split(".", 1)[1]


class TestEveryBindingIsChecked:
    def test_a_matching_token_is_accepted_once(self, store):
        token, grant = issue(store)
        assert consume(store, token).confirmation_id == grant.confirmation_id
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, token)
        # Gone, not flagged: there is no record left to mis-read as still usable.
        assert store.peek(grant.confirmation_id) is None
        assert len(store) == 0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"user_id": 8},
            {"capability": "jobs.unsave"},
            {"argument_fingerprint": "b" * 64},
        ],
    )
    def test_a_mismatched_binding_is_refused(self, store, overrides):
        token, _grant = issue(store)
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, token, **overrides)

    def test_a_rejected_attempt_does_not_burn_the_grant(self, store):
        """Otherwise a wrong-capability request would invalidate the student's pending approval,
        turning a refused call into a denial of their own action."""
        token, _grant = issue(store)
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, token, capability="jobs.unsave")
        assert consume(store, token) is not None

    @pytest.mark.parametrize("token", [None, "", "   ", "noseparator", ".", "unknownid.secret"])
    def test_a_malformed_or_unknown_token_is_refused(self, store, token):
        issue(store)
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, token)

    def test_the_right_id_with_the_wrong_secret_is_refused(self, store):
        _token, grant = issue(store)
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, f"{grant.confirmation_id}.not-the-secret")

    def test_every_refusal_gives_the_same_message(self, store):
        """Distinguishing the causes would turn this into an oracle for probing whose grant an
        id belongs to. The client's recovery is identical in every case: preview again."""
        messages = set()
        for attempt in ("garbage", "unknownid.secret", ""):
            try:
                consume(store, attempt)
            except ToolConfirmationInvalid as exc:
                messages.add(str(exc))
        token, _grant = issue(store)
        try:
            consume(store, token, user_id=99)
        except ToolConfirmationInvalid as exc:
            messages.add(str(exc))
        assert len(messages) == 1


class TestExpiry:
    def test_an_expired_grant_is_refused_and_dropped(self, store):
        token, grant = issue(store, now=NOW)
        later = NOW + datetime.timedelta(seconds=DEFAULT_TTL_SECONDS + 1)
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, token, now=later)
        assert store.peek(grant.confirmation_id) is None

    def test_a_grant_inside_its_window_is_accepted(self, store):
        token, _grant = issue(store, now=NOW)
        assert consume(store, token, now=NOW + datetime.timedelta(seconds=1)) is not None

    def test_the_expiry_is_fixed_at_issue_time(self, store):
        """The VA-2 lesson, restated: a TTL changed afterwards does not reach back into a grant
        that was already minted, so a test that patches it must mint again to see the effect."""
        _token, grant = issue(store, now=NOW)
        assert grant.expires_at == NOW + datetime.timedelta(seconds=DEFAULT_TTL_SECONDS)
        store.ttl_seconds = 1.0
        assert grant.expires_at == NOW + datetime.timedelta(seconds=DEFAULT_TTL_SECONDS)

    def test_a_non_positive_ttl_is_refused(self):
        for ttl in (0, -1):
            with pytest.raises(ValueError):
                ConfirmationStore(ttl_seconds=ttl)

    def test_expiry_is_reported_as_remaining_seconds(self, store):
        _token, grant = issue(store, now=NOW)
        assert grant.expires_in_seconds(NOW) == pytest.approx(DEFAULT_TTL_SECONDS)
        assert grant.expires_in_seconds(NOW + datetime.timedelta(hours=1)) == 0.0


class TestBounds:
    def test_the_store_is_capped_and_evicts_the_oldest(self):
        store = ConfirmationStore(max_pending=4)
        tokens = [issue(store, resource_key=f"job_listing:{index}")[0] for index in range(6)]
        assert len(store) == 4
        # The oldest are gone, so a client that previews in a loop cannot grow the worker.
        with pytest.raises(ToolConfirmationInvalid):
            consume(store, tokens[0])
        assert consume(store, tokens[-1]) is not None

    def test_reset_clears_everything(self, store):
        issue(store)
        store.reset()
        assert len(store) == 0


class TestConcurrency:
    def test_only_one_of_two_racing_consumers_wins(self, store):
        """Single use has to mean single use across threads: the registry is reachable from a
        request thread and from the voice gateway's pool, and two threads both finding the same
        live grant is exactly the duplicate mutation the whole mechanism exists to prevent."""
        token, _grant = issue(store)
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait(timeout=5)
            try:
                consume(store, token)
                results.append(True)
            except ToolConfirmationInvalid:
                results.append(False)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert results.count(True) == 1, results
        assert results.count(False) == 1, results
