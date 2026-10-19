"""VA-5: world tools through the Tool Registry, with the real authorization boundary.

Real signup, real tokens, real database. The provider is a deterministic fake injected into
the factory, because this environment has no usable credentials and VA-5 may not make
unauthorised outbound calls -- but everything between the token and the provider is the real
code path.

What these are for: a world tool reaches outside the deployment, so it adds failure modes a
personal read tool does not have, and each of them must stay distinguishable. "No provider
configured" is not "no jobs found"; a spent quota is not an outage; and neither may be
answered with a plausible-looking empty list.
"""

import datetime

import pytest

from app.tools.contracts import ToolContext, ToolInputInvalid, ToolKind
from app.tools.registry import build_default_registry
from app.tools.world_tools import WorldToolError
from app.world import job_market
from tests.fixtures import replay_harness as h
from tests.unit.test_world_job_market import FakeProvider, MeteredProvider, job

NOW = datetime.datetime(2026, 10, 11, 12, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture()
def engine(tmp_path):
    from sqlalchemy.orm import sessionmaker

    from app.database.base import Base
    from app.models.role import Role

    eng = h.make_engine(tmp_path / "va5_world_tools.db")
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


@pytest.fixture(autouse=True)
def clear_cache():
    job_market.reset_cache()
    yield
    job_market.reset_cache()


@pytest.fixture()
def registry():
    return build_default_registry()


def use_provider(monkeypatch, provider):
    """Inject a provider at the factory, which is where the tools resolve it."""
    monkeypatch.setattr("app.services.job_hunt.providers.factory.get_job_provider", lambda: provider)


def context_for(client, db, email: str) -> ToolContext:
    headers = h.signup(client, email=email)
    return ToolContext.authenticate(headers["Authorization"].split(" ", 1)[1], db)


def populated(count: int = 12) -> FakeProvider:
    return FakeProvider(jobs=[job(i, company=f"C{i}", title=f"Python Developer {i}") for i in range(1, count + 1)])


class TestRegistration:
    def test_all_world_tools_are_read_only(self, registry):
        world = [registry.get(name) for name in registry.names if name.startswith("world.")]
        assert world and {spec.kind for spec in world} == {ToolKind.READ}
        assert all(spec.side_effects == () for spec in world)

    def test_a_world_tool_declares_its_metering_write(self, registry):
        # A world tool spends a shared provider call and records it. Claiming no writes would
        # have been tidier and false.
        assert registry.get("world.find_openings").metering_effects == ("ProviderCallBudget",)

    def test_there_is_no_learning_resources_tool(self, registry):
        assert not any("learning" in name for name in registry.names)

    def test_the_catalogue_describes_world_tools_to_an_authenticated_caller(self, client, db, registry):
        context = context_for(client, db, "enum-world@example.com")
        described = {entry["name"]: entry for entry in registry.describe_for(context)}
        assert "world.find_openings" in described
        assert described["world.find_openings"]["kind"] == "read"
        assert described["world.find_openings"]["metering_effects"] == ["ProviderCallBudget"]


class TestDataMinimization:
    def test_no_world_input_schema_accepts_personal_data(self, registry):
        forbidden = {
            "resume", "resume_id", "profile", "profile_id", "user_id", "email",
            "skills", "full_name", "mobile_number",
        }
        for name in registry.names:
            if not name.startswith("world."):
                continue
            fields = set(registry.get(name).input_model.model_fields)
            assert not (fields & forbidden), f"{name} accepts {fields & forbidden}"

    @pytest.mark.parametrize("injected", [{"resume_id": 1}, {"profile_id": 2}, {"email": "a@b.c"}])
    def test_attaching_personal_data_is_rejected_not_ignored(self, client, db, registry, monkeypatch, injected):
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "inject-world@example.com")
        with pytest.raises(ToolInputInvalid) as caught:
            registry.invoke_read("world.find_openings", {"role": "python", **injected}, context)
        assert any("extra_forbidden" in field for field in caught.value.fields)

    def test_the_provider_receives_only_query_terms(self, client, db, registry, monkeypatch):
        provider = populated()
        use_provider(monkeypatch, provider)
        context = context_for(client, db, "minimal@example.com")
        registry.invoke_read(
            "world.find_openings", {"role": "python developer", "location": "Pune"}, context
        )
        sent = provider.calls[0]
        assert sent.keywords == "python developer" and sent.location == "Pune"
        assert sent.end_user_ip is None and sent.end_user_agent is None

    def test_no_advert_prose_reaches_the_tool_output(self, client, db, registry, monkeypatch):
        # Untrusted external text, the largest field by far, and no VA-5 consumer needs it.
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "noprose@example.com")
        result = registry.invoke_read("world.find_openings", {"role": "python"}, context)
        assert "description" not in result.output.openings[0].model_fields


