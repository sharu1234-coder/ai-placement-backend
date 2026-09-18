"""Phase 3: what external evidence may and may not do to a skill's state.

The rule this file exists to hold: authorised external evidence can support a skill, and can
widen the base under one, but can never on its own make it VERIFIED. That is not enforced by a
convention anyone has to remember -- ExternalEvidenceItem has no score column and the matcher
emits no score, while `verified` requires a source scored at or above the strong threshold. The
tests below pin the behaviour so a future change that adds a score has to argue with them.

The second rule is the topic distinction. A repository language is GitHub's measurement of the
bytes of code; a repository topic is a label the owner typed. Treating the second as independent
evidence would let a student promote a skill by labelling an empty repository.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.external_evidence_item import (
    SIGNAL_REPO_PRIMARY_LANGUAGE,
    SIGNAL_REPO_TOPIC,
    ExternalEvidenceItem,
)
from app.models.profile import StudentProfile
from app.models.project import Project
from app.models.provider_connection import PROVIDER_GITHUB, ProviderConnection
from app.models.user import User
from app.services import evidence_engine

_RECENT = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5)


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


def _connection(db, profile_id, revoked_at=None) -> ProviderConnection:
    row = ProviderConnection(
        profile_id=profile_id,
        provider=PROVIDER_GITHUB,
        provider_user_id="4242",
        provider_username="priya",
        revoked_at=revoked_at,
    )
    db.add(row)
    db.commit()
    return row


def _evidence(db, connection_id, skill, signal_kind=SIGNAL_REPO_PRIMARY_LANGUAGE, ref="github:repo:priya/a"):
    db.add(
        ExternalEvidenceItem(
            connection_id=connection_id,
            skill=skill,
            signal_kind=signal_kind,
            source_ref=ref,
            observed_at=_RECENT,
            detail={"repository": "priya/a"},
        )
    )
    db.commit()


def _scored_assessment(db, profile_id, skill, score):
    blueprint = AssessmentBlueprint(profile_id=profile_id, source="default_taxonomy", sections=[])
    db.add(blueprint)
    db.flush()
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.flush()
    db.add(
        AssessmentResult(
            assessment_id=assessment.id,
            overall_score=score,
            skill_scores={skill: score},
            priority_weaknesses=[],
            created_at=_RECENT,
        )
    )
    db.commit()


class TestExternalEvidenceCounts:
    def test_a_repository_language_supports_a_skill(self, db_session, profile):
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Python")

        assert view.state == "supported"
        assert [item.source for item in view.items] == ["external"]

    def test_a_skill_with_nothing_external_is_unaffected(self, db_session, profile):
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Rust")

        assert view.state == "claimed"
        assert view.items == []

    def test_a_measured_language_is_not_self_asserted(self, db_session, profile):
        """GitHub measured the bytes; the student typed nothing."""
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")

        item = evidence_engine.get_skill_evidence(db_session, profile.id, "Python").items[0]
        assert item.self_asserted is False

    def test_a_topic_is_self_asserted(self, db_session, profile):
        """Anyone can put a topic on an empty repository in seconds."""
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Kubernetes", signal_kind=SIGNAL_REPO_TOPIC)

        item = evidence_engine.get_skill_evidence(db_session, profile.id, "Kubernetes").items[0]
        assert item.self_asserted is True

    def test_a_measured_language_is_preferred_over_a_topic(self, db_session, profile):
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python", signal_kind=SIGNAL_REPO_TOPIC, ref="github:repo:priya/x")
        _evidence(db_session, connection.id, "Python", ref="github:repo:priya/y")

        item = evidence_engine.get_skill_evidence(db_session, profile.id, "Python").items[0]
        assert item.self_asserted is False


class TestExternalEvidenceCannotVerify:
    def test_external_evidence_alone_never_reaches_verified(self, db_session, profile):
        """Even with several repositories, there is no scored source, so `verified` is
        unreachable -- and that is the point."""
        connection = _connection(db_session, profile.id)
        for index in range(5):
            _evidence(db_session, connection.id, "Python", ref=f"github:repo:priya/r{index}")

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Python")

        assert view.state != "verified"
        assert all(item.score is None for item in view.items)

    def test_a_topic_alone_cannot_reach_strong(self, db_session, profile):
        """`strong` needs breadth AND an independent source. A typed label is neither enough
        on its own nor independent, so labelling cannot promote a skill."""
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Kubernetes", signal_kind=SIGNAL_REPO_TOPIC)
        db_session.add(Project(profile_id=profile.id, title="K8s thing", description="Kubernetes work"))
        db_session.commit()

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Kubernetes")

        assert view.state not in ("strong", "verified")

    def test_external_evidence_adds_breadth_to_an_already_scored_skill(self, db_session, profile):
        """The legitimate contribution: it widens the base under evidence that was already
        independently scored, which is the existing ladder working as designed."""
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")
        _scored_assessment(db_session, profile.id, "Python", 82.0)

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Python")

        sources = {item.source for item in view.items}
        assert "external" in sources and "assessment" in sources
        assert view.state == "strong"


class TestOwnershipAndLifecycle:
    def test_one_students_repositories_never_count_for_another(self, db_session, profile):
        other_user = User(email="arjun@example.com", password_hash="x")
        db_session.add(other_user)
        db_session.flush()
        other = StudentProfile(user_id=other_user.id)
        db_session.add(other)
        db_session.commit()
        connection = _connection(db_session, other.id)
        _evidence(db_session, connection.id, "Python")

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Python")

        assert view.state == "claimed"
        assert view.items == []

    def test_disconnecting_stops_the_evidence_counting_immediately(self, db_session, profile):
        """Ownership is filtered in SQL on the live connection, so a revoked grant stops
        counting without needing a cleanup pass to have run."""
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")
        assert evidence_engine.get_skill_evidence(db_session, profile.id, "Python").state == "supported"

        connection.revoked_at = datetime.datetime.now(datetime.timezone.utc)
        db_session.commit()

        assert evidence_engine.get_skill_evidence(db_session, profile.id, "Python").state == "claimed"

    def test_last_verified_at_reflects_the_push_time(self, db_session, profile):
        """A measured language is independent evidence, so it may date a verification --
        unlike a resume keyword, which may not."""
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Python")

        assert view.last_verified_at is not None

    def test_a_topic_does_not_date_a_verification(self, db_session, profile):
        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Kubernetes", signal_kind=SIGNAL_REPO_TOPIC)

        view = evidence_engine.get_skill_evidence(db_session, profile.id, "Kubernetes")

        assert view.last_verified_at is None


class TestBatchFormStaysFlat:
    def test_the_batch_form_does_not_query_per_skill(self, db_session, profile):
        """The existing N+1 guarantee must survive a seventh source being added."""
        from sqlalchemy import event

        connection = _connection(db_session, profile.id)
        _evidence(db_session, connection.id, "Python")

        counts = {"n": 0}

        def _count(*_args, **_kwargs):
            counts["n"] += 1

        event.listen(db_session.get_bind(), "before_cursor_execute", _count)
        try:
            evidence_engine.get_all_skill_evidence(db_session, profile.id, ["Python", "SQL", "Go"])
            few = counts["n"]
            counts["n"] = 0
            evidence_engine.get_all_skill_evidence(
                db_session, profile.id, ["Python", "SQL", "Go", "Rust", "Java", "C", "Ruby", "PHP"]
            )
            many = counts["n"]
        finally:
            event.remove(db_session.get_bind(), "before_cursor_execute", _count)

        # The guarantee is that the count does not GROW with the number of skills -- the sources
        # are fetched once and matched in memory. It is not required to be identical: the first
        # call of a session does a little one-off work the second does not, so pinning equality
        # would make this test fail for a reason that has nothing to do with N+1.
        assert many <= few, f"query count grew with skill count: {few} -> {many}"
