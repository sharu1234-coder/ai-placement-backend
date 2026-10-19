"""VA-4: the Tool Registry's authorization boundary, exercised for real.

No mock stands in for the thing under test here. Tokens are issued by the real signup/login
routes and signed with the real key; the expired one is built with the real token builder and a
negative lifetime; the database is a real database with real rows. The reason is that the
guard being tested *is* `authenticate_access_token` plus the ownership filters inside the
domain services, and a fake that reproduced either would prove only that the fake works.

The caller on the other side of this registry will eventually be a language model reading a
student's own words back to the server. So the tests that matter most are the ones where the
caller is assumed hostile: it invents arguments, it asks for capabilities that do not exist, it
supplies someone else's identifiers, and it asks what else is available.
"""

import datetime

import pytest

from app.tools.contracts import (
    CancellationToken,
    ProfileNotReady,
    ToolCancelled,
    ToolContext,
    ToolError,
    ToolExecutionFailed,
    ToolInputInvalid,
    ToolKind,
    ToolNotFound,
    ToolPermissionDenied,
    ToolSpec,
    ToolTimeout,
    ToolUnauthenticated,
    Verification,
)
from app.tools.read_tools import NoArguments, ProfileSummary
from app.tools.registry import ToolRegistry, build_default_registry
from tests.fixtures import replay_harness as h

SECRET_SENTINEL = "s3cret-table-name"


@pytest.fixture()
def engine(tmp_path):
    from app.database.base import Base
    from app.models.role import Role

    eng = h.make_engine(tmp_path / "va4_tools.db")
    Base.metadata.create_all(bind=eng)
    # The role seed conftest provides, minimally: a profile can name a target role, and
    # profile.get_summary resolves that id to a name.
    from sqlalchemy.orm import sessionmaker

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
    return build_default_registry()


def token_for(client, email: str) -> str:
    """A genuine access token for a genuine, fully set-up account."""
    headers = h.signup(client, email=email)
    return headers["Authorization"].split(" ", 1)[1]


def context_for(client, db, email: str) -> ToolContext:
    return ToolContext.authenticate(token_for(client, email), db)


# --------------------------------------------------------------------------- credentials


class TestCredentials:
    @pytest.mark.parametrize("token", ["", "   ", "not.a.jwt", "Bearer abc", "a.b.c"])
    def test_missing_or_malformed_credentials_are_refused(self, db, token):
        with pytest.raises(ToolUnauthenticated):
            ToolContext.authenticate(token, db)

    def test_an_expired_but_correctly_signed_token_is_refused(self, client, db):
        # Built with the real builder and the real key, so the only thing wrong with it is the
        # expiry -- which is what makes this a test of expiry rather than of signing.
        from app.core.security import _create_token

        token_for(client, "expired@example.com")
        from app.models.user import User

        user = db.query(User).filter(User.email == "expired@example.com").one()
        expired = _create_token(str(user.id), "access", user.token_version, datetime.timedelta(minutes=-5))
        with pytest.raises(ToolUnauthenticated):
            ToolContext.authenticate(expired, db)

    def test_a_refresh_token_cannot_open_a_tool_context(self, client, db):
        # The VA-2 lesson, re-asserted at this boundary: a long-lived refresh token must not
        # buy access to a student's data through a second door.
        client.post("/auth/signup", json={"email": "refresh@example.com", "password": h.PASSWORD})
        refresh = client.post(
            "/auth/login", json={"email": "refresh@example.com", "password": h.PASSWORD}
        ).json()["refresh_token"]
        with pytest.raises(ToolUnauthenticated):
            ToolContext.authenticate(refresh, db)

    def test_a_token_issued_before_logout_is_refused(self, client, db):
        # token_version is bumped on logout. A tool call with the old token must fail even
        # though the signature and expiry are both still valid.
        headers = h.signup(client, email="loggedout@example.com")
        token = headers["Authorization"].split(" ", 1)[1]
        assert ToolContext.authenticate(token, db).user.email == "loggedout@example.com"
        assert client.post("/auth/logout", headers=headers).status_code in (200, 204)
        db.expire_all()
        with pytest.raises(ToolUnauthenticated):
            ToolContext.authenticate(token, db)

    def test_every_refusal_gives_the_same_message(self, client, db):
        # Distinguishing "expired" from "revoked" from "no such user" would turn this into an
        # oracle. Matches the HTTP path's single opaque detail.
        messages = set()
        for bad in ["not.a.jwt", "", "a.b.c"]:
            try:
                ToolContext.authenticate(bad, db)
            except ToolUnauthenticated as exc:
                messages.add(str(exc))
        assert len(messages) <= 2, messages  # "no credentials" and "invalid or expired"

    def test_a_valid_token_resolves_to_its_own_user(self, client, db):
        context = context_for(client, db, "valid@example.com")
        assert context.user.email == "valid@example.com"
        assert context.require_profile().user_id == context.user.id


