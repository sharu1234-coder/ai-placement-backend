"""What "we have no evidence for that" is allowed to mean.

The distinction these tests hold: a source we never looked at is not a source that came back
empty. A student who has never connected GitHub has no external evidence rows -- exactly like a
student who connected it and writes no public code -- and only the second of those two is a fact
about the student. evidence_engine.unavailable_sources is what lets a caller tell them apart, so
a readiness number computed without GitHub can say which part it could not see rather than
letting the absence read as a finding.

The needs-reauth case is the one worth stating out loud: that connection is still live and its
evidence is still counted, so it is NOT unavailable. What it needs is a reconnect prompt, which
Professional Presence already owns.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.profile import StudentProfile
from app.models.provider_connection import (
    PROVIDER_GITHUB,
    STATUS_NEEDS_REAUTH,
    ProviderConnection,
)
from app.models.resume import Resume
from app.models.resume_analysis import ResumeAnalysis
from app.models.user import User
from app.services import evidence_engine


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()


def _profile(db, email="priya@example.com") -> StudentProfile:
    user = User(email=email, password_hash="x")
    db.add(user)
    db.flush()
    row = StudentProfile(user_id=user.id)
    db.add(row)
    db.commit()
    return row


def _connection(db, profile_id, *, revoked_at=None, status="active") -> ProviderConnection:
    row = ProviderConnection(
        profile_id=profile_id,
        provider=PROVIDER_GITHUB,
        provider_user_id="4242",
        provider_username="priya",
        status=status,
        revoked_at=revoked_at,
    )
    db.add(row)
    db.commit()
    return row


def _analysed_resume(db, profile_id, skills) -> None:
    resume = Resume(
        profile_id=profile_id,
        file_path="/tmp/cv.pdf",
        raw_text="cv",
        parse_status="completed",
    )
    db.add(resume)
    db.flush()
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=skills,
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=50.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()


def test_a_brand_new_student_has_both_sources_unavailable(db_session):
    profile = _profile(db_session)

    assert evidence_engine.unavailable_sources(db_session, profile.id) == [
        evidence_engine.SOURCE_EXTERNAL,
        evidence_engine.SOURCE_RESUME,
    ]


def test_a_connected_account_with_no_code_is_available_not_missing(db_session):
    """The case the whole module exists for: connected and empty is a real, reportable finding."""
    profile = _profile(db_session)
    _connection(db_session, profile.id)

    assert evidence_engine.SOURCE_EXTERNAL not in evidence_engine.unavailable_sources(db_session, profile.id)


def test_a_connection_needing_reauthorisation_still_counts_as_available(db_session):
    profile = _profile(db_session)
    _connection(db_session, profile.id, status=STATUS_NEEDS_REAUTH)

    assert evidence_engine.SOURCE_EXTERNAL not in evidence_engine.unavailable_sources(db_session, profile.id)


def test_a_revoked_connection_is_unavailable_again(db_session):
    import datetime

    profile = _profile(db_session)
    _connection(db_session, profile.id, revoked_at=datetime.datetime.now(datetime.timezone.utc))

    assert evidence_engine.SOURCE_EXTERNAL in evidence_engine.unavailable_sources(db_session, profile.id)


def test_an_analysed_resume_makes_the_resume_source_available(db_session):
    profile = _profile(db_session)
    _analysed_resume(db_session, profile.id, ["Python"])

    assert evidence_engine.SOURCE_RESUME not in evidence_engine.unavailable_sources(db_session, profile.id)


def test_a_parsed_resume_that_yielded_nothing_is_still_available(db_session):
    """We looked. Finding no skills on it is a finding, not a gap in our coverage."""
    profile = _profile(db_session)
    _analysed_resume(db_session, profile.id, [])

    assert evidence_engine.SOURCE_RESUME not in evidence_engine.unavailable_sources(db_session, profile.id)


def test_another_students_connection_does_not_make_this_one_available(db_session):
    mine = _profile(db_session)
    theirs = _profile(db_session, email="other@example.com")
    _connection(db_session, theirs.id)

    assert evidence_engine.SOURCE_EXTERNAL in evidence_engine.unavailable_sources(db_session, mine.id)
