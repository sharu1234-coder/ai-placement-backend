"""Phase 30: a replayed practice handoff must hand back the same assessment, not a second one.

Both Career Path and Placement Path offer a "Practice" button that get-or-creates a one-skill
AssessmentBlueprint and returns its id, which the client immediately posts to /assessments. The
get-or-create reads `practice_blueprint_id`, and if it is NULL inserts a blueprint, commits, then
writes the link in a *second* commit. Between those two commits the row still looks unused.

A sequential replay never lands there -- by the time OkHttp's resend is sent, the link is
committed and the same id comes back. What lands there is a resend that overlaps the original,
which is the case the transport actually produces: OkHttp cannot tell "the connection was already
dead" from "the request arrived and the reply was lost", so it re-sends while the first request
may still be running (the reasoning is written up in the Android app's NonResentRequests.kt).
Two requests in that window both see NULL, both insert, and the second link overwrites the first.

The cost is not a tidiness problem. Each request returns *its own* blueprint id and the client
starts an assessment against it, so the student can be sitting in an assessment that the topic no
longer points at: finishing it leaves the topic un-practised, because the completion path reads
the topic's link. The other blueprint is unreachable for good.

What makes these tests able to fail. The database is a real file with one connection per session,
never conftest's in-memory StaticPool -- that pool hands every session the SAME connection and
therefore the same transaction, so each request would see the other's uncommitted INSERT, and a
"concurrent" test would pass whatever the code did. (Measured, not assumed: this file's first
draft used StaticPool and reported the race as safe.) And the interleaving is made certain rather
than lucky by a barrier at the INSERT itself: a thread that has reached the insert has necessarily
already passed the "is it already linked?" read, so releasing both there is exactly the window.
"""

import pytest

from app.database.base import Base
from app.main import app
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.career_path import CareerPath
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.placement_path import PlacementPath
from app.models.placement_path_topic import PlacementPathTopic
from tests.fixtures import replay_harness as h


@pytest.fixture()
def engine(tmp_path):
    eng = h.make_engine(tmp_path / "practice_replay.db")
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


def _seed_skill_items(db, profile_id: int, titles=("SQL", "Python")) -> list[int]:
    path = CareerPath(
        profile_id=profile_id,
        role_id=None,
        custom_role="Data Analyst",
        role_family_key="custom:data-analyst",
        display_role_name="Data Analyst",
        is_primary=True,
        role_overview={},
    )
    db.add(path)
    db.commit()
    items = [
        CareerPathSkillItem(
            career_path_id=path.id,
            phase_key="core_skills",
            skill_key=title.lower(),
            title=title,
            order_index=index,
        )
        for index, title in enumerate(titles)
    ]
    db.add_all(items)
    db.commit()
    return [item.id for item in items]


def _seed_topic(db, profile_id: int) -> int:
    """The first topic of the SQL track: order_in_track=1, so it has no prerequisite and is
    unlocked, and its catalog spec carries assessment_type="sql" so practice is allowed."""
    path = PlacementPath(profile_id=profile_id, status="active")
    db.add(path)
    db.commit()
    topic = PlacementPathTopic(
        path_id=path.id,
        stage="core_technical",
        track_key="sql",
        topic_key="sql_basics",
        order_in_track=1,
        title="SQL Basics",
        skill_tag="SQL Basics",
    )
    db.add(topic)
    db.commit()
    return topic.id


def _blueprint_ids(db) -> list[int]:
    return [row.id for row in db.query(AssessmentBlueprint).order_by(AssessmentBlueprint.id).all()]


# ---------------------------------------------------------------------------
# Career Path
# ---------------------------------------------------------------------------


def test_sequential_replay_of_career_path_practice_returns_the_same_blueprint(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    item_id, _other = _seed_skill_items(db, h.profile_id(db))

    first = client.post(f"/career-path/skill-items/{item_id}/practice", headers=headers)
    second = client.post(f"/career-path/skill-items/{item_id}/practice", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["blueprint_id"] == second.json()["blueprint_id"]
    assert _blueprint_ids(db) == [first.json()["blueprint_id"]]


def test_overlapping_replay_of_career_path_practice_creates_one_blueprint(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    item_id, _other = _seed_skill_items(db, h.profile_id(db))

    responses = h.post_together(
        client,
        f"/career-path/skill-items/{item_id}/practice",
        headers,
        gate="INSERT INTO assessment_blueprints",
    )

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    returned = {r.json()["blueprint_id"] for r in responses}
    stored = _blueprint_ids(db)
    linked = db.query(CareerPathSkillItem).filter(CareerPathSkillItem.id == item_id).one().practice_blueprint_id

    assert len(stored) == 1, (
        f"an overlapping replay left {len(stored)} blueprints behind ({stored}); the skill item "
        f"links to {linked}, so the others are unreachable and an assessment started against one "
        f"of them can never mark this skill practised"
    )
    assert returned == {linked}, (
        f"the two requests were handed {returned} but the skill item links to {linked}: a student "
        f"on the losing side starts an assessment the skill item does not point at"
    )


def test_two_different_skill_items_still_get_their_own_blueprints(client_and_sessions):
    """The collapse must be per identity, not per endpoint -- practising SQL and practising
    Python are two operations and must stay two blueprints even back to back."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    first_item, second_item = _seed_skill_items(db, h.profile_id(db))

    first = client.post(f"/career-path/skill-items/{first_item}/practice", headers=headers)
    second = client.post(f"/career-path/skill-items/{second_item}/practice", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["blueprint_id"] != second.json()["blueprint_id"]
    assert len(_blueprint_ids(db)) == 2


# ---------------------------------------------------------------------------
# Placement Path -- independent code (Career Path must not depend on Roadmap's), same shape,
# so it needs its own proof rather than an argument by analogy.
# ---------------------------------------------------------------------------


def test_sequential_replay_of_placement_path_practice_returns_the_same_blueprint(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    topic_id = _seed_topic(db, h.profile_id(db))

    first = client.post(f"/placement-path/topics/{topic_id}/practice", headers=headers)
    second = client.post(f"/placement-path/topics/{topic_id}/practice", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["blueprint_id"] == second.json()["blueprint_id"]
    assert _blueprint_ids(db) == [first.json()["blueprint_id"]]


def test_overlapping_replay_of_placement_path_practice_creates_one_blueprint(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    topic_id = _seed_topic(db, h.profile_id(db))

    responses = h.post_together(
        client,
        f"/placement-path/topics/{topic_id}/practice",
        headers,
        gate="INSERT INTO assessment_blueprints",
    )

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    returned = {r.json()["blueprint_id"] for r in responses}
    stored = _blueprint_ids(db)
    linked = db.query(PlacementPathTopic).filter(PlacementPathTopic.id == topic_id).one().practice_blueprint_id

    assert len(stored) == 1, (
        f"an overlapping replay left {len(stored)} blueprints behind ({stored}); the topic links "
        f"to {linked}, so the others are unreachable and an assessment started against one of "
        f"them can never mark this topic practised"
    )
    assert returned == {linked}, (
        f"the two requests were handed {returned} but the topic links to {linked}: a student on "
        f"the losing side starts an assessment the topic does not point at"
    )
