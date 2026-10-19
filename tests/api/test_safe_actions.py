"""VA-7: the write-action boundary, exercised against the real guards.

Nothing here stands in for the thing under test. Tokens come from the real signup and login
routes, the confirmation store is the real one, the idempotency claim is the real
`operation_claims` table with its real unique constraint, and the saved-job rows are read back
with the same service the tracker screen uses. A fake that reproduced any of those would prove
only that the fake works -- and a fake confirmation store is exactly the shape of mistake that
hides a missing check.

The caller is assumed hostile throughout. It sends `confirmed: true`, it reuses tokens, it
changes an argument after the preview, it presents another student's grant, it asks for
capabilities that do not exist, and it retries things whose outcome it cannot know.
"""

import datetime
import logging

import pytest

from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.models.user import User
from app.tools.action_tools import JobActionArguments, SavedJobActionResult, action_specs
from app.tools.confirmation import ConfirmationStore
from app.tools.contracts import (
    ActionPreview,
    ActionState,
    CancellationToken,
    Idempotency,
    RiskClass,
    ToolCancelled,
    ToolConfirmationInvalid,
    ToolConfirmationRequired,
    ToolContext,
    ToolIdempotencyKeyRequired,
    ToolInputInvalid,
    ToolKind,
    ToolNotFound,
    ToolPermissionDenied,
    ToolResourceNotFound,
    ToolSpec,
    Verification,
)
from app.tools.registry import ToolRegistry, build_default_registry
from tests.fixtures import replay_harness as h


