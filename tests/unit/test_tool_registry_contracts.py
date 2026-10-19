"""VA-4: the Tool Registry contract, with no database and no credentials.

The authorization boundary is tested for real in `tests/api/test_tool_registry_security.py`.
This file covers the part that must hold before any call happens: a tool cannot declare
something its classification forbids, the catalogue cannot bind itself to one LLM vendor, and
VA-4 must not have quietly switched on the voice protocol's tool-calling messages.
"""

import json
import pathlib
import re

import pytest

from app.tools.contracts import (
    ActionPreview,
    Idempotency,
    RiskClass,
    ToolInput,
    ToolKind,
    ToolOutput,
    ToolSpec,
    Verification,
)
from app.tools.read_tools import NoArguments, ProfileSummary, read_only_specs
from app.tools.registry import MAX_RESULT_CHARACTERS, ToolRegistry, build_default_registry

TOOLS_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "tools"


def spec(**overrides) -> ToolSpec:
    defaults = dict(
        name="test.read",
        kind=ToolKind.READ,
        summary="a test tool",
        input_model=NoArguments,
        output_model=ProfileSummary,
        handler=lambda ctx, args: ProfileSummary(),
    )
    defaults.update(overrides)
    return ToolSpec(**defaults)


def _preview(ctx, args) -> ActionPreview:
    return ActionPreview(
        description="would write", resource_key="thing:1", resource_label="a thing", target_state="written"
    )


#: Everything VA-7 made mandatory for a write tool, in one place, so a test that is about one
#: missing declaration can drop exactly that key.
WRITE = dict(
    name="test.act",
    kind=ToolKind.ACTION,
    side_effects=("creates a row",),
    idempotency=Idempotency.KEY_REQUIRED,
    verification=Verification.USER_CONFIRMATION,
    risk_class=RiskClass.LOW,
    preview=_preview,
    verify=lambda ctx, args: True,
)


class TestClassificationIsEnforced:
    def test_a_read_tool_cannot_declare_a_side_effect(self):
        # The guarantee the read-only phase rests on. If this were only a convention,
        # "read-only" would be a naming style and the registry would have no basis for
        # running a tool without confirmation.
        with pytest.raises(ValueError) as caught:
            spec(side_effects=("sends an email",))
        assert "declares side effects" in str(caught.value)

    def test_a_read_tool_cannot_require_confirmation(self):
        with pytest.raises(ValueError):
            spec(verification=Verification.USER_CONFIRMATION)

    def test_a_read_tool_cannot_claim_a_non_natural_idempotency(self):
        with pytest.raises(ValueError):
            spec(idempotency=Idempotency.KEY_REQUIRED)

    @pytest.mark.parametrize("kind", [ToolKind.ACTION, ToolKind.NAVIGATION])
    def test_a_write_tool_must_declare_verification_and_idempotency(self, kind):
        # VA-4 registers none of these, but the contract is in place now so VA-7 cannot add
        # one that forgot to say whether it is safe to retry.
        with pytest.raises(ValueError) as caught:
            spec(name="test.act", kind=kind, verification=Verification.NONE)
        assert "verification" in str(caught.value)

        with pytest.raises(ValueError) as caught:
            spec(
                name="test.act",
                kind=kind,
                verification=Verification.USER_CONFIRMATION,
                idempotency=Idempotency.NATURAL,
            )
        assert "idempotency" in str(caught.value)

    def test_a_fully_declared_write_tool_is_constructible(self):
        built = spec(**WRITE)
        assert built.kind is ToolKind.ACTION
        assert built.risk_class is RiskClass.LOW

    @pytest.mark.parametrize("name", ["", "has space", "two words"])
    def test_an_unstable_name_is_refused(self, name):
        # The name appears in prompts, transcripts and logs, so it is an interface.
        with pytest.raises(ValueError):
            spec(name=name)

    @pytest.mark.parametrize("overrides", [{"timeout_seconds": 0}, {"timeout_seconds": -1}, {"max_items": 0}])
    def test_non_positive_bounds_are_refused(self, overrides):
        with pytest.raises(ValueError):
            spec(**overrides)


