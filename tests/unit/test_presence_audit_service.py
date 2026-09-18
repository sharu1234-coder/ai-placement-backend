"""Phase 3: what the audit observes, and what it refuses to let the model say.

Two halves, tested separately because they fail differently. The observation half is pure
deterministic reading of a stored snapshot -- its failure mode is saying something untrue about
the student's repositories. The narrative half takes model output -- its failure mode is letting
an invented observation through, which is the one this module exists to prevent.

Every generated-output test drives the real validator with structured output a hostile or
hallucinating model could genuinely return, never with prompt text, because the guarantee has to
hold regardless of what the prompt says.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.ai.provider import AIGenerationError
from app.ai.schemas.professional_presence import (
    GeneratedObservationText,
    GeneratedPresenceNarrative,
)
from app.database.base import Base
from app.models.external_profile_snapshot import ExternalProfileSnapshot
from app.models.profile import StudentProfile
from app.models.skill import Skill
from app.models.user import User
from app.services.professional_presence import audit_service


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def profile(db_session):
    user = User(email="priya@example.com", password_hash="x")
    db_session.add(user)
    db_session.flush()
    row = StudentProfile(user_id=user.id)
    db_session.add(row)
    db_session.commit()
    return row


_RECENT = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)


def _repo(name="bus-tracker", **overrides):
    base = dict(
        full_name=f"priya/{name}",
        description="Live arrival board",
        primary_language="Python",
        languages=["Python"],
        topics=["transit"],
        is_fork=False,
        is_archived=False,
        pushed_at=_RECENT.isoformat(),
    )
    base.update(overrides)
    return base


def _snapshot(repos, profile_overrides=None):
    profile_payload = {
        "username": "priya",
        "name": "Priya R",
        "bio": "Building data tools",
        "public_repos": len(repos),
    }
    profile_payload.update(profile_overrides or {})
    return ExternalProfileSnapshot(
        connection_id=1,
        sequence=1,
        fingerprint="f",
        payload={"profile": profile_payload, "repositories": repos},
    )


def _codes(observations):
    return [o.code for o in observations]


class TestProfileObservations:
    def test_a_missing_bio_is_observed(self, db_session, profile):
        snapshot = _snapshot([_repo()], {"bio": None})
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_NO_BIO in codes

    def test_a_missing_display_name_is_observed(self, db_session, profile):
        snapshot = _snapshot([_repo()], {"name": None})
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_NO_DISPLAY_NAME in codes

    def test_a_complete_profile_raises_neither(self, db_session, profile):
        codes = _codes(audit_service.observations_for(db_session, profile.id, _snapshot([_repo()])))
        assert audit_service.CODE_NO_BIO not in codes
        assert audit_service.CODE_NO_DISPLAY_NAME not in codes


class TestRepositoryObservations:
    def test_missing_descriptions_are_counted_and_named(self, db_session, profile):
        snapshot = _snapshot([_repo("a", description=None), _repo("b", description=None), _repo("c")])
        finding = next(
            o
            for o in audit_service.observations_for(db_session, profile.id, snapshot)
            if o.code == audit_service.CODE_REPOS_WITHOUT_DESCRIPTION
        )
        # The evidence sentence must carry the real counts, because it is what makes the
        # recommendation traceable rather than generic.
        assert "2 of your 3" in finding.evidence

    def test_missing_topics_are_observed(self, db_session, profile):
        snapshot = _snapshot([_repo("a", topics=[])])
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_REPOS_WITHOUT_TOPICS in codes

    def test_forks_are_not_counted_against_presentation(self, db_session, profile):
        """Telling a student to describe somebody else's project is bad advice."""
        snapshot = _snapshot([_repo("mine"), _repo("theirs", description=None, is_fork=True)])
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_REPOS_WITHOUT_DESCRIPTION not in codes

    def test_a_mostly_forked_account_is_observed(self, db_session, profile):
        snapshot = _snapshot(
            [_repo("a", is_fork=True), _repo("b", is_fork=True), _repo("c", is_fork=True), _repo("d")]
        )
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_MOSTLY_FORKS in codes

    def test_one_fork_out_of_one_repository_is_not_a_pattern(self, db_session, profile):
        snapshot = _snapshot([_repo("a", is_fork=True)])
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_MOSTLY_FORKS not in codes

    def test_long_abandoned_work_is_observed(self, db_session, profile):
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=400)).isoformat()
        snapshot = _snapshot([_repo("a", pushed_at=old)])
        codes = _codes(audit_service.observations_for(db_session, profile.id, snapshot))
        assert audit_service.CODE_WORK_IS_STALE in codes

    def test_recent_work_is_not_called_stale(self, db_session, profile):
        codes = _codes(audit_service.observations_for(db_session, profile.id, _snapshot([_repo()])))
        assert audit_service.CODE_WORK_IS_STALE not in codes

    def test_an_empty_account_says_so_once(self, db_session, profile):
        """With nothing to look at, the only honest observation is that there is nothing."""
        observations = audit_service.observations_for(db_session, profile.id, _snapshot([]))
        assert _codes(observations) == [audit_service.CODE_NO_PUBLIC_REPOS]