@pytest.fixture()
def engine(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.database.base import Base
    from app.models.role import Role

    eng = h.make_engine(tmp_path / "va7_actions.db")
    Base.metadata.create_all(bind=eng)
    seed = sessionmaker(bind=eng)()
    seed.add(Role(name="SDE", category="Engineering", description="Software Development Engineer"))
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


@pytest.fixture()
def registry() -> ToolRegistry:
    """The catalogue the action route builds: read tools plus VA-7's two writes, no world."""
    return build_default_registry(include_world=False, include_actions=True)


@pytest.fixture()
def confirmations() -> ConfirmationStore:
    """A store per test. The production module-level store is shared process state, and a test
    that leaked a grant into the next one would make a single-use assertion meaningless."""
    return ConfirmationStore()


def token_for(client, email: str) -> str:
    return h.signup(client, email=email)["Authorization"].split(" ", 1)[1]


def context_for(client, db, email: str) -> ToolContext:
    return ToolContext.authenticate(token_for(client, email), db)


def headers_for(client, email: str) -> dict:
    return h.signup(client, email=email)


def profile_for(db, email: str) -> StudentProfile:
    user = db.query(User).filter(User.email == email).one()
    return db.query(StudentProfile).filter(StudentProfile.user_id == user.id).one()


def make_listing(db, *, title="Backend Engineer", company="Acme", owner_profile_id=None) -> JobListing:
    """A provider advert by default; a manually entered, privately owned listing when
    `owner_profile_id` is set. The two have opposite privacy, which is the whole point of
    `assert_listing_visible`."""
    listing = JobListing(
        provider="test",
        provider_job_id=f"job-{title}-{company}-{datetime.datetime.now().timestamp()}",
        url="https://example.invalid/job",
        title=title,
        company=company,
        created_by_profile_id=owner_profile_id,
        fetched_at=datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)
    return listing


def tracked(db, profile_id: int, listing_id: int):
    """Read the authoritative row directly, bypassing anything under test."""
    db.expire_all()
    return (
        db.query(SavedJob)
        .filter(SavedJob.profile_id == profile_id, SavedJob.listing_id == listing_id)
        .first()
    )


def run(registry, confirmations, context, capability, arguments, *, key="k-va7-0000001"):
    """preview -> execute, the whole happy path, returning the outcome."""
    token, _grant, _preview = registry.preview_action(capability, arguments, context, confirmations=confirmations)
    return registry.invoke_action(
        capability,
        arguments,
        context,
        confirmation_token=token,
        idempotency_key=key,
        confirmations=confirmations,
    )


# --------------------------------------------------------------------------- the happy path


class TestTheConfirmedPath:
    def test_a_confirmed_save_succeeds_and_is_verified(self, client, db, registry, confirmations):
        context = context_for(client, db, "save1@example.com")
        profile = profile_for(db, "save1@example.com")
        listing = make_listing(db)

        outcome = run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id})

        assert outcome.state is ActionState.SUCCEEDED
        assert outcome.verified is True
        row = tracked(db, profile.id, listing.id)
        assert row is not None and row.status == "saved"

    def test_a_confirmed_unsave_succeeds_and_is_verified(self, client, db, registry, confirmations):
        context = context_for(client, db, "unsave1@example.com")
        profile = profile_for(db, "unsave1@example.com")
        listing = make_listing(db)
        run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-save-00000001")

        outcome = run(
            registry, confirmations, context, "jobs.unsave", {"listing_id": listing.id}, key="k-unsave-0000001"
        )

        assert outcome.state is ActionState.SUCCEEDED and outcome.verified is True
        assert tracked(db, profile.id, listing.id) is None

    def test_the_preview_describes_the_specific_job(self, client, db, registry, confirmations):
        context = context_for(client, db, "prev1@example.com")
        listing = make_listing(db, title="Data Analyst", company="Globex")

        _token, grant, preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
        )

        # The sentence the student is shown and the sentence the server will act on are the
        # same object, so they cannot drift.
        assert "Data Analyst at Globex" in preview.description
        assert grant.description == preview.description
        assert grant.resource_key == f"job_listing:{listing.id}"
        assert grant.risk_class is RiskClass.LOW

    def test_the_preview_writes_nothing(self, client, db, registry, confirmations):
        context = context_for(client, db, "prev2@example.com")
        profile = profile_for(db, "prev2@example.com")
        listing = make_listing(db)

        registry.preview_action("jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations)

        assert tracked(db, profile.id, listing.id) is None


# --------------------------------------------------------------------------- the read boundary


class TestReadAndWritePathsStaySeparate:
    def test_the_read_entry_point_refuses_an_action(self, client, db, registry):
        context = context_for(client, db, "ro1@example.com")
        with pytest.raises(ToolPermissionDenied) as caught:
            registry.invoke_read("jobs.save", {"listing_id": 1}, context)
        assert "write tools are not enabled" in str(caught.value)

    def test_the_general_entry_point_also_refuses_an_action(self, client, db, registry):
        # VA-7 narrowed `invoke` to READ. Before that, a caller with a reference to the
        # registry could run a mutation with no confirmation, no claim and no verification --
        # and "the caller remembered to use the other method" is not a boundary.
        context = context_for(client, db, "ro2@example.com")
        with pytest.raises(ToolPermissionDenied) as caught:
            registry.invoke("jobs.save", {"listing_id": 1}, context)
        assert "confirmed action path" in str(caught.value)

    def test_the_action_entry_point_refuses_a_read_tool(self, client, db, registry, confirmations):
        context = context_for(client, db, "ro3@example.com")
        with pytest.raises(ToolPermissionDenied):
            registry.preview_action("profile.get_summary", None, context, confirmations=confirmations)

    def test_the_read_interface_still_works_unchanged(self, client, db, registry):
        context = context_for(client, db, "ro4@example.com")
        result = registry.invoke_read("profile.get_summary", None, context)
        assert result.kind is ToolKind.READ and result.output is not None

    def test_the_default_catalogue_has_no_write_tool(self):
        # The VA-6 orchestrator builds this one. A write tool arriving here by default would
        # put a mutation in a model's tool list as a side effect of a registry change.
        default = build_default_registry()
        assert {default.get(name).kind for name in default.names} == {ToolKind.READ}
        assert "jobs.save" not in default.names and "jobs.unsave" not in default.names

    def test_the_action_catalogue_is_exactly_two_low_risk_tools(self, registry):
        writes = {name for name in registry.names if registry.get(name).kind is not ToolKind.READ}
        assert writes == {"jobs.save", "jobs.unsave"}
        assert all(registry.get(name).risk_class is RiskClass.LOW for name in writes)
        # Nothing destructive, financial or account-level slipped in alongside them.
        assert all(registry.get(name).side_effects == ("SavedJob",) for name in writes)


# --------------------------------------------------------------------------- confirmation


class TestConfirmationIsRequiredAndBound:
    def test_an_unregistered_action_is_rejected(self, client, db, registry, confirmations):
        context = context_for(client, db, "conf0@example.com")
        with pytest.raises(ToolNotFound) as caught:
            registry.invoke_action(
                "jobs.delete_account",
                {},
                context,
                confirmation_token="anything",
                idempotency_key="k-unknown-000001",
                confirmations=confirmations,
            )
        # The refusal names what was asked for and never what is registered.
        assert "jobs.save" not in str(caught.value)

    def test_an_unconfirmed_action_never_executes(self, client, db, registry, confirmations):
        context = context_for(client, db, "conf1@example.com")
        profile = profile_for(db, "conf1@example.com")
        listing = make_listing(db)

        with pytest.raises(ToolConfirmationRequired):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                context,
                confirmation_token=None,
                idempotency_key="k-noconfirm-01",
                confirmations=confirmations,
            )

        assert tracked(db, profile.id, listing.id) is None

    @pytest.mark.parametrize(
        "token",
        ["", "   ", "garbage", "no-separator", "abc.def", "yes I confirm", "true", "confirmed"],
    )
    def test_a_made_up_confirmation_is_refused(self, client, db, registry, confirmations, token):
        # A model emitting a plausible-sounding approval is the first thing to try, and none of
        # these strings is a token the server minted.
        context = context_for(client, db, "conf2@example.com")
        profile = profile_for(db, "conf2@example.com")
        listing = make_listing(db)

        with pytest.raises((ToolConfirmationRequired, ToolConfirmationInvalid)):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key="k-madeup-00001",
                confirmations=confirmations,
            )
        assert tracked(db, profile.id, listing.id) is None

    def test_a_confirmed_true_field_in_the_arguments_is_refused(self, client, db, registry, confirmations):
        # `extra="forbid"` on ToolInput. An ignored field is indistinguishable from an honoured
        # one from the caller's side, which is why it is a 422 rather than a shrug.
        context = context_for(client, db, "conf3@example.com")
        listing = make_listing(db)
        with pytest.raises(ToolInputInvalid):
            registry.preview_action(
                "jobs.save",
                {"listing_id": listing.id, "confirmed": True},
                context,
                confirmations=confirmations,
            )

    def test_a_consumed_confirmation_cannot_be_used_again(self, client, db, registry, confirmations):
        context = context_for(client, db, "conf4@example.com")
        profile = profile_for(db, "conf4@example.com")
        listing = make_listing(db)
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
        )
        first = registry.invoke_action(
            "jobs.save",
            {"listing_id": listing.id},
            context,
            confirmation_token=token,
            idempotency_key="k-reuse-000001",
            confirmations=confirmations,
        )
        assert first.state is ActionState.SUCCEEDED

        # Same token, a different idempotency key -- so nothing but the single-use rule is
        # standing between this request and a second mutation.
        with pytest.raises(ToolConfirmationInvalid):
            registry.invoke_action(
                "jobs.unsave",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key="k-reuse-000002",
                confirmations=confirmations,
            )
        assert tracked(db, profile.id, listing.id) is not None

    def test_an_expired_confirmation_is_refused(self, client, db, registry):
        context = context_for(client, db, "conf5@example.com")
        profile = profile_for(db, "conf5@example.com")
        listing = make_listing(db)
        store = ConfirmationStore(ttl_seconds=0.01)
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=store
        )
        import time

        time.sleep(0.05)

        with pytest.raises(ToolConfirmationInvalid):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key="k-expired-0001",
                confirmations=store,
            )
        assert tracked(db, profile.id, listing.id) is None

    def test_a_confirmation_for_one_action_cannot_run_another(self, client, db, registry, confirmations):
        context = context_for(client, db, "conf6@example.com")
        profile = profile_for(db, "conf6@example.com")
        listing = make_listing(db)
        run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-cross-000001")
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
        )

        with pytest.raises(ToolConfirmationInvalid):
            registry.invoke_action(
                "jobs.unsave",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key="k-cross-000002",
                confirmations=confirmations,
            )
        assert tracked(db, profile.id, listing.id) is not None

    def test_changing_an_argument_after_the_preview_invalidates_it(self, client, db, registry, confirmations):
        context = context_for(client, db, "conf7@example.com")
        profile = profile_for(db, "conf7@example.com")
        previewed = make_listing(db, title="Previewed Job")
        other = make_listing(db, title="Substituted Job")
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": previewed.id}, context, confirmations=confirmations
        )

        with pytest.raises(ToolConfirmationInvalid):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": other.id},
                context,
                confirmation_token=token,
                idempotency_key="k-swap-0000001",
                confirmations=confirmations,
            )
        # Neither one: not the job that was approved and not the one that was substituted.
        assert tracked(db, profile.id, previewed.id) is None
        assert tracked(db, profile.id, other.id) is None

    def test_a_confirmation_cannot_be_transferred_to_another_account(self, client, db, registry, confirmations):
        owner = context_for(client, db, "owner7@example.com")
        attacker = context_for(client, db, "attacker7@example.com")
        listing = make_listing(db)
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, owner, confirmations=confirmations
        )

        with pytest.raises(ToolConfirmationInvalid):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                attacker,
                confirmation_token=token,
                idempotency_key="k-transfer-0001",
                confirmations=confirmations,
            )
        assert tracked(db, profile_for(db, "attacker7@example.com").id, listing.id) is None
        # And the owner's grant was not burned by someone else's failed attempt, which would
        # have turned a rejected request into a denial of the owner's own action.
        assert tracked(db, profile_for(db, "owner7@example.com").id, listing.id) is None
        assert registry.invoke_action(
            "jobs.save",
            {"listing_id": listing.id},
            owner,
            confirmation_token=token,
            idempotency_key="k-transfer-0002",
            confirmations=confirmations,
        ).state is ActionState.SUCCEEDED

    def test_the_grant_record_never_contains_the_token(self, client, db, registry, confirmations):
        context = context_for(client, db, "conf8@example.com")
        listing = make_listing(db)
        token, grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
        )
        secret = token.split(".", 1)[1]
        rendered = repr(grant) + repr(grant.to_payload())
        assert secret not in rendered
        assert token not in rendered