class TestRegistration:
    def test_a_duplicate_name_is_refused(self):
        # Otherwise which implementation runs depends on import order.
        registry = ToolRegistry()
        registry.register(spec())
        with pytest.raises(ValueError):
            registry.register(spec())

    def test_the_personal_catalogue_is_the_documented_inventory(self):
        # VA-5 added world tools to the default catalogue, so the personal group is pinned on
        # its own. Both are asserted rather than only the total: a total would still pass if a
        # personal tool were dropped and a world tool added.
        registry = build_default_registry(include_world=False)
        assert len(registry) == 13
        assert registry.names == {
            "profile.get_summary",
            "skills.get_passport",
            "resume.get_latest_summary",
            "resume.list_ats_checks",
            "career_goals.list",
            "career_twin.get_readiness",
            "career_path.get_current",
            "jobs.list_saved",
            "interviews.list_history",
            "weaknesses.list",
            "jobs.get_preparation_plan",
            "missions.get_today",
            "progress.get_summary",
        }

    def test_the_world_catalogue_is_the_documented_inventory(self):
        from app.tools.world_tools import world_specs

        names = {s.name for s in world_specs()}
        assert names == {
            "world.find_openings",
            "world.find_fresher_openings",
            "world.get_role_requirements",
            "world.get_skill_demand",
            "world.get_company_hiring",
            "world.compare_locations",
        }
        # No learning-resources tool, and that absence is asserted rather than assumed: there
        # is no approved retrieval source for it, so registering one would mean answering from
        # a model's recollection of a URL.
        assert not any("learning" in name for name in names)

    def test_the_default_catalogue_is_both_groups(self):
        registry = build_default_registry()
        assert len(registry) == 19
        assert len([n for n in registry.names if n.startswith("world.")]) == 6

    def test_every_tool_is_read_only(self):
        from app.tools.world_tools import world_specs

        assert {s.kind for s in read_only_specs()} == {ToolKind.READ}
        assert {s.kind for s in world_specs()} == {ToolKind.READ}

    def test_only_world_tools_declare_a_metering_write(self):
        from app.tools.world_tools import world_specs

        # A personal read tool queries this deployment's own database and spends nothing, so
        # it has no quota to meter. A world tool spends a shared provider call and must say so.
        assert all(s.metering_effects == () for s in read_only_specs())
        assert all(s.metering_effects == ("ProviderCallBudget",) for s in world_specs())
        assert all(s.side_effects == () for s in world_specs())

    def test_every_input_model_forbids_unknown_fields(self):
        # The single most important schema property here: an invented `profile_id` must be
        # rejected rather than ignored, and that comes from extra="forbid" on every model.
        for built in read_only_specs():
            assert built.input_model.model_config.get("extra") == "forbid", built.name
            assert issubclass(built.input_model, ToolInput), built.name
            assert issubclass(built.output_model, ToolOutput), built.name


class TestProviderNeutrality:
    def test_a_description_is_plain_json(self):
        # Provider-neutral means a caller can serialise it. A vendor-specific object, or a
        # Python callable leaking into the description, would fail here.
        described = build_default_registry().get("jobs.get_preparation_plan").describe()
        round_tripped = json.loads(json.dumps(described))
        assert round_tripped["name"] == "jobs.get_preparation_plan"
        assert round_tripped["kind"] == "read"
        assert "job_description_id" in round_tripped["input_schema"]["properties"]

    def test_the_registry_imports_no_llm_or_provider_sdk(self):
        # A registry that imported one would be a registry built for that one. The translation
        # into any vendor's function-calling format belongs in the orchestrator.
        banned = ("anthropic", "openai", "app.ai.provider", "app.speech", "ollama")
        for path in TOOLS_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            for name in banned:
                assert not re.search(rf"^\s*(?:import|from)\s+{re.escape(name)}\b", source, re.MULTILINE), (
                    f"{path.name} imports {name}"
                )

    def test_no_tool_module_composes_sql(self):
        # The model may request a capability; it may not reach the database. Tools call
        # domain services, and the one direct query is a scoped ORM filter.
        for path in TOOLS_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert "text(" not in source, f"{path.name} may be composing SQL"
            assert ".execute(" not in source, f"{path.name} executes statements directly"
            assert not re.search(r"\bSELECT\b.*\bFROM\b", source, re.IGNORECASE), f"{path.name} contains SQL"

    def test_the_registry_does_not_import_the_voice_transport(self):
        # VA-4 must not wire itself into the VA-2 gateway. The seam is documented, not built.
        for path in TOOLS_DIR.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            assert not re.search(r"^\s*(?:import|from)\s+app\.voice\b", source, re.MULTILINE), path.name
            assert not re.search(r"^\s*(?:import|from)\s+app\.api\.voice_session\b", source, re.MULTILINE), path.name