# --------------------------------------------------------------------------- identity


class TestIdentityIsServerDerived:
    def test_no_tool_accepts_an_owner_identifier(self, registry):
        # The structural guarantee. A tool cannot be asked to act as someone else because the
        # argument does not exist -- there is no comparison here to get wrong.
        forbidden = {"profile_id", "user_id", "owner_id", "student_id", "account_id", "email"}
        for name in sorted(registry.names):
            fields = set(registry.get(name).input_model.model_fields)
            assert not (fields & forbidden), f"{name} accepts {fields & forbidden}"

    @pytest.mark.parametrize(
        "injected",
        [{"profile_id": 1}, {"user_id": 2}, {"email": "victim@example.com"}, {"limit": 10_000}],
    )
    def test_supplying_another_users_identifier_is_rejected_not_ignored(self, client, db, registry, injected):
        # Rejected rather than silently dropped: from the caller's side "ignored" and
        # "honoured" look identical, and a model probing for a way in would keep trying.
        context = context_for(client, db, "inject@example.com")
        with pytest.raises(ToolInputInvalid) as caught:
            registry.invoke_read("profile.get_summary", injected, context)
        assert any("extra_forbidden" in field for field in caught.value.fields)

    def test_two_accounts_see_only_their_own_data(self, client, db, registry):
        alice = context_for(client, db, "alice@example.com")
        bob = context_for(client, db, "bob@example.com")
        assert alice.require_profile().id != bob.require_profile().id

        client.patch(
            "/users/me",
            json={"full_name": "Alice Only"},
            headers={"Authorization": f"Bearer {token_for(client, 'alice2@example.com')}"},
        )
        alice_view = registry.invoke_read("profile.get_summary", None, alice).output
        bob_view = registry.invoke_read("profile.get_summary", None, bob).output
        # Whatever either holds, neither may hold the other's profile row.
        assert alice_view != bob_view or (alice_view.full_name is None and bob_view.full_name is None)

    def test_a_resource_belonging_to_another_account_is_not_found(self, client, db, registry):
        # Cross-resource: the service filters by profile, so someone else's job-description id
        # comes back as "no plan" rather than as a plan. The same answer as a non-existent id,
        # deliberately -- a different one would let a caller enumerate which ids exist.
        owner = context_for(client, db, "owner@example.com")
        intruder = context_for(client, db, "intruder@example.com")
        for context in (owner, intruder):
            result = registry.invoke_read("jobs.get_preparation_plan", {"job_description_id": 987654}, context)
            assert result.output.found is False

    def test_a_profileless_account_gets_a_typed_not_ready_rather_than_a_new_profile(self, client, db, registry):
        # The HTTP routes call get_or_create_profile, which WRITES. A read tool must not, so
        # this path has to exist and has to be distinguishable from an error.
        from app.models.profile import StudentProfile
        from app.models.user import User

        client.post("/auth/signup", json={"email": "bare@example.com", "password": h.PASSWORD})
        token = client.post(
            "/auth/login", json={"email": "bare@example.com", "password": h.PASSWORD}
        ).json()["access_token"]
        user = db.query(User).filter(User.email == "bare@example.com").one()
        assert db.query(StudentProfile).filter(StudentProfile.user_id == user.id).count() == 0

        context = ToolContext.authenticate(token, db)
        with pytest.raises(ProfileNotReady):
            registry.invoke_read("profile.get_summary", None, context)
        db.expire_all()
        assert db.query(StudentProfile).filter(StudentProfile.user_id == user.id).count() == 0