# --------------------------------------------------------------------------- ownership


class TestOwnership:
    def test_another_students_manual_listing_is_not_found(self, client, db, registry, confirmations):
        """Not 'forbidden'. Confirming that a row exists is the disclosure."""
        token_for(client, "mowner@example.com")
        owner_id = profile_for(db, "mowner@example.com").id
        private = make_listing(db, title="Referral from a senior", owner_profile_id=owner_id)
        stranger = context_for(client, db, "stranger@example.com")

        with pytest.raises(ToolResourceNotFound) as caught:
            registry.preview_action("jobs.save", {"listing_id": private.id}, stranger, confirmations=confirmations)
        message = str(caught.value)
        assert str(private.id) not in message
        assert "Referral" not in message and "not yours" not in message

    def test_a_missing_listing_is_refused_the_same_way(self, client, db, registry, confirmations):
        # Indistinguishable from the case above, which is what stops the pair being an oracle.
        context = context_for(client, db, "missing@example.com")
        with pytest.raises(ToolResourceNotFound) as caught:
            registry.preview_action("jobs.save", {"listing_id": 987654}, context, confirmations=confirmations)
        assert "987654" not in str(caught.value)

    def test_one_students_action_does_not_touch_anothers_tracker(self, client, db, registry, confirmations):
        first = context_for(client, db, "iso1@example.com")
        second = context_for(client, db, "iso2@example.com")
        listing = make_listing(db)
        run(registry, confirmations, first, "jobs.save", {"listing_id": listing.id}, key="k-iso-00000001")

        assert tracked(db, profile_for(db, "iso1@example.com").id, listing.id) is not None
        assert tracked(db, profile_for(db, "iso2@example.com").id, listing.id) is None

        # And the second student unsaving the same listing does not remove the first one's row.
        outcome = run(registry, confirmations, second, "jobs.unsave", {"listing_id": listing.id}, key="k-iso-00000002")
        assert outcome.no_change_needed is True
        assert tracked(db, profile_for(db, "iso1@example.com").id, listing.id) is not None

    @pytest.mark.parametrize(
        "arguments",
        [
            {},
            {"listing_id": 0},
            {"listing_id": -1},
            {"listing_id": "abc"},
            {"listing_id": 1, "profile_id": 2},
            {"listing_id": 1, "user_id": 2},
            {"listing_id": 1, "status": "applied"},
            {"profile_id": 1},
        ],
    )
    def test_malformed_or_invented_arguments_are_refused(self, client, db, registry, confirmations, arguments):
        context = context_for(client, db, "args7@example.com")
        with pytest.raises(ToolInputInvalid):
            registry.preview_action("jobs.save", arguments, context, confirmations=confirmations)


