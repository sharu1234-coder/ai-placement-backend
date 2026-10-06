"""Phase 30: two overlapping answers to the same recruiter-attack turn store exactly one.

answer_turn is already correct -- it writes through an atomic conditional UPDATE whose WHERE
clause requires the turn's answer to still be NULL, and raises when no row was flipped. This file
exists because nothing proved that clause was load-bearing.

Phase 30's M11 deleted `answer_text.is_(None)` from that WHERE clause, and the recruiter-attack
unit suite passed all 33 tests. The reason is worth recording, because it is the general shape of
this kind of gap rather than an oversight about one endpoint: `answer_turn` reads the turn and
raises before the UPDATE if it is already answered, so a *sequential* duplicate is refused by
that read and never reaches the clause under test. Only an overlapping pair reaches it -- both
requests pass the read, and without the clause the loser's UPDATE silently overwrites the
winner's stored answer instead of being refused. Every test was sequential, so the clause could
be deleted without a single failure.

See tests/fixtures/replay_harness.py for why the database is a real file and why the requests
are released together at the read.
"""

import pytest

from app.database.base import Base
from app.main import app
from app.models.recruiter_attack_session import RecruiterAttackSession
from app.models.recruiter_attack_turn import RecruiterAttackTurn
from tests.fixtures import replay_harness as h


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "recruiter_answer_replay.db")
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def client_and_sessions(engine, tmp_path, monkeypatch):
    client, sessions = h.make_client(engine, tmp_path, monkeypatch)
    with client as test_client:
        yield test_client, sessions
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _disarm():
    yield
    h.disarm_gate()


def _seed(db, profile_id: int) -> tuple[int, int]:
    """A session with one pending turn -- seeded directly, because start_session needs a
    completed project build or an analysed resume and none of that is what is under test here."""
    session = RecruiterAttackSession(
        profile_id=profile_id,
        source_type="resume_claim",
        source_label="Built a payments service",
        status="in_progress",
    )
    db.add(session)
    db.commit()
    turn = RecruiterAttackTurn(
        session_id=session.id,
        order_index=1,
        question_text="Which part did you write yourself?",
        based_on="Built a payments service",
    )
    db.add(turn)
    db.commit()
    return session.id, turn.id


def test_sequential_replay_of_an_answer_is_refused_and_keeps_the_first(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    session_id, turn_id = _seed(db, h.profile_id(db))
    url = f"/recruiter-attack/sessions/{session_id}/answer"

    first = client.post(url, headers=headers, json={"turn_id": turn_id, "answer_text": "I wrote the ledger."})
    second = client.post(url, headers=headers, json={"turn_id": turn_id, "answer_text": "Something else."})

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    db.expire_all()
    assert db.get(RecruiterAttackTurn, turn_id).answer_text == "I wrote the ledger."


def test_overlapping_conflicting_answers_to_one_turn_store_exactly_one(client_and_sessions):
    """Two overlapping requests carrying *different* answers.

    Phase 30 wrote this with the same text in both requests, which stopped proving the guard once
    Phase 31 began reconciling an identical resend: with matching text the stored row is the same
    whether the guard refuses the loser or lets it overwrite the winner, so both run green either
    way. Differing text is what makes the loser's fate observable -- it must be refused, and the
    winner's answer must survive. (The identical-text overlap is now covered as a *replay* in
    tests/api/test_answer_result_replay.py, where both callers correctly succeed.)
    """
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    session_id, turn_id = _seed(db, h.profile_id(db))

    responses = h.post_together(
        client,
        f"/recruiter-attack/sessions/{session_id}/answer",
        headers,
        # The UPDATE itself, which is the only seam that reaches the clause under test. A
        # thread arrives here having already passed its own "is this turn still unanswered?"
        # read, and no thread can commit while the barrier holds them, so holding both here
        # forces exactly the interleaving where the WHERE clause decides the winner. Gating the
        # ownership read instead (the first attempt) let one request finish before the other read
        # the turn, so the second was refused by that read and the test passed with the guard
        # deleted -- M11 reported MISSED twice before this line was right.
        gate="UPDATE recruiter_attack_turns",
        bodies=(
            {"turn_id": turn_id, "answer_text": "I wrote the ledger."},
            {"turn_id": turn_id, "answer_text": "Someone else wrote it."},
        ),
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 409], (
        f"expected one stored answer and one refusal, got {codes}: "
        f"{[r.text for r in responses]}"
    )
    db.expire_all()
    turn = db.get(RecruiterAttackTurn, turn_id)
    winner = next(r for r in responses if r.status_code == 200)
    assert turn.answer_text == winner.json()["answer_text"], (
        "the refused request's answer was stored anyway -- the loser overwrote the winner"
    )
    assert turn.answer_text in ("I wrote the ledger.", "Someone else wrote it.")
    assert db.query(RecruiterAttackTurn).filter(RecruiterAttackTurn.session_id == session_id).count() == 1