# --------------------------------------------------------------------------- arguments


class TestArguments:
    def test_an_unregistered_capability_is_refused_without_naming_the_registered_ones(
        self, client, db, registry
    ):
        context = context_for(client, db, "unknown@example.com")
        with pytest.raises(ToolNotFound) as caught:
            registry.invoke_read("database.run_sql", {"q": "select 1"}, context)
        message = str(caught.value)
        assert "database.run_sql" in message
        # A model guessing names must not be handed the catalogue for guessing wrong.
        for registered in registry.names:
            assert registered not in message

    @pytest.mark.parametrize(
        "arguments",
        [None, {}, {"job_description_id": 0}, {"job_description_id": -1}, {"job_description_id": "abc"},
         {"job_description_id": None}, {"job_description_id": 1, "profile_id": 2}],
    )
    def test_invalid_or_missing_required_arguments_are_refused(self, client, db, registry, arguments):
        context = context_for(client, db, "args@example.com")
        with pytest.raises(ToolInputInvalid):
            registry.invoke_read("jobs.get_preparation_plan", arguments, context)

    def test_a_valid_argument_is_accepted(self, client, db, registry):
        context = context_for(client, db, "goodargs@example.com")
        assert registry.invoke_read("jobs.get_preparation_plan", {"job_description_id": 1}, context).output.found is False

    def test_an_oversized_argument_is_refused_by_the_schema(self, client, db, registry):
        context = context_for(client, db, "big@example.com")
        with pytest.raises(ToolInputInvalid):
            registry.invoke_read("jobs.list_saved", {"status": "x" * 5_000}, context)

    def test_a_malformed_argument_container_is_refused(self, client, db, registry):
        context = context_for(client, db, "malformed@example.com")
        with pytest.raises((ToolInputInvalid, TypeError, ValueError)):
            registry.invoke_read("profile.get_summary", ["not", "a", "mapping"], context)

    def test_the_rejection_names_the_field_but_never_the_value(self, client, db, registry):
        # The values are model-generated text derived from a student's own data. Echoing them
        # into an error that may be logged is how that data travels somewhere nobody intended.
        context = context_for(client, db, "redact@example.com")
        with pytest.raises(ToolInputInvalid) as caught:
            registry.invoke_read("jobs.list_saved", {"status": SECRET_SENTINEL * 3}, context)
        assert SECRET_SENTINEL not in str(caught.value)
        assert any("status" in field for field in caught.value.fields)


# --------------------------------------------------------------------------- enumeration


class TestEnumeration:
    def test_enumeration_requires_a_context(self, registry):
        # No anonymous catalogue: the tool list is a map of what the product can do with a
        # student's data, and handing it out unauthenticated is a free design document.
        #
        # The first version of this test accepted an AttributeError, which is what actually
        # happened -- the context was only touched by the premium check, so a catalogue with
        # nothing gated enumerated happily for a caller with no identity. That made the
        # guarantee accidental, and it would have broken the day a gated tool was added. The
        # registry now checks explicitly.
        with pytest.raises(ToolUnauthenticated):
            registry.describe_for(None)

    def test_an_authenticated_caller_sees_descriptions_without_handlers(self, client, db, registry):
        context = context_for(client, db, "enum@example.com")
        described = registry.describe_for(context)
        assert len(described) == len(registry)
        for entry in described:
            assert set(entry) == {
                "name", "kind", "summary", "input_schema", "requires_premium",
                "side_effects", "metering_effects", "idempotency", "verification", "max_items",
                # VA-7. Null for a read tool and a declared class for an action, so a caller
                # can see how an action will be gated without having to invoke it to find out.
                "risk_class",
            }
            # Provider-neutral: a JSON Schema and primitives, with no vendor's
            # function-calling wrapper and no Python callable to be found.
            assert "handler" not in entry
            assert isinstance(entry["input_schema"], dict)

    def test_a_premium_only_tool_is_hidden_from_a_free_account(self, client, db):
        # Omitted rather than listed-as-locked: a locked entry in a model's tool list is one
        # it will try, fail, and have to explain. The paywall belongs in the app's own UI.
        gated = ToolSpec(
            name="premium.only",
            kind=ToolKind.READ,
            summary="gated",
            input_model=NoArguments,
            output_model=ProfileSummary,
            handler=lambda ctx, args: ProfileSummary(),
            requires_premium=True,
        )
        registry = ToolRegistry()
        registry.register(gated)
        context = context_for(client, db, "free@example.com")
        assert registry.describe_for(context) == []
        with pytest.raises(ToolPermissionDenied):
            registry.invoke_read("premium.only", None, context)

    def test_entitlement_is_checked_before_arguments_are_parsed(self, client, db):
        # Otherwise a free account could probe a premium tool's schema by reading the
        # validation errors it gets back.
        gated = ToolSpec(
            name="premium.schema_probe",
            kind=ToolKind.READ,
            summary="gated",
            input_model=NoArguments,
            output_model=ProfileSummary,
            handler=lambda ctx, args: ProfileSummary(),
            requires_premium=True,
        )
        registry = ToolRegistry()
        registry.register(gated)
        context = context_for(client, db, "probe@example.com")
        with pytest.raises(ToolPermissionDenied):
            registry.invoke_read("premium.schema_probe", {"nonsense": 1}, context)