class TestStrengths:
    def test_a_measured_language_becomes_a_strength(self, db_session, profile):
        snapshot = _snapshot([_repo("a"), _repo("b")])
        strength = next(
            o
            for o in audit_service.observations_for(db_session, profile.id, snapshot)
            if o.code == audit_service.CODE_LANGUAGE_STRENGTH
        )
        assert strength.subject == "Python"
        assert strength.kind == audit_service.KIND_STRENGTH
        assert "2 of your public repositories" in strength.evidence

    def test_a_topic_alone_is_never_a_strength(self, db_session, profile):
        """A label the student typed is not proof they can do the thing."""
        snapshot = _snapshot([_repo("a", primary_language=None, languages=[], topics=["kubernetes"])])
        subjects = [
            o.subject
            for o in audit_service.observations_for(db_session, profile.id, snapshot)
            if o.code == audit_service.CODE_LANGUAGE_STRENGTH
        ]
        assert "kubernetes" not in subjects


class TestClaimGaps:
    def test_a_declared_skill_with_no_public_trace_is_a_gap(self, db_session, profile):
        db_session.add(Skill(profile_id=profile.id, name="Kubernetes"))
        db_session.commit()
        snapshot = _snapshot([_repo("a")])

        gap = next(
            o
            for o in audit_service.observations_for(db_session, profile.id, snapshot)
            if o.code == audit_service.CODE_SKILL_WITHOUT_EVIDENCE
        )
        assert gap.subject == "Kubernetes"
        assert gap.kind == audit_service.KIND_GAP

    def test_a_declared_skill_the_code_shows_is_not_a_gap(self, db_session, profile):
        db_session.add(Skill(profile_id=profile.id, name="Python"))
        db_session.commit()
        snapshot = _snapshot([_repo("a")])

        subjects = [
            o.subject
            for o in audit_service.observations_for(db_session, profile.id, snapshot)
            if o.code == audit_service.CODE_SKILL_WITHOUT_EVIDENCE
        ]
        assert "Python" not in subjects

    def test_no_repositories_means_no_claim_gaps_at_all(self, db_session, profile):
        """Otherwise every declared skill becomes a gap and the screen says nothing that the
        "no public repositories" observation has not already said."""
        db_session.add(Skill(profile_id=profile.id, name="Kubernetes"))
        db_session.commit()

        codes = _codes(audit_service.observations_for(db_session, profile.id, _snapshot([])))
        assert audit_service.CODE_SKILL_WITHOUT_EVIDENCE not in codes


class _Provider:
    def __init__(self, result):
        self.result = result

    def generate_structured(self, prompt, schema):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def generate_text(self, prompt):
        return ""


def _observation(code=audit_service.CODE_NO_BIO, subject=None):
    return audit_service.AuditObservation(
        code=code, kind=audit_service.KIND_OPPORTUNITY, subject=subject,
        evidence="evidence sentence", fallback_action="template action",
    )