# --------------------------------------------------------------------------- idempotency


class TestIdempotency:
    def test_an_action_declaring_key_required_refuses_to_run_without_one(
        self, client, db, registry, confirmations
    ):
        context = context_for(client, db, "idem0@example.com")
        profile = profile_for(db, "idem0@example.com")
        listing = make_listing(db)
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
        )

        with pytest.raises(ToolIdempotencyKeyRequired):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key=None,
                confirmations=confirmations,
            )
        assert tracked(db, profile.id, listing.id) is None
        # The grant survived a client bug, so the student is not asked to approve again.
        assert confirmations.peek(_grant.confirmation_id) is not None

    def test_a_duplicate_submission_results_in_one_mutation(self, client, db, registry, confirmations):
        context = context_for(client, db, "idem1@example.com")
        profile = profile_for(db, "idem1@example.com")
        listing = make_listing(db)
        key = "k-dup-00000001"

        first = run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key=key)
        second = run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key=key)

        assert first.state is ActionState.SUCCEEDED and first.replayed is False
        # The second never reaches the claim, because the preview already found the target state
        # satisfied and that short circuit comes first. Both guards would collapse this
        # duplicate on their own; the earlier one is the one that fires, and it is also the one
        # that avoids a pointless write. `test_a_completed_mutation_is_not_re_run_by_a_retry`
        # below is where the claim itself does the work.
        assert second.state is ActionState.SUCCEEDED and second.no_change_needed is True
        rows = (
            db.query(SavedJob)
            .filter(SavedJob.profile_id == profile.id, SavedJob.listing_id == listing.id)
            .all()
        )
        assert len(rows) == 1

    def test_a_completed_mutation_is_not_re_run_by_a_retry(self, client, db, registry, confirmations):
        """The sharpest version of the replay question.

        The job is unsaved, then deliberately saved again, then the *same* unsave key is
        retried. If the retry re-ran the mutation it would remove the job a second time and
        report success. A correct replay does not run the handler at all -- and the
        authoritative read then says the intended state does not hold, which is reported as a
        verification failure rather than as a success.
        """
        context = context_for(client, db, "idem2@example.com")
        profile = profile_for(db, "idem2@example.com")
        listing = make_listing(db)
        run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-retry-000001")
        key = "k-retry-000002"
        assert run(
            registry, confirmations, context, "jobs.unsave", {"listing_id": listing.id}, key=key
        ).state is ActionState.SUCCEEDED

        run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-retry-000003")
        retried = run(registry, confirmations, context, "jobs.unsave", {"listing_id": listing.id}, key=key)

        assert retried.replayed is True
        assert retried.state is ActionState.VERIFICATION_FAILED
        assert retried.verified is False
        # The row the retry must not have deleted.
        assert tracked(db, profile.id, listing.id) is not None

    def test_a_key_cannot_be_reused_for_a_different_request(self, client, db, registry, confirmations):
        from app.services.idempotency import IdempotencyConflictError

        context = context_for(client, db, "idem3@example.com")
        first = make_listing(db, title="First Job")
        second = make_listing(db, title="Second Job")
        key = "k-shared-00001"
        run(registry, confirmations, context, "jobs.save", {"listing_id": first.id}, key=key)

        # Same key, a genuinely different mutation. Handing back the first one's result would
        # be a silent substitution; the fingerprint is what makes it a conflict instead.
        with pytest.raises(IdempotencyConflictError):
            run(registry, confirmations, context, "jobs.save", {"listing_id": second.id}, key=key)
        assert tracked(db, profile_for(db, "idem3@example.com").id, second.id) is None

    def test_save_and_unsave_are_not_a_toggle(self, client, db, registry, confirmations):
        """A toggle is the wrong shape for anything retryable: a duplicate "flip it" undoes the
        first submission and reports success while leaving the opposite of what was asked."""
        context = context_for(client, db, "idem4@example.com")
        profile = profile_for(db, "idem4@example.com")
        listing = make_listing(db)

        for index in range(3):
            outcome = run(
                registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key=f"k-tog-000000{index}"
            )
            assert outcome.state is ActionState.SUCCEEDED
            assert tracked(db, profile.id, listing.id) is not None

        for index in range(3):
            outcome = run(
                registry, confirmations, context, "jobs.unsave", {"listing_id": listing.id}, key=f"k-tgu-000000{index}"
            )
            assert outcome.state is ActionState.SUCCEEDED
            assert tracked(db, profile.id, listing.id) is None

    def test_a_repeat_save_does_not_reset_a_pipeline_status(self, client, db, registry, confirmations):
        """`save_job` also writes status, and passing one through would mean a repeat save on a
        job the student has moved to "interview" rewrote their own history back to "saved"."""
        context = context_for(client, db, "idem5@example.com")
        profile = profile_for(db, "idem5@example.com")
        listing = make_listing(db)
        run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-stat-000001")
        row = tracked(db, profile.id, listing.id)
        row.status = "interview"
        row.notes = "second round on Thursday"
        db.commit()

        outcome = run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-stat-000002")

        assert outcome.state is ActionState.SUCCEEDED and outcome.no_change_needed is True
        after = tracked(db, profile.id, listing.id)
        assert after.status == "interview" and after.notes == "second round on Thursday"

    def test_an_unsave_preview_names_what_will_be_lost(self, client, db, registry, confirmations):
        context = context_for(client, db, "idem6@example.com")
        profile = profile_for(db, "idem6@example.com")
        listing = make_listing(db)
        run(registry, confirmations, context, "jobs.save", {"listing_id": listing.id}, key="k-warn-000001")
        row = tracked(db, profile.id, listing.id)
        row.status = "interview"
        db.commit()

        _token, _grant, preview = registry.preview_action(
            "jobs.unsave", {"listing_id": listing.id}, context, confirmations=confirmations
        )
        # A student told only "remove this" has not been told that the notes go with it.
        assert "interview" in preview.description and "notes" in preview.description