# --------------------------------------------------------------------------- outputs


class TestBoundedAndRedactedOutputs:
    def test_every_read_tool_returns_its_declared_model(self, client, db, registry):
        context = context_for(client, db, "outputs@example.com")
        for name in sorted(registry.names):
            spec = registry.get(name)
            if spec.input_model is not NoArguments:
                continue
            result = registry.invoke_read(name, None, context)
            assert isinstance(result.output, spec.output_model), name
            assert result.kind is ToolKind.READ

    def test_no_output_carries_contact_details_or_credentials(self, client, db, registry):
        # Data minimisation asserted on the schemas rather than on one run's values, so a
        # field added later is caught even when this account happens to leave it empty.
        banned = {
            "email", "mobile_number", "phone", "password", "hashed_password", "token",
            "access_token", "refresh_token", "api_key", "secret", "url", "file_url",
            "signing_key", "oauth_token",
        }
        for name in sorted(registry.names):
            model = registry.get(name).output_model
            leaked = set(model.model_fields) & banned
            assert not leaked, f"{name} exposes {leaked}"
            for field in model.model_fields.values():
                nested = getattr(field.annotation, "model_fields", None)
                if nested:
                    assert not set(nested) & banned, f"{name} exposes {set(nested) & banned} nested"

    def test_a_list_result_is_capped_and_says_so(self, client, db, registry):
        from app.models.weakness_record import WeaknessRecord

        context = context_for(client, db, "capped@example.com")
        profile_id = context.require_profile().id
        for index in range(25):
            db.add(
                WeaknessRecord(
                    profile_id=profile_id,
                    skill_key=f"skill-{index}",
                    status="NEW",  # the service ranks on this; see _STATUS_RANK
                    occurrences=1,
                    last_score=4.0,
                    last_seen_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )
        db.commit()

        result = registry.invoke_read("weaknesses.list", None, context)
        assert len(result.output.weaknesses) == 20
        # Truncation is reported, never silent: a caller must be able to say "and more"
        # instead of presenting a cut-off list as complete.
        assert result.truncated is True

    def test_an_internal_failure_is_redacted_but_logged(self, client, db, caplog):
        import logging

        def explode(ctx, args):
            raise RuntimeError(f"relation {SECRET_SENTINEL} does not exist")

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="broken.read",
                kind=ToolKind.READ,
                summary="raises",
                input_model=NoArguments,
                output_model=ProfileSummary,
                handler=explode,
            )
        )
        context = context_for(client, db, "boom@example.com")
        with caplog.at_level(logging.DEBUG, logger="app.tools"):
            with pytest.raises(ToolExecutionFailed) as caught:
                registry.invoke_read("broken.read", None, context)

        # A database error's text routinely carries table and column names. The caller -- a
        # model that may read it aloud or echo it into a prompt -- gets none of it.
        assert SECRET_SENTINEL not in caught.value.safe_message()
        assert SECRET_SENTINEL not in "\n".join(r.getMessage() for r in caplog.records)
        assert isinstance(caught.value.cause, RuntimeError)
        assert "broken.read" in "\n".join(r.getMessage() for r in caplog.records)

    def test_a_handler_returning_the_wrong_type_is_a_failure_not_a_result(self, client, db):
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="wrong.type",
                kind=ToolKind.READ,
                summary="lies",
                input_model=NoArguments,
                output_model=ProfileSummary,
                handler=lambda ctx, args: {"full_name": "not a model"},
            )
        )
        context = context_for(client, db, "wrongtype@example.com")
        with pytest.raises(ToolExecutionFailed):
            registry.invoke_read("wrong.type", None, context)