class TestOutputBounds:
    def test_every_list_output_carries_a_truncation_flag(self):
        # A bounded list that could not say it was cut off would be presented as complete.
        for built in read_only_specs():
            fields = built.output_model.model_fields
            has_list = any(
                getattr(field.annotation, "__origin__", None) is list for field in fields.values()
            )
            if has_list and built.max_items > 1:
                assert "truncated" in fields, f"{built.name} returns a list without a truncated flag"

    def test_the_result_size_ceiling_is_enforced(self):
        class Fat(ToolOutput):
            blob: str

        registry = ToolRegistry()
        registry.register(
            spec(
                name="fat.read",
                output_model=Fat,
                handler=lambda ctx, args: Fat(blob="x" * (MAX_RESULT_CHARACTERS + 1)),
            )
        )
        # A bounded schema bounds the shape, not the bytes: a list of strings is still
        # unbounded in size, so the registry needs its own ceiling.
        from app.tools.contracts import ToolExecutionFailed

        class FakeContext:
            user = type("U", (), {"id": 1})()

            class cancellation:  # noqa: N801 - a stand-in, not a class being designed
                @staticmethod
                def raise_if_cancelled(*_args):
                    return None

        with pytest.raises(ToolExecutionFailed) as caught:
            registry.invoke("fat.read", None, FakeContext())
        assert "ceiling" in str(caught.value)


class TestTheVoiceProtocolIsUnchanged:
    """VA-4 defines the orchestrator seam. It must not have switched anything on.

    The VA-2 client already *parses* `tool_call_started` -- it is in the Kotlin decoder, and so
    in `SERVER_MESSAGE_TYPES`. What matters is that the gateway still does not emit it, because
    the moment it does, the app is claiming an orchestration that does not exist.
    """

    def test_tool_call_started_is_parseable_by_the_client(self):
        from app.voice import realtime_protocol

        assert "tool_call_started" in realtime_protocol.SERVER_MESSAGE_TYPES

    def test_but_the_gateway_still_emits_nothing_of_the_kind(self):
        """Updated for the realtime integration, and deliberately not relaxed.

        The gateway now runs real turns and emits the ten types a turn genuinely produces. The
        thing this guard exists to protect is unchanged: **`tool_call_started` is still not one
        of them**, and neither is `operation_outcome`. A turn's coach responder really does run
        read tools, which is exactly why the temptation existed -- but the fan-out completes
        inside one synchronous responder call, so a "started" frame would announce work that had
        already finished, and this path executes no confirmable ACTION tool at all.

        Pinned by the forbidden set rather than by the whole allowlist, so adding a justified
        turn event does not require editing a tool-registry test -- while adding a tool or action
        event still does.
        """
        from app.voice import realtime_protocol

        assert "tool_call_started" in realtime_protocol.FORBIDDEN_SERVER_MESSAGE_TYPES
        assert "operation_outcome" in realtime_protocol.FORBIDDEN_SERVER_MESSAGE_TYPES
        assert "tool_call_started" not in realtime_protocol.SUPPORTED_SERVER_MESSAGE_TYPES
        assert "operation_outcome" not in realtime_protocol.SUPPORTED_SERVER_MESSAGE_TYPES

    def test_the_gateway_does_not_import_the_registry(self):
        """Still true after the realtime integration, and now worth more than it was.

        The gateway runs real conversations, so it had every reason to reach for a tool
        catalogue. It does not: it names a mode and asks `app/voice/responders.py` for a
        responder bound to the authenticated user. One file builds the catalogue, which is one
        grep away from being auditable -- and `app/voice/turn_runtime.py` cannot reach a tool at
        all, which is what keeps the runtime unable to authorize its own access.
        """
        source = (TOOLS_DIR.parent / "api" / "voice_session.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+app\.tools\b", source, re.MULTILINE)
        assert not re.search(r"^\s*(?:import|from)\s+app\.agent\b", source, re.MULTILINE)

        runtime = (TOOLS_DIR.parent / "voice" / "turn_runtime.py").read_text(encoding="utf-8")
        for forbidden in (r"app\.tools", r"app\.agent", r"app\.services"):
            assert not re.search(rf"^\s*(?:import|from)\s+{forbidden}\b", runtime, re.MULTILINE), (
                "the turn runtime must take its responder by injection, never import one"
            )