# --------------------------------------------------------------------------- failure handling


def _action_spec(**overrides) -> ToolSpec:
    defaults = dict(
        name="test.act",
        kind=ToolKind.ACTION,
        summary="a test action",
        input_model=JobActionArguments,
        output_model=SavedJobActionResult,
        handler=lambda ctx, args: SavedJobActionResult(listing_id=args.listing_id, title="t", tracked=True),
        preview=lambda ctx, args: ActionPreview(
            description="would act", resource_key=f"job_listing:{args.listing_id}",
            resource_label="a job", target_state="acted",
        ),
        verify=lambda ctx, args: True,
        side_effects=("SavedJob",),
        idempotency=Idempotency.KEY_REQUIRED,
        verification=Verification.USER_CONFIRMATION,
        risk_class=RiskClass.LOW,
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


class TestOutcomesAreHonest:
    def test_success_is_not_reported_when_verification_says_otherwise(
        self, client, db, confirmations
    ):
        """A handler that returns a success-shaped output is not evidence. The registry refuses
        to call it success without an authoritative read agreeing."""
        registry = ToolRegistry()
        registry.register(_action_spec(verify=lambda ctx, args: False))
        context = context_for(client, db, "fail1@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-verf-000001")

        assert outcome.state is ActionState.VERIFICATION_FAILED
        assert outcome.verified is False
        assert "could not be confirmed" in outcome.detail

    def test_an_unreadable_verification_is_unknown_not_success_or_failure(
        self, client, db, confirmations
    ):
        def exploding_verify(ctx, args):
            raise RuntimeError("the read failed")

        registry = ToolRegistry()
        registry.register(_action_spec(verify=exploding_verify))
        context = context_for(client, db, "fail2@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-unk-00000001")

        assert outcome.state is ActionState.UNKNOWN and outcome.verified is False
        assert "not be retried automatically" in outcome.detail
        # And no internal text reached the caller.
        assert "the read failed" not in (outcome.detail or "")

    def test_a_failure_before_mutation_reconciles_to_failed(self, client, db, confirmations):
        def exploding_handler(ctx, args):
            raise RuntimeError("the write failed")

        registry = ToolRegistry()
        registry.register(_action_spec(handler=exploding_handler, verify=lambda ctx, args: False))
        context = context_for(client, db, "fail3@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-prefail-0001")

        assert outcome.state is ActionState.FAILED and outcome.verified is False
        assert "the write failed" not in (outcome.detail or "")

    def test_a_failure_after_the_mutation_committed_reconciles_to_succeeded(
        self, client, db, confirmations
    ):
        """The case a boolean cannot express. The handler raised, so the request looks failed;
        the authoritative read says the change is there. Reporting failure would invite a retry
        of something that already happened."""

        def exploding_handler(ctx, args):
            raise RuntimeError("lost the connection after committing")

        registry = ToolRegistry()
        registry.register(_action_spec(handler=exploding_handler, verify=lambda ctx, args: True))
        context = context_for(client, db, "fail4@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-postfail-001")

        assert outcome.state is ActionState.SUCCEEDED and outcome.verified is True
        assert "confirmed to have taken effect" in outcome.detail

    def test_an_unknown_outcome_is_never_automatically_retried(self, client, db, confirmations):
        """Observed rather than asserted about the source: the handler counts its own calls."""
        calls: list[int] = []

        def counting_handler(ctx, args):
            calls.append(args.listing_id)
            raise RuntimeError("timed out mid-write")

        registry = ToolRegistry()
        registry.register(
            _action_spec(handler=counting_handler, verify=lambda ctx, args: (_ for _ in ()).throw(RuntimeError("x")))
        )
        context = context_for(client, db, "fail5@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-noretry-0001")

        assert outcome.state is ActionState.UNKNOWN
        assert len(calls) == 1, "an unknown mutation was retried"

    def test_a_slow_but_committed_mutation_is_not_reported_as_a_timeout(self, client, db, confirmations):
        """The deadline is checked after the handler returns, because a synchronous write cannot
        be interrupted from Python. So an action that overran may well have committed, and
        raising a timeout would tell a student their change did not happen when the row is
        already there."""
        import time

        def slow_handler(ctx, args):
            time.sleep(0.05)
            return SavedJobActionResult(listing_id=args.listing_id, title="t", tracked=True)

        registry = ToolRegistry()
        registry.register(_action_spec(handler=slow_handler, timeout_seconds=0.01))
        context = context_for(client, db, "slow7@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-slow-000001")

        assert outcome.state is ActionState.SUCCEEDED and outcome.verified is True

    def test_a_slow_action_that_did_not_take_effect_is_not_reported_as_success(
        self, client, db, confirmations
    ):
        import time

        def slow_handler(ctx, args):
            time.sleep(0.05)
            return SavedJobActionResult(listing_id=args.listing_id, title="t", tracked=True)

        registry = ToolRegistry()
        registry.register(
            _action_spec(handler=slow_handler, timeout_seconds=0.01, verify=lambda ctx, args: False)
        )
        context = context_for(client, db, "slow8@example.com")

        outcome = run(registry, confirmations, context, "test.act", {"listing_id": 1}, key="k-slow-000002")

        assert outcome.state is ActionState.FAILED and outcome.verified is False

    def test_an_outcome_cannot_claim_success_without_verification(self):
        from app.tools.contracts import ActionOutcome

        with pytest.raises(ValueError):
            ActionOutcome(
                capability="x", state=ActionState.SUCCEEDED, verified=False, description="d", resource_label="r"
            )
        with pytest.raises(ValueError):
            ActionOutcome(
                capability="x", state=ActionState.FAILED, verified=True, description="d", resource_label="r"
            )

    def test_cancellation_before_execution_prevents_the_mutation(self, client, db, registry, confirmations):
        cancellation = CancellationToken()
        context = ToolContext.authenticate(
            token_for(client, "cancel7@example.com"), db, cancellation=cancellation
        )
        profile = profile_for(db, "cancel7@example.com")
        listing = make_listing(db)
        token, _grant, _preview = registry.preview_action(
            "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
        )
        cancellation.cancel()

        with pytest.raises(ToolCancelled):
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key="k-cancel-00001",
                confirmations=confirmations,
            )
        assert tracked(db, profile.id, listing.id) is None
        # Cancelled before the confirmation was spent, so the student's approval is intact.
        assert confirmations.peek(_grant.confirmation_id) is not None


