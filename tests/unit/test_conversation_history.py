"""Durability and ownership for voice transcripts.

The requirement these tests exist for is "conversations survive the app closing and the server
restarting". That cannot be demonstrated by writing and reading in one session -- a SQLAlchemy
identity map will return the object it already holds whether or not anything reached the disk. So
the durability tests here dispose the engine, build a new one against the same file, and read with
a session that has never seen the rows.

A file-backed SQLite database rather than conftest's in-memory one, for the reason
tests/unit/test_business_claim.py gives: that fixture uses StaticPool, so every session shares one
connection and a "restart" would prove nothing. These also run against PostgreSQL when
TEST_DATABASE_URL is set.
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.database.base import Base
from app.models.conversation import Conversation, ConversationTurn
from app.models.profile import StudentProfile
from app.models.user import User
from app.services import conversation_history as history
from tests import db_safety

# Supplied by tests/db_safety.py, never read from the environment here: that module
# refuses a target it cannot prove is disposable, and it does so at import time -- before
# any fixture in this file can drop a table.
_TEST_DATABASE_URL = db_safety.resolve_test_database_url()


@pytest.fixture()
def db_file(tmp_path):
    """A database that genuinely lives somewhere, so a restart can be simulated."""
    if _TEST_DATABASE_URL:
        return _TEST_DATABASE_URL
    # Windows-style path: a sqlite URL needs `sqlite:///C:/...`, not a POSIX-looking one.
    return "sqlite:///" + str(tmp_path / "history.db").replace("\\", "/")


def _engine(url):
    eng = create_engine(url, poolclass=NullPool)
    return eng


@pytest.fixture()
def engine(db_file):
    eng = _engine(db_file)
    db_safety.drop_all_safely(eng)
    Base.metadata.create_all(bind=eng)
    yield eng
    db_safety.drop_all_safely(eng)
    eng.dispose()


@pytest.fixture()
def session(engine):
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    db = maker()
    yield db
    db.close()


def _profile(db) -> StudentProfile:
    user = User(email="p%s@example.com" % os.urandom(4).hex(), password_hash="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    profile = StudentProfile(user_id=user.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _coach(db, profile, key="sess-1"):
    return history.ensure_conversation(
        db, profile_id=profile.id, kind="career_coach", session_key=key
    )


class TestItIsSavedAsItHappens:
    def test_a_turn_is_stored_when_it_completes_not_when_the_session_ends(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile)

        history.append_turn(
            session, conversation=conversation, role="user", text="How do I prepare?",
            turn_id="t-1",
        )
        session.commit()

        # Still `active`: nothing ended, and the turn is already on disk.
        assert conversation.status == "active"
        turns = history.get_turns(session, conversation_id=conversation.id)
        assert [t.text for t in turns] == ["How do I prepare?"]

    def test_both_sides_of_one_turn_are_stored(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile)

        history.append_turn(
            session, conversation=conversation, role="user", text="What should I study?",
            turn_id="t-9",
        )
        history.append_turn(
            session, conversation=conversation, role="assistant", text="Start with SQL.",
            turn_id="t-9",
        )
        session.commit()

        turns = history.get_turns(session, conversation_id=conversation.id)
        assert [(t.role, t.seq) for t in turns] == [("user", 1), ("assistant", 2)]

    def test_an_empty_transcript_is_not_stored_as_a_turn(self, session) -> None:
        # A turn the student never produced must not appear in their record, and must not be
        # counted by a final report that says how many questions they answered.
        profile = _profile(session)
        conversation = _coach(session, profile)

        for blank in ("", "   ", "\n"):
            assert history.append_turn(
                session, conversation=conversation, role="user", text=blank, turn_id="t-blank",
            ) is None
        session.commit()
        assert history.get_turns(session, conversation_id=conversation.id) == []


class TestItSurvivesARestart:
    def test_turns_are_readable_from_a_session_that_never_saw_them(self, engine, db_file) -> None:
        maker = sessionmaker(bind=engine, expire_on_commit=False)
        first = maker()
        profile = _profile(first)
        profile_id = profile.id
        conversation = _coach(first, profile, key="sess-restart")
        history.append_turn(
            first, conversation=conversation, role="user", text="Answer one.", turn_id="t-1"
        )
        history.append_turn(
            first, conversation=conversation, role="assistant", text="Good start.", turn_id="t-1"
        )
        first.commit()
        first.close()

        # The restart: a new engine, a new connection, a cold identity map.
        engine.dispose()
        reopened = _engine(db_file)
        try:
            db = sessionmaker(bind=reopened, expire_on_commit=False)()
            stored = history.list_conversations(db, profile_id=profile_id)
            assert len(stored) == 1
            turns = history.get_turns(db, conversation_id=stored[0].id)
            assert [t.text for t in turns] == ["Answer one.", "Good start."]
            db.close()
        finally:
            reopened.dispose()

    def test_an_interrupted_session_is_not_reported_as_completed(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile)
        history.append_turn(
            session, conversation=conversation, role="user", text="Half an answer",
            turn_id="t-1",
        )
        history.mark_ended(session, conversation=conversation, status="interrupted")
        session.commit()

        reread = history.get_conversation(
            session, profile_id=profile.id, conversation_id=conversation.id
        )
        assert reread is not None and reread.status == "interrupted"
        assert reread.ended_at is not None


class TestReconnectDoesNotDuplicate:
    def test_replaying_the_same_turn_stores_nothing_new(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile)

        first = history.append_turn(
            session, conversation=conversation, role="user", text="My answer.", turn_id="t-42"
        )
        session.commit()
        assert first is not None

        # The socket dropped and the client re-sent the same committed turn.
        replay = history.append_turn(
            session, conversation=conversation, role="user", text="My answer.", turn_id="t-42"
        )
        session.commit()

        assert replay is None, "a replay must be a no-op, not a second row"
        turns = history.get_turns(session, conversation_id=conversation.id)
        assert len(turns) == 1

    def test_reconnecting_with_the_same_session_key_resumes_one_conversation(
        self, session
    ) -> None:
        profile = _profile(session)
        opened = _coach(session, profile, key="sess-resume")
        history.append_turn(
            session, conversation=opened, role="user", text="Before the drop.", turn_id="t-1"
        )
        session.commit()

        resumed = _coach(session, profile, key="sess-resume")
        history.append_turn(
            session, conversation=resumed, role="user", text="After the drop.", turn_id="t-2"
        )
        session.commit()

        assert resumed.id == opened.id, "a reconnect must not open a parallel transcript"
        assert len(history.list_conversations(session, profile_id=profile.id)) == 1
        turns = history.get_turns(session, conversation_id=opened.id)
        assert [t.text for t in turns] == ["Before the drop.", "After the drop."]


class TestOwnership:
    def test_another_students_conversation_is_not_found(self, session) -> None:
        mine = _profile(session)
        theirs = _profile(session)
        hidden = _coach(session, theirs, key="sess-theirs")
        session.commit()

        assert history.get_conversation(
            session, profile_id=mine.id, conversation_id=hidden.id
        ) is None
        assert history.list_conversations(session, profile_id=mine.id) == []

    def test_another_students_conversation_cannot_be_deleted(self, session) -> None:
        mine = _profile(session)
        theirs = _profile(session)
        hidden = _coach(session, theirs, key="sess-theirs-2")
        session.commit()

        assert history.delete_conversation(
            session, profile_id=mine.id, conversation_id=hidden.id
        ) is False
        session.commit()
        # Still there for its real owner.
        assert history.get_conversation(
            session, profile_id=theirs.id, conversation_id=hidden.id
        ) is not None

    def test_a_session_key_belonging_to_someone_else_is_refused(self, session) -> None:
        theirs = _profile(session)
        mine = _profile(session)
        _coach(session, theirs, key="sess-shared")
        session.commit()

        with pytest.raises(PermissionError):
            history.ensure_conversation(
                session, profile_id=mine.id, kind="career_coach", session_key="sess-shared"
            )


class TestTheHistoryList:
    def test_both_kinds_appear_and_can_be_filtered(self, session) -> None:
        profile = _profile(session)
        history.ensure_conversation(
            session, profile_id=profile.id, kind="career_coach", session_key="c-1"
        )
        history.ensure_conversation(
            session, profile_id=profile.id, kind="mock_interview", session_key="i-1",
            mode="practice",
        )
        session.commit()

        everything = history.list_conversations(session, profile_id=profile.id)
        assert {c.kind for c in everything} == {"career_coach", "mock_interview"}
        assert len(history.list_conversations(
            session, profile_id=profile.id, kind="mock_interview"
        )) == 1

    def test_the_most_recently_active_conversation_comes_first(self, session) -> None:
        profile = _profile(session)
        older = _coach(session, profile, key="older")
        newer = _coach(session, profile, key="newer")
        session.commit()

        # Activity, not creation, is what orders the list: appending to the older conversation
        # must float it back to the top.
        history.append_turn(
            session, conversation=older, role="user", text="still going", turn_id="t-1"
        )
        session.commit()

        listed = history.list_conversations(session, profile_id=profile.id)
        assert listed[0].id == older.id
        assert {c.id for c in listed} == {older.id, newer.id}

    def test_pagination_does_not_repeat_or_skip_a_conversation(self, session) -> None:
        profile = _profile(session)
        for n in range(5):
            history.ensure_conversation(
                session, profile_id=profile.id, kind="career_coach", session_key="s-%d" % n
            )
        session.commit()

        first = history.list_conversations(session, profile_id=profile.id, limit=2, offset=0)
        second = history.list_conversations(session, profile_id=profile.id, limit=2, offset=2)
        third = history.list_conversations(session, profile_id=profile.id, limit=2, offset=4)

        seen = [c.id for c in list(first) + list(second) + list(third)]
        assert len(seen) == 5
        assert len(set(seen)) == 5, "a page boundary repeated a conversation"

    def test_a_page_is_capped_however_much_is_asked_for(self, session) -> None:
        profile = _profile(session)
        for n in range(3):
            history.ensure_conversation(
                session, profile_id=profile.id, kind="career_coach", session_key="cap-%d" % n
            )
        session.commit()
        assert len(history.list_conversations(
            session, profile_id=profile.id, limit=10_000
        )) == 3

    def test_an_unknown_kind_is_refused_rather_than_silently_returning_everything(
        self, session
    ) -> None:
        profile = _profile(session)
        with pytest.raises(ValueError):
            history.list_conversations(session, profile_id=profile.id, kind="not_a_kind")


class TestDeletion:
    def test_deleting_a_conversation_removes_its_turns(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile, key="to-delete")
        history.append_turn(
            session, conversation=conversation, role="user", text="private answer",
            turn_id="t-1",
        )
        session.commit()
        conversation_id = conversation.id

        assert history.delete_conversation(
            session, profile_id=profile.id, conversation_id=conversation_id
        ) is True
        session.commit()

        # No orphan rows: there is no database-level cascade on this foreign key.
        left = session.query(ConversationTurn).filter(
            ConversationTurn.conversation_id == conversation_id
        ).count()
        assert left == 0
        assert session.query(Conversation).filter(
            Conversation.id == conversation_id
        ).count() == 0


class TestTheClosedVocabularies:
    def test_an_unknown_kind_cannot_be_written(self, session) -> None:
        profile = _profile(session)
        with pytest.raises(ValueError):
            history.ensure_conversation(
                session, profile_id=profile.id, kind="chatbot", session_key="x"
            )

    def test_an_unknown_role_cannot_be_written(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile)
        with pytest.raises(ValueError):
            history.append_turn(
                session, conversation=conversation, role="system", text="hi", turn_id="t"
            )

    def test_an_unknown_status_cannot_be_written(self, session) -> None:
        profile = _profile(session)
        conversation = _coach(session, profile)
        with pytest.raises(ValueError):
            history.mark_ended(session, conversation=conversation, status="finished")