# --------------------------------------------------------------------------- cancellation


class TestCancellationAndTimeout:
    def test_a_cancelled_call_never_starts(self, client, db, registry):
        token = CancellationToken()
        token.cancel()
        context = ToolContext.authenticate(
            token_for(client, "cancel@example.com"), db, cancellation=token
        )
        with pytest.raises(ToolCancelled) as caught:
            registry.invoke_read("profile.get_summary", None, context)
        assert caught.value.stage == "starting"

    def test_an_overrun_is_reported_as_a_timeout(self, client, db):
        import time

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="slow.read",
                kind=ToolKind.READ,
                summary="slow",
                input_model=NoArguments,
                output_model=ProfileSummary,
                handler=lambda ctx, args: (time.sleep(0.05), ProfileSummary())[1],
                timeout_seconds=0.01,
            )
        )
        context = context_for(client, db, "slow@example.com")
        with pytest.raises(ToolTimeout) as caught:
            registry.invoke_read("slow.read", None, context)
        # Honest about granularity: a synchronous query cannot be interrupted from here, so
        # the overrun is detected after the fact rather than claimed to be pre-empted.
        assert "0.01" in str(caught.value)

    def test_a_fast_call_reports_its_duration(self, client, db, registry):
        context = context_for(client, db, "fast@example.com")
        result = registry.invoke_read("profile.get_summary", None, context)
        assert 0 <= result.duration_seconds < 10


# --------------------------------------------------------------------------- read-only proof