class TestProvenanceInOutputs:
    def test_every_row_carries_provider_freshness_and_evidence(self, client, db, registry, monkeypatch):
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "prov@example.com")
        row = registry.invoke_read("world.find_openings", {"role": "python"}, context).output.openings[0]
        assert row.provenance.provider == "fake"
        assert row.provenance.source_kind == "live_provider"
        assert row.provenance.freshness in {"fresh", "aging", "stale", "undated"}
        assert row.provenance.evidence in {"confirmed", "partial", "conflicting"}
        assert row.provenance.attribution == "Jobs by Fake"

    def test_an_aggregate_carries_the_sample_it_was_computed_over(self, client, db, registry, monkeypatch):
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "sample@example.com")
        output = registry.invoke_read("world.get_skill_demand", {"role": "python"}, context).output
        assert output.available is True
        assert output.sample is not None
        assert output.sample.unique_count >= 8
        assert output.provenance.source_kind == "derived"

    def test_capability_limitations_travel_with_the_result(self, client, db, registry, monkeypatch):
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "limits@example.com")
        output = registry.invoke_read("world.find_openings", {"role": "python"}, context).output
        assert output.capability_limitations
        assert any("not the whole market" in note for note in output.capability_limitations)


class TestHonestUnavailability:
    def test_an_unconfigured_source_is_a_typed_error_not_an_empty_list(self, client, db, registry, monkeypatch):
        from app.services.job_hunt.providers.null import NullJobProvider

        use_provider(monkeypatch, NullJobProvider())
        context = context_for(client, db, "unconfigured@example.com")
        with pytest.raises(WorldToolError) as caught:
            registry.invoke_read("world.find_openings", {"role": "python"}, context)
        assert caught.value.code == "source_not_configured"
        # An empty list would tell a student "there are no jobs matching you", which is a
        # claim about the labour market nobody here has evidence for.
        assert "no jobs found" not in caught.value.safe_message().lower()

    def test_each_provider_failure_keeps_its_own_code(self, client, db, registry, monkeypatch):
        from app.services.job_hunt.providers.base import (
            JobProviderAuthError,
            JobProviderRateLimitError,
            JobProviderTimeoutError,
        )

        cases = {
            JobProviderRateLimitError("x"): "source_quota_exhausted",
            JobProviderAuthError("x"): "source_rejected_credentials",
            JobProviderTimeoutError("x"): "source_unavailable",
        }
        context = context_for(client, db, "codes@example.com")
        for index, (raised, expected) in enumerate(cases.items()):
            job_market.reset_cache()
            use_provider(monkeypatch, FakeProvider(raises=raised))
            with pytest.raises(WorldToolError) as caught:
                registry.invoke_read("world.find_openings", {"role": f"r{index}"}, context)
            assert caught.value.code == expected

    def test_a_provider_message_never_reaches_the_caller(self, client, db, registry, monkeypatch):
        from app.services.job_hunt.providers.base import JobProviderUnavailableError

        use_provider(monkeypatch, FakeProvider(raises=JobProviderUnavailableError("key=SUPERSECRET")))
        context = context_for(client, db, "redact-world@example.com")
        with pytest.raises(WorldToolError) as caught:
            registry.invoke_read("world.find_openings", {"role": "python"}, context)
        assert "SUPERSECRET" not in str(caught.value)
        assert "SUPERSECRET" not in caught.value.safe_message()

    def test_a_thin_sample_is_reported_as_unavailable_with_a_reason(self, client, db, registry, monkeypatch):
        use_provider(monkeypatch, FakeProvider(jobs=[job(1, company="One")]))
        context = context_for(client, db, "thin@example.com")
        output = registry.invoke_read("world.get_skill_demand", {"role": "python"}, context).output
        assert output.available is False
        assert output.evidence == "insufficient"
        assert output.skills == []
        assert output.remediation

    def test_a_quota_exhausted_source_is_distinguishable_from_an_outage(self, client, db, registry, monkeypatch):
        provider = MeteredProvider(jobs=[job(1, company="One")])
        use_provider(monkeypatch, provider)
        context = context_for(client, db, "quota@example.com")
        for index in range(2):
            registry.invoke_read("world.find_openings", {"role": f"role-{index}"}, context)
        with pytest.raises(WorldToolError) as caught:
            registry.invoke_read("world.find_openings", {"role": "role-3"}, context)
        # Not fixed by the student paying, so it must not read like an entitlement problem.
        assert caught.value.code == "source_quota_exhausted"