# --------------------------------------------------------------------------- entitlement


class TestEntitlement:
    def test_a_premium_action_is_refused_for_a_free_account(self, client, db, confirmations):
        registry = ToolRegistry()
        registry.register(_action_spec(name="test.premium_act", requires_premium=True))
        context = context_for(client, db, "ent7@example.com")

        with pytest.raises(ToolPermissionDenied):
            registry.preview_action("test.premium_act", {"listing_id": 1}, context, confirmations=confirmations)

    def test_a_premium_action_is_refused_at_execution_too(self, client, db, confirmations):
        """Entitlement is re-checked rather than inherited from the preview: a subscription can
        lapse between the two, and the preview's verdict is a fact about a moment that passed."""
        registry = ToolRegistry()
        spec = _action_spec(name="test.premium_act")
        registry.register(spec)
        context = context_for(client, db, "ent8@example.com")
        token, _grant, _preview = registry.preview_action(
            "test.premium_act", {"listing_id": 1}, context, confirmations=confirmations
        )

        gated = ToolRegistry()
        gated.register(_action_spec(name="test.premium_act", requires_premium=True))
        with pytest.raises(ToolPermissionDenied):
            gated.invoke_action(
                "test.premium_act",
                {"listing_id": 1},
                context,
                confirmation_token=token,
                idempotency_key="k-prem-000001",
                confirmations=confirmations,
            )


