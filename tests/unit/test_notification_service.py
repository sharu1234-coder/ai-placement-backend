import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.base import Base
from app.models.evidence_state_snapshot import EvidenceStateSnapshot
from app.services import notification_service as svc

PROFILE_ID = 1


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_enqueue_then_get_pending_returns_it_in_order(db):
    svc.enqueue(db, PROFILE_ID, "mission_completed", "Mission Complete!", "You did it.")
    svc.enqueue(db, PROFILE_ID, "readiness_milestone", "70% Ready!", "Nice.")
    db.commit()

    pending = svc.get_pending(db, PROFILE_ID)
    assert [p.kind for p in pending] == ["mission_completed", "readiness_milestone"]


def test_acknowledged_events_no_longer_appear_as_pending(db):
    svc.enqueue(db, PROFILE_ID, "mission_completed", "Mission Complete!", "You did it.")
    db.commit()
    event_id = svc.get_pending(db, PROFILE_ID)[0].id

    svc.acknowledge(db, PROFILE_ID, [event_id])

    assert svc.get_pending(db, PROFILE_ID) == []


def test_acknowledge_is_scoped_to_the_requesting_profile(db):
    svc.enqueue(db, PROFILE_ID, "mission_completed", "Mission Complete!", "You did it.")
    db.commit()
    event_id = svc.get_pending(db, PROFILE_ID)[0].id

    svc.acknowledge(db, profile_id=999, ids=[event_id])  # a different profile's ack

    assert len(svc.get_pending(db, PROFILE_ID)) == 1  # unaffected -- not owned by profile 999


def test_acknowledge_with_no_ids_is_a_safe_no_op(db):
    svc.acknowledge(db, PROFILE_ID, [])
    assert svc.get_pending(db, PROFILE_ID) == []


def test_check_readiness_milestone_fires_only_on_a_real_crossing(db):
    svc.check_readiness_milestone(db, PROFILE_ID, "Software Engineer", baseline_percent=40, new_percent=55)
    db.commit()
    pending = svc.get_pending(db, PROFILE_ID)
    assert len(pending) == 1
    assert "50%" in pending[0].title


def test_check_readiness_milestone_reports_only_the_highest_crossed(db):
    svc.check_readiness_milestone(db, PROFILE_ID, "Software Engineer", baseline_percent=10, new_percent=90)
    db.commit()
    pending = svc.get_pending(db, PROFILE_ID)
    assert len(pending) == 1
    assert "85%" in pending[0].title


def test_check_readiness_milestone_never_fires_with_no_baseline(db):
    svc.check_readiness_milestone(db, PROFILE_ID, "Software Engineer", baseline_percent=None, new_percent=90)
    db.commit()
    assert svc.get_pending(db, PROFILE_ID) == []


def test_check_readiness_milestone_does_not_fire_on_a_decrease(db):
    svc.check_readiness_milestone(db, PROFILE_ID, "Software Engineer", baseline_percent=80, new_percent=60)
    db.commit()
    assert svc.get_pending(db, PROFILE_ID) == []


def test_check_evidence_transitions_fires_on_a_crossing_into_strong(db):
    svc.check_evidence_transitions(db, PROFILE_ID, {"Python": "strong"})
    db.commit()
    pending = svc.get_pending(db, PROFILE_ID)
    assert len(pending) == 1
    assert pending[0].kind == "evidence_verified"
    assert db.query(EvidenceStateSnapshot).filter(EvidenceStateSnapshot.skill_key == "Python").one().state == "strong"


def test_check_evidence_transitions_does_not_refire_once_already_strong(db):
    svc.check_evidence_transitions(db, PROFILE_ID, {"Python": "strong"})
    db.commit()
    svc.check_evidence_transitions(db, PROFILE_ID, {"Python": "strong"})
    db.commit()
    assert len(svc.get_pending(db, PROFILE_ID)) == 1


def test_check_evidence_transitions_ignores_claimed_and_supported(db):
    svc.check_evidence_transitions(db, PROFILE_ID, {"Python": "claimed", "SQL": "supported"})
    db.commit()
    assert svc.get_pending(db, PROFILE_ID) == []


def test_check_evidence_transitions_regressing_from_strong_does_not_notify_and_updates_the_snapshot(db):
    svc.check_evidence_transitions(db, PROFILE_ID, {"Python": "strong"})
    db.commit()
    svc.check_evidence_transitions(db, PROFILE_ID, {"Python": "stale"})
    db.commit()

    assert len(svc.get_pending(db, PROFILE_ID)) == 1  # only the original "strong" notification
    assert db.query(EvidenceStateSnapshot).filter(EvidenceStateSnapshot.skill_key == "Python").one().state == "stale"