class TestBoundsAndArguments:
    def test_results_are_bounded_and_truncation_is_reported(self, client, db, registry, monkeypatch):
        use_provider(monkeypatch, populated(count=40))
        context = context_for(client, db, "bounded@example.com")
        result = registry.invoke_read("world.find_openings", {"role": "python", "limit": 10}, context)
        assert len(result.output.openings) <= 10
        assert result.output.truncated is True

    @pytest.mark.parametrize(
        "arguments",
        [
            {"limit": 0}, {"limit": 999}, {"max_days_old": 0}, {"max_days_old": 1000},
            {"role": "x" * 500}, {"remote_only": "maybe"},
        ],
    )
    def test_out_of_range_arguments_are_refused(self, client, db, registry, monkeypatch, arguments):
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "range@example.com")
        with pytest.raises(ToolInputInvalid):
            registry.invoke_read("world.find_openings", arguments, context)

    def test_a_comparison_needs_at_least_two_locations_and_at_most_three(
        self, client, db, registry, monkeypatch
    ):
        # Each location costs a provider call against the same shared daily budget.
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "compare@example.com")
        for bad in ({"locations": ["Pune"]}, {"locations": ["A", "B", "C", "D"]}):
            with pytest.raises(ToolInputInvalid):
                registry.invoke_read("world.compare_locations", bad, context)

    def test_a_valid_comparison_returns_both_samples(self, client, db, registry, monkeypatch):
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "compare-ok@example.com")
        output = registry.invoke_read(
            "world.compare_locations", {"role": "python", "locations": ["Pune", "Bangalore"]}, context
        ).output
        assert set(output.openings_by_location) == {"Pune", "Bangalore"}
        assert set(output.sample_sizes) == {"Pune", "Bangalore"}


class TestNoCrossUserLeakage:
    def test_two_accounts_get_the_same_public_data_and_nothing_private(
        self, client, db, registry, monkeypatch
    ):
        # World data is public by definition, so the thing worth asserting is the negative:
        # nothing in the response is scoped to, or derived from, either account.
        use_provider(monkeypatch, populated())
        alice = context_for(client, db, "alice-world@example.com")
        bob = context_for(client, db, "bob-world@example.com")
        first = registry.invoke_read("world.find_openings", {"role": "python"}, alice).output
        second = registry.invoke_read("world.find_openings", {"role": "python"}, bob).output

        # Compared on the advert content, not byte-for-byte: `retrieved_at` differs between two
        # calls and the second is served from this worker's cache, which it says so in its own
        # limitations. Both of those are honest differences, and asserting exact equality would
        # have been asserting that the provenance is *not* per-call -- the opposite of the
        # design.
        def adverts(output):
            return [
                (row.title, row.company, row.location, tuple(row.skills), row.provenance.provider)
                for row in output.openings
            ]

        assert adverts(first) == adverts(second)
        assert any("cache" in note for note in second.openings[0].provenance.limitations)

    def test_a_world_tool_does_not_touch_the_callers_profile(self, client, db, registry, monkeypatch):
        # It needs no profile at all: a world query carries no personal data, so requiring one
        # would be an access it has no use for.
        use_provider(monkeypatch, populated())
        context = context_for(client, db, "noprofile@example.com")
        context.profile = None
        registry.invoke_read("world.find_openings", {"role": "python"}, context)
        assert context.profile is None

    def test_no_world_tool_writes_domain_state(self, client, db, registry, monkeypatch):
        from sqlalchemy import event

        use_provider(monkeypatch, populated())
        context = context_for(client, db, "nowrite-world@example.com")
        recorded: list[str] = []

        def before_flush(sess, _ctx, _instances):
            recorded.extend(type(obj).__name__ for obj in list(sess.new) + list(sess.dirty) + list(sess.deleted))

        event.listen(db, "before_flush", before_flush)
        try:
            for name in sorted(n for n in registry.names if n.startswith("world.")):
                arguments = {"role": "python"}
                if name == "world.compare_locations":
                    arguments = {"role": "python", "locations": ["Pune", "Bangalore"]}
                registry.invoke_read(name, arguments, context)
            db.flush()
        finally:
            event.remove(db, "before_flush", before_flush)

        assert "JobListing" not in recorded, "a world retrieval must not persist external adverts"
        assert "StudentProfile" not in recorded
        assert set(recorded) <= {"ProviderCallBudget"}, recorded
