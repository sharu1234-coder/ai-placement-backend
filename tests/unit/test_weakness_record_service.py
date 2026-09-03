import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.weakness_record import WeaknessRecord
from app.services import notification_service
from app.services import weakness_record_service as svc

PROFILE_ID = 1
SKILL = "interview:technical_correctness"

WEAK = 0.3  # below INTERVIEW_DIMENSION_THRESHOLD (0.6)
GOOD = 0.8  # above threshold


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_a_good_score_with_no_history_creates_nothing(db):
    result = svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    assert result is None
    assert db.query(WeaknessRecord).count() == 0


def test_first_weak_score_creates_a_NEW_record(db):
    record = svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    assert record.status == "NEW"
    assert record.occurrences == 1
    assert record.consecutive_good_signals == 0
    assert record.last_score == WEAK


def test_second_weak_score_advances_to_REPEATED(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    record = svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    assert record.status == "REPEATED"
    assert record.occurrences == 2


def test_single_good_answer_is_not_enough_to_close(db):
    """The spec's own explicit callout: one correct answer must not resolve a weakness."""
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    record = svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    assert record.status == "IMPROVING"
    assert record.status != "CLOSED"


def test_two_consecutive_good_answers_reach_NEARLY_CLOSED_not_CLOSED(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    record = svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    assert record.status == "NEARLY_CLOSED"


def test_three_consecutive_good_answers_reach_CLOSED(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    record = svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    assert record.status == "CLOSED"


def test_a_weak_signal_after_improving_resets_the_streak_and_regresses_to_REPEATED(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # IMPROVING
    record = svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    assert record.status == "REPEATED"
    assert record.consecutive_good_signals == 0
    assert record.occurrences == 2


def test_a_weak_signal_after_CLOSED_reopens_it(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # CLOSED
    record = svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    assert record.status == "REPEATED"
    assert record.consecutive_good_signals == 0


def test_different_skills_are_tracked_independently(db):
    svc.record_or_update(db, PROFILE_ID, "interview:clarity", WEAK)
    svc.record_or_update(db, PROFILE_ID, "interview:structure", WEAK)
    records = svc.list_for_profile(db, PROFILE_ID)
    assert {r.skill_key for r in records} == {"interview:clarity", "interview:structure"}


def test_different_profiles_are_isolated(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, 2, SKILL, WEAK)
    assert len(svc.list_for_profile(db, PROFILE_ID)) == 1
    assert len(svc.list_for_profile(db, 2)) == 1


def test_first_good_signal_notifies_progress(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    pending_before = notification_service.get_pending(db, PROFILE_ID)
    assert pending_before == []

    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # IMPROVING

    pending = notification_service.get_pending(db, PROFILE_ID)
    assert len(pending) == 1
    assert pending[0].kind == "weakness_improved"
    assert "Technical correctness" in pending[0].title


def test_staying_at_nearly_closed_does_not_re_notify(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # IMPROVING -- notifies
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # NEARLY_CLOSED -- not a notable status

    pending = notification_service.get_pending(db, PROFILE_ID)
    assert len(pending) == 1  # only the IMPROVING transition


def test_reaching_closed_notifies_again(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # IMPROVING
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # NEARLY_CLOSED
    svc.record_or_update(db, PROFILE_ID, SKILL, GOOD)  # CLOSED

    pending = notification_service.get_pending(db, PROFILE_ID)
    assert [p.kind for p in pending] == ["weakness_improved", "weakness_improved"]
    assert "closed out" in pending[-1].body.lower()


def test_a_new_weak_record_never_notifies(db):
    svc.record_or_update(db, PROFILE_ID, SKILL, WEAK)  # NEW -- not a notable status
    assert notification_service.get_pending(db, PROFILE_ID) == []


def test_list_for_profile_orders_open_before_closed(db):
    svc.record_or_update(db, PROFILE_ID, "interview:clarity", WEAK)
    svc.record_or_update(db, PROFILE_ID, "interview:clarity", GOOD)
    svc.record_or_update(db, PROFILE_ID, "interview:clarity", GOOD)
    svc.record_or_update(db, PROFILE_ID, "interview:clarity", GOOD)  # CLOSED

    svc.record_or_update(db, PROFILE_ID, "interview:structure", WEAK)  # NEW, still open

    records = svc.list_for_profile(db, PROFILE_ID)
    assert records[0].skill_key == "interview:structure"
    assert records[-1].skill_key == "interview:clarity"