# --------------------------------------------------------------------------- logging


class TestNothingSensitiveIsLogged:
    def test_no_confirmation_token_or_argument_reaches_the_logs(
        self, client, db, registry, confirmations, caplog
    ):
        context = context_for(client, db, "log7@example.com")
        listing = make_listing(db, title="Confidential Referral", company="SecretCo")

        with caplog.at_level(logging.DEBUG):
            token, grant, _preview = registry.preview_action(
                "jobs.save", {"listing_id": listing.id}, context, confirmations=confirmations
            )
            registry.invoke_action(
                "jobs.save",
                {"listing_id": listing.id},
                context,
                confirmation_token=token,
                idempotency_key="k-log-00000001",
                confirmations=confirmations,
            )

        text = caplog.text
        assert token not in text
        assert token.split(".", 1)[1] not in text
        assert grant.confirmation_id not in text
        assert "SecretCo" not in text and "Confidential Referral" not in text
        assert "k-log-00000001" not in text
        # Identity and capability are logged, which is what an audit of "who did what" needs.
        assert "jobs.save" in text and f"user={context.user.id}" in text


# --------------------------------------------------------------------------- HTTP surface


class TestTheHttpSurface:
    def test_preview_then_execute_saves_the_job(self, client, db):
        headers = headers_for(client, "http1@example.com")
        listing = make_listing(db)

        preview = client.post(
            "/actions/preview",
            headers=headers,
            json={"capability": "jobs.save", "arguments": {"listing_id": listing.id}},
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["state"] == "confirmation_required"
        assert body["risk_class"] == "low"
        assert body["requires_idempotency_key"] is True
        assert listing.title in body["description"]

        executed = client.post(
            "/actions/execute",
            headers={**headers, "Idempotency-Key": "k-http-00000001"},
            json={
                "capability": "jobs.save",
                "arguments": {"listing_id": listing.id},
                "confirmation_token": body["confirmation_token"],
            },
        )
        assert executed.status_code == 200, executed.text
        outcome = executed.json()
        assert outcome["state"] == "succeeded" and outcome["verified"] is True
        assert tracked(db, profile_for(db, "http1@example.com").id, listing.id) is not None

    def test_execute_without_a_confirmation_is_refused(self, client, db):
        headers = headers_for(client, "http2@example.com")
        listing = make_listing(db)
        response = client.post(
            "/actions/execute",
            headers={**headers, "Idempotency-Key": "k-http-00000002"},
            json={"capability": "jobs.save", "arguments": {"listing_id": listing.id}},
        )
        # A missing required body field: the request cannot even be formed without one.
        assert response.status_code == 422
        assert tracked(db, profile_for(db, "http2@example.com").id, listing.id) is None

    def test_execute_without_an_idempotency_key_is_refused(self, client, db):
        headers = headers_for(client, "http3@example.com")
        listing = make_listing(db)
        preview = client.post(
            "/actions/preview",
            headers=headers,
            json={"capability": "jobs.save", "arguments": {"listing_id": listing.id}},
        ).json()
        response = client.post(
            "/actions/execute",
            headers=headers,
            json={
                "capability": "jobs.save",
                "arguments": {"listing_id": listing.id},
                "confirmation_token": preview["confirmation_token"],
            },
        )
        assert response.status_code == 400
        assert tracked(db, profile_for(db, "http3@example.com").id, listing.id) is None

    def test_a_reused_token_over_http_is_a_conflict(self, client, db):
        headers = headers_for(client, "http4@example.com")
        listing = make_listing(db)
        preview = client.post(
            "/actions/preview",
            headers=headers,
            json={"capability": "jobs.save", "arguments": {"listing_id": listing.id}},
        ).json()
        body = {
            "capability": "jobs.save",
            "arguments": {"listing_id": listing.id},
            "confirmation_token": preview["confirmation_token"],
        }
        assert client.post(
            "/actions/execute", headers={**headers, "Idempotency-Key": "k-http-00000003"}, json=body
        ).status_code == 200
        second = client.post(
            "/actions/execute", headers={**headers, "Idempotency-Key": "k-http-00000004"}, json=body
        )
        assert second.status_code == 409

    def test_an_unregistered_capability_is_a_404(self, client, db):
        headers = headers_for(client, "http5@example.com")
        response = client.post(
            "/actions/preview", headers=headers, json={"capability": "account.delete", "arguments": {}}
        )
        assert response.status_code == 404
        assert "jobs.save" not in response.text

    def test_a_read_tool_cannot_be_driven_through_the_action_endpoint(self, client, db):
        headers = headers_for(client, "http6@example.com")
        response = client.post(
            "/actions/preview", headers=headers, json={"capability": "profile.get_summary", "arguments": {}}
        )
        assert response.status_code == 403

    def test_the_endpoints_require_authentication(self, client, db):
        assert client.post("/actions/preview", json={"capability": "jobs.save", "arguments": {}}).status_code == 401
        assert client.post(
            "/actions/execute",
            json={"capability": "jobs.save", "arguments": {}, "confirmation_token": "x.y"},
        ).status_code == 401

    def test_another_students_private_listing_is_a_404_over_http(self, client, db):
        token_for(client, "httpowner@example.com")
        owner_id = profile_for(db, "httpowner@example.com").id
        private = make_listing(db, title="Private Referral", owner_profile_id=owner_id)
        headers = headers_for(client, "httpstranger@example.com")

        response = client.post(
            "/actions/preview",
            headers=headers,
            json={"capability": "jobs.save", "arguments": {"listing_id": private.id}},
        )
        assert response.status_code == 404
        assert "Private Referral" not in response.text and str(private.id) not in response.text

    def test_an_invented_top_level_field_is_refused(self, client, db):
        headers = headers_for(client, "http7@example.com")
        listing = make_listing(db)
        response = client.post(
            "/actions/preview",
            headers=headers,
            json={"capability": "jobs.save", "arguments": {"listing_id": listing.id}, "confirmed": True},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------- declarations


class TestTheDeclarationsCannotLie:
    def test_every_action_declares_a_preview_a_verifier_and_a_risk_class(self):
        for spec in action_specs():
            assert spec.kind is ToolKind.ACTION
            assert spec.preview is not None and spec.verify is not None
            assert spec.risk_class is not None
            assert spec.verification is Verification.USER_CONFIRMATION
            assert spec.idempotency is Idempotency.KEY_REQUIRED
            assert spec.side_effects, "a write tool that declares no side effect is lying"

    @pytest.mark.parametrize(
        ("missing", "phrase"),
        [("preview", "must declare a preview"), ("verify", "authoritative verifier"), ("risk_class", "risk class")],
    )
    def test_an_action_missing_a_mandatory_declaration_cannot_be_constructed(self, missing, phrase):
        with pytest.raises(ValueError) as caught:
            _action_spec(**{missing: None})
        assert phrase in str(caught.value)

    def test_a_read_tool_cannot_declare_a_preview_or_a_verifier(self):
        from app.tools.read_tools import NoArguments, ProfileSummary

        for extra in ({"preview": lambda c, a: None}, {"verify": lambda c, a: True}, {"risk_class": RiskClass.LOW}):
            with pytest.raises(ValueError):
                ToolSpec(
                    name="test.read",
                    kind=ToolKind.READ,
                    summary="a read tool",
                    input_model=NoArguments,
                    output_model=ProfileSummary,
                    handler=lambda ctx, args: ProfileSummary(),
                    **extra,
                )

    def test_the_voice_protocol_still_carries_no_action_messages(self):
        """VA-7 is a server-side boundary plus a confirmation screen. It must not have grown a
        realtime action channel as a side effect.

        The gateway now runs real conversations, so the set it sends has grown from three types
        to twelve. The claim this guard makes is unchanged, and is now checked against the
        protocol's own allowlist rather than a literal: **no action message is among them.**
        `operation_outcome` is the one that would matter -- it is the only frame that can resolve
        a pending operation on the client -- and this path mints no confirmable operation at all.

        Two fixes to the regex while updating it, both of which made the guard weaker than it
        looked. It required `protocol.` to sit immediately after `send_text(`, so every
        *multi-line* send was invisible to it -- three of the twelve, including both of the types
        added for the realtime turn. And it ignored `send_bytes` entirely. A guard that cannot
        see a send site cannot forbid anything sent from one.
        """
        import pathlib
        import re

        from app.voice import realtime_protocol as protocol

        gateway = pathlib.Path(__file__).resolve().parents[2] / "app" / "api" / "voice_session.py"
        assert gateway.exists(), f"the voice gateway moved; this guard is pointing at nothing: {gateway}"
        builders = set(
            re.findall(
                r"send_(?:text|bytes)\(\s*protocol\.([a-z_]+)\(",
                gateway.read_text(encoding="utf-8"),
            )
        )
        assert builders, "no send site was found at all, so this test proves nothing"

        # `encode_audio` builds a binary audio frame, not a control message, so it is not a
        # message type. Named explicitly rather than filtered silently, so that losing the audio
        # send site fails here instead of quietly shrinking what this test examines.
        assert "encode_audio" in builders, "the audio send site vanished; the regex is stale"
        sent = builders - {"encode_audio"}

        assert sent <= protocol.SUPPORTED_SERVER_MESSAGE_TYPES, sent
        assert not sent & protocol.FORBIDDEN_SERVER_MESSAGE_TYPES, sent
        assert "operation_outcome" not in sent
        assert "tool_call_started" not in sent