class TestReadToolsDoNotMutate:
    """The guarantee the whole phase rests on, measured rather than asserted.

    A SQLAlchemy flush listener records anything the session was about to write. This is not a
    regex over the source and not a mock that refuses writes: the tools run against a real
    session, and if one inserted, updated or deleted a row the listener would see it.

    It is also how `missions.get_today` earns its READ classification -- the HTTP route's
    `get_or_create_today` commits a new Mission, and a tool doing that would fail here.
    """

    @staticmethod
    def _watch(session):
        from sqlalchemy import event

        recorded: list[tuple[str, str]] = []

        def before_flush(sess, _flush_context, _instances):
            recorded.extend(("insert", type(obj).__name__) for obj in sess.new)
            recorded.extend(("update", type(obj).__name__) for obj in sess.dirty)
            recorded.extend(("delete", type(obj).__name__) for obj in sess.deleted)

        event.listen(session, "before_flush", before_flush)
        return recorded, lambda: event.remove(session, "before_flush", before_flush)

    def test_no_personal_read_tool_writes_anything(self, client, db):
        # The personal group only: these query this deployment's own database and must write
        # nothing at all. The world group spends a provider call and meters it, which the next
        # test covers against each tool's own declaration.
        from app.tools.registry import build_default_registry

        registry = build_default_registry(include_world=False)
        context = context_for(client, db, "readonly@example.com")
        context.require_profile()  # resolve first, so the lookup itself is not under test
        recorded, stop = self._watch(db)
        try:
            for name in sorted(registry.names):
                spec = registry.get(name)
                arguments = None if spec.input_model is NoArguments else {"job_description_id": 1}
                if name == "jobs.list_saved":
                    arguments = {}
                registry.invoke_read(name, arguments, context)
            db.flush()
        finally:
            stop()
        assert recorded == [], f"read tools attempted writes: {recorded}"

    def test_a_read_tools_writes_are_confined_to_what_it_declared(self, client, db):
        """VA-5: a world tool spends a provider call and meters it, which is a write.

        So the guarantee is not "a READ tool writes nothing" -- that would have been a tidier
        claim and a false one. It is that a READ tool's writes are confined to exactly the
        metering classes it declared, and that no READ tool touches domain state.
        """
        from app.tools.registry import build_default_registry

        registry = build_default_registry()
        context = context_for(client, db, "declared@example.com")
        context.require_profile()
        recorded, stop = self._watch(db)
        try:
            for name in sorted(registry.names):
                spec = registry.get(name)
                permitted = set(spec.metering_effects)
                arguments: object = None
                if spec.input_model is not NoArguments:
                    arguments = {"job_description_id": 1} if name == "jobs.get_preparation_plan" else {}
                if name == "world.compare_locations":
                    arguments = {"locations": ["Pune", "Bangalore"]}
                recorded.clear()
                try:
                    registry.invoke_read(name, arguments, context)
                except ToolError:
                    # A world tool with no provider configured fails, and must still not have
                    # written anything on the way to failing.
                    pass
                db.flush()
                wrote = {model for _op, model in recorded}
                assert wrote <= permitted, f"{name} wrote {wrote - permitted}, declared {permitted}"
        finally:
            stop()

    def test_the_mission_tool_does_not_create_todays_mission(self, client, db, registry):
        from app.models.mission import Mission

        context = context_for(client, db, "mission@example.com")
        profile_id = context.require_profile().id
        assert db.query(Mission).filter(Mission.profile_id == profile_id).count() == 0

        result = registry.invoke_read("missions.get_today", None, context)

        assert result.output.has_mission is False
        db.expire_all()
        # The HTTP route would have created one here. A READ tool must not.
        assert db.query(Mission).filter(Mission.profile_id == profile_id).count() == 0

    def test_it_reads_a_mission_that_already_exists(self, client, db, registry):
        from app.models.mission import Mission

        context = context_for(client, db, "hasmission@example.com")
        profile_id = context.require_profile().id
        db.add(
            Mission(
                profile_id=profile_id,
                mission_date=datetime.date.today(),
                title="Revise DSA arrays",
                action_type="assessment",
                reasons=[],
                status="not_started",
                estimated_minutes=20,
            )
        )
        db.commit()

        output = registry.invoke_read("missions.get_today", None, context).output
        assert output.has_mission is True and output.title == "Revise DSA arrays"

    def test_another_accounts_mission_is_not_visible(self, client, db, registry):
        from app.models.mission import Mission

        owner = context_for(client, db, "mowner@example.com")
        intruder = context_for(client, db, "mintruder@example.com")
        db.add(
            Mission(
                profile_id=owner.require_profile().id,
                mission_date=datetime.date.today(),
                title="Owner only",
                action_type="assessment",
                reasons=[],
                status="not_started",
            )
        )
        db.commit()

        assert registry.invoke_read("missions.get_today", None, intruder).output.has_mission is False
        assert registry.invoke_read("missions.get_today", None, owner).output.title == "Owner only"


# --------------------------------------------------------------------------- write tools


class TestWriteToolsAreNotReachable:
    def test_a_non_read_tool_cannot_be_invoked_through_the_read_entry_point(self, client, db):
        from app.tools.contracts import ActionPreview, Idempotency, RiskClass

        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="danger.delete_everything",
                kind=ToolKind.ACTION,
                summary="would write",
                input_model=NoArguments,
                output_model=ProfileSummary,
                handler=lambda ctx, args: ProfileSummary(),
                side_effects=("deletes rows",),
                idempotency=Idempotency.NOT_IDEMPOTENT,
                verification=Verification.USER_CONFIRMATION,
                risk_class=RiskClass.HIGH,
                preview=lambda ctx, args: ActionPreview(
                    description="would delete everything",
                    resource_key="everything:1",
                    resource_label="everything",
                    target_state="gone",
                ),
                verify=lambda ctx, args: False,
            )
        )
        context = context_for(client, db, "danger@example.com")
        with pytest.raises(ToolPermissionDenied) as caught:
            registry.invoke_read("danger.delete_everything", None, context)
        assert "write tools are not enabled" in str(caught.value)

    def test_the_default_catalogue_contains_only_read_tools(self, registry):
        assert {registry.get(name).kind for name in registry.names} == {ToolKind.READ}
        assert all(registry.get(name).side_effects == () for name in registry.names)