class TestNarrativeGrounding:
    def test_prose_for_a_real_observation_is_kept(self):
        obs = [_observation()]
        provider = _Provider(
            GeneratedPresenceNarrative(
                items=[
                    GeneratedObservationText(
                        code=audit_service.CODE_NO_BIO,
                        why_it_matters="Recruiters skim the bio first.",
                        recommended_action="Write one line naming the work you want.",
                    )
                ]
            )
        )
        narrative = audit_service.generate_narrative(obs, provider)
        assert len(narrative) == 1

    def test_an_invented_observation_code_is_dropped(self):
        """The core guarantee: the model may phrase an observation, never introduce one."""
        provider = _Provider(
            GeneratedPresenceNarrative(
                items=[
                    GeneratedObservationText(
                        code="your_code_quality_is_poor",
                        why_it_matters="Made up entirely.",
                        recommended_action="Do something.",
                    )
                ]
            )
        )
        assert audit_service.generate_narrative([_observation()], provider) == {}

    def test_a_real_code_with_an_invented_subject_is_dropped(self):
        """Half-right is still fabricated: a gap about a skill the student never declared."""
        obs = [_observation(audit_service.CODE_SKILL_WITHOUT_EVIDENCE, "Kubernetes")]
        provider = _Provider(
            GeneratedPresenceNarrative(
                items=[
                    GeneratedObservationText(
                        code=audit_service.CODE_SKILL_WITHOUT_EVIDENCE,
                        subject="Rust",
                        why_it_matters="...",
                        recommended_action="...",
                    )
                ]
            )
        )
        assert audit_service.generate_narrative(obs, provider) == {}

    def test_subject_case_does_not_lose_a_usable_sentence(self):
        obs = [_observation(audit_service.CODE_SKILL_WITHOUT_EVIDENCE, "Kubernetes")]
        provider = _Provider(
            GeneratedPresenceNarrative(
                items=[
                    GeneratedObservationText(
                        code=audit_service.CODE_SKILL_WITHOUT_EVIDENCE,
                        subject="kubernetes",
                        why_it_matters="Nothing public shows it yet.",
                        recommended_action="Publish something small using it.",
                    )
                ]
            )
        )
        assert len(audit_service.generate_narrative(obs, provider)) == 1

    def test_a_second_entry_for_one_observation_is_ignored(self):
        obs = [_observation()]
        provider = _Provider(
            GeneratedPresenceNarrative(
                items=[
                    GeneratedObservationText(
                        code=audit_service.CODE_NO_BIO, why_it_matters="First.", recommended_action="A."
                    ),
                    GeneratedObservationText(
                        code=audit_service.CODE_NO_BIO, why_it_matters="Second.", recommended_action="B."
                    ),
                ]
            )
        )
        narrative = audit_service.generate_narrative(obs, provider)
        assert len(narrative) == 1
        assert narrative[audit_service._narrative_key(audit_service.CODE_NO_BIO, None)]["why_it_matters"] == "First."

    def test_blank_prose_is_dropped_rather_than_shown(self):
        provider = _Provider(
            GeneratedPresenceNarrative(
                items=[
                    GeneratedObservationText(
                        code=audit_service.CODE_NO_BIO, why_it_matters="   ", recommended_action="   "
                    )
                ]
            )
        )
        assert audit_service.generate_narrative([_observation()], provider) == {}

    def test_an_ai_failure_yields_no_narrative_and_does_not_raise(self):
        provider = _Provider(AIGenerationError("provider down"))
        assert audit_service.generate_narrative([_observation()], provider) == {}

    def test_no_observations_means_no_ai_call_at_all(self):
        class _Exploding:
            def generate_structured(self, prompt, schema):
                raise AssertionError("must not be called with nothing to write about")

            def generate_text(self, prompt):
                return ""

        assert audit_service.generate_narrative([], _Exploding()) == {}


class TestAssembly:
    def test_an_observation_the_model_ignored_still_reaches_the_student(self):
        findings = audit_service.assemble([_observation()], {})
        assert len(findings) == 1
        assert findings[0].recommended_action == "template action"
        assert findings[0].is_template is True
        assert findings[0].why_it_matters is None

    def test_generated_prose_replaces_the_template(self):
        obs = [_observation()]
        narrative = {
            audit_service._narrative_key(audit_service.CODE_NO_BIO, None): {
                "why_it_matters": "Because recruiters read it.",
                "recommended_action": "Write one line.",
            }
        }
        finding = audit_service.assemble(obs, narrative)[0]
        assert finding.is_template is False
        assert finding.recommended_action == "Write one line."

    def test_evidence_is_never_replaced_by_generated_text(self):
        """The proof half must stay deterministic however good the prose is."""
        obs = [_observation()]
        narrative = {
            audit_service._narrative_key(audit_service.CODE_NO_BIO, None): {
                "why_it_matters": "x",
                "recommended_action": "y",
            }
        }
        assert audit_service.assemble(obs, narrative)[0].evidence == "evidence sentence"
