"""Findings 4 and 6: transcript metadata, and who owns the database session.

Both are about the same file (`app/api/voice_session.py`) and both were invisible defects
rather than crashes, which is why they are pinned here with explicit reasoning.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.base import Base
from app.models.conversation import Conversation, ConversationTurn
from app.services import conversation_history

VOICE_SESSION = Path(__file__).resolve().parents[2] / "app" / "api" / "voice_session.py"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _conversation(db, *, kind="career_coach", interview_id=None):
    conversation = Conversation(
        profile_id=1,
        kind=kind,
        session_key="voice-test",
        status="active",
        interview_id=interview_id,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


class TestTranscriptQuestionLink:
    """Finding 4: mock-interview turns were stored without the question they answered.

    `append_turn` has accepted `interview_question_id` the whole time. Neither call site in
    `voice_session.py` passed it, so the column existed, the parameter existed, and every
    voice-interview transcript was still unlinked -- a defect that nothing fails on, because
    the default is simply None.
    """

    def test_a_turn_records_the_question_it_answers(self, db):
        conversation = _conversation(db, kind="interview", interview_id=7)
        conversation_history.append_turn(
            db,
            conversation=conversation,
            role="user",
            text="I would use a hash map.",
            turn_id="t1",
            interview_question_id=42,
        )
        db.commit()

        turn = db.execute(select(ConversationTurn)).scalars().one()
        assert turn.interview_question_id == 42

    def test_a_coach_turn_records_no_question(self, db):
        conversation = _conversation(db)
        conversation_history.append_turn(
            db,
            conversation=conversation,
            role="user",
            text="How do I prepare?",
            turn_id="t1",
        )
        db.commit()

        turn = db.execute(select(ConversationTurn)).scalars().one()
        assert turn.interview_question_id is None

    def test_the_gateway_actually_passes_the_question(self):
        """The guard for the real defect.

        The bug was not that the plumbing was wrong -- it was that the argument was never
        supplied. A behavioural test on `append_turn` alone would have passed throughout, so
        this asserts the call sites, which is where the omission lived.
        """
        source = VOICE_SESSION.read_text(encoding="utf-8")
        calls = re.findall(r"conversation_history\.append_turn\((.*?)\n            \)", source, re.S)
        assert len(calls) == 2, "expected the user and assistant append_turn calls, found %d" % len(calls)
        for call in calls:
            assert "interview_question_id=" in call, (
                "an append_turn call in voice_session.py does not pass interview_question_id, "
                "so interview transcripts would again be stored unlinked to their question"
            )

    def test_interview_metadata_is_gated_on_the_mode(self):
        """Finding 4's other half: a career-coach socket must not acquire interview metadata.

        The query parameter is accepted regardless of mode, so passing the connection value
        through unconditionally would let `?interview_question_id=` tag a coach conversation.
        """
        source = VOICE_SESSION.read_text(encoding="utf-8")
        assert "if selected is VoiceMode.INTERVIEW else None" in source, (
            "the interview_question_id carried on the connection must be gated on the mode"
        )


class TestWorkerSessionLifetime:
    """Finding 6: the request-scoped session outlives its request only by accident.

    `_Connection.db` is the session from `Depends(get_db)` -- its own docstring calls it "the
    one Session that serves this whole socket". Turn workers are `asyncio.create_task` tasks,
    and the endpoint's `finally` deliberately leaves them running ("The worker is left to
    finish: it may be blocked inside a recogniser that cannot be interrupted"). When the
    endpoint returns, FastAPI closes the `get_db` dependency.

    So a worker that comes back after the socket closed calls `commit()` on a closed session.
    This is confirmed structurally and at the session level below. It is **not** fixed here:
    moving workers onto their own session changes session ownership across the whole voice
    path, and is recorded as an open finding rather than attempted in passing.
    """

    def test_a_write_after_the_request_ended_succeeds_in_a_new_transaction(self, db):
        """Measured, and it corrects the premise this finding was raised on.

        The expectation was that a worker writing through the closed request session would
        fail and lose the student's words. It does not. `get_db` ends with `db.close()`, which
        returns the connection to the pool but leaves the `Session` fully usable -- the next
        use transparently checks out another connection and begins a new transaction.

        So the late write *succeeds*. That makes this a transaction-boundary and
        connection-pool concern, not the data-loss defect it was reported as:

        * the turn is committed outside the request's transaction, so it is no longer atomic
          with anything else that request did, and
        * a task nobody awaits checks a connection out of the pool, which under load is how a
          pool gets exhausted by work that has no client left to serve.

        Pinned as the real behaviour so the severity stays honest and a future change that
        *does* make this raise has to confront the difference.
        """
        conversation = _conversation(db)
        conversation_id = conversation.id
        db.close()

        conversation_history.append_turn(
            db,
            conversation=db.get(Conversation, conversation_id),
            role="assistant",
            text="late answer",
            turn_id="late",
        )
        db.commit()

        stored = db.execute(select(ConversationTurn)).scalars().all()
        assert len(stored) == 1, (
            "a closed Session is still usable, so the late worker's write lands rather than "
            "being lost -- which is why this finding is not the data-loss bug it was filed as"
        )

    def test_the_worker_is_documented_as_outliving_the_request(self):
        """Pins the two facts that together make the race reachable, so that a future change
        to either one has to confront this test."""
        source = VOICE_SESSION.read_text(encoding="utf-8")
        assert "db: Session = Depends(get_db)" in source, "the session is request-scoped"
        assert "connection.db.commit()" in source, "workers commit through that session"
        assert "The worker is left to finish" in source, (
            "the endpoint deliberately returns while workers may still be running"
        )
