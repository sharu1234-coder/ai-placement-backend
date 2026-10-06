"""Phase 30: replaying "update my Career Path using this JD" must not add the skills twice.

apply_jd reads the path's existing skill items, decides which of the job description's
requirements are not represented yet, and inserts those. The read and the insert are a
check-then-act, and nothing in the database stops a second insert: career_path_skill_items has
an index on career_path_id but no unique constraint, so two overlapping requests can both decide
the same requirement is missing and both add it. The generated skill_key is deterministic
(`jd_<jd>_<req>`), so the duplicates are not even distinguishable rows -- they are the same skill
listed twice in the student's plan.

A sequential replay is already safe, and these tests prove that too: by the time a resend is
sent, the first request's items are committed and `skill_present` skips every one of them, so
`added` is zero and the function does not even commit. What is not safe is a resend that overlaps
the original, which is the case the transport produces -- OkHttp cannot tell "the connection was
already dead" from "the request arrived and the reply was lost".

See tests/fixtures/replay_harness.py for why the database is a real file and why the threads are
released at the INSERT.
"""

import pytest

from app.models.career_path import CareerPath
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.job_description import JobDescription
from app.models.requirement import Requirement
from tests.fixtures import replay_harness as h


@pytest.fixture()
def engine(tmp_path):
    from app.database.base import Base

    eng = h.make_engine(tmp_path / "apply_jd_replay.db")
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def client_and_sessions(engine, tmp_path, monkeypatch):
    from app.main import app

    client, sessions = h.make_client(engine, tmp_path, monkeypatch)
    with client as test_client:
        yield test_client, sessions
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _disarm():
    yield
    h.disarm_gate()


def _seed(db, profile_id: int) -> tuple[int, int]:
    """A career path with one existing skill, and a JD asking for two more."""
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
    db.add(
        CareerPathSkillItem(
            career_path_id=path.id,
            phase_key="core_skills",
            skill_key="sql",
            title="SQL",
            order_index=0,
        )
    )
    jd = JobDescription(profile_id=profile_id, raw_text="We need Airflow and dbt.", role_title="Analyst")
    db.add(jd)
    db.commit()
    db.add_all(
        [
            Requirement(jd_id=jd.id, category="skill", value="Airflow"),
            Requirement(jd_id=jd.id, category="tool", value="dbt"),
        ]
    )
    db.commit()
    return path.id, jd.id


def _titles(db, path_id: int) -> list[str]:
    return sorted(
        item.title
        for item in db.query(CareerPathSkillItem).filter(CareerPathSkillItem.career_path_id == path_id).all()
    )


def test_sequential_replay_of_apply_jd_adds_each_skill_once(client_and_sessions):
    """The control: the existing title check already handles this case, and must keep doing so."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    path_id, jd_id = _seed(db, h.profile_id(db))

    url = f"/career-path/{path_id}/apply-jd/{jd_id}"

    first = client.post(url, headers=headers)
    second = client.post(url, headers=headers)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    db.expire_all()
    assert _titles(db, path_id) == ["Airflow", "SQL", "dbt"], "a sequential replay duplicated a skill"


def test_overlapping_replay_of_apply_jd_adds_each_skill_once(client_and_sessions):
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    path_id, jd_id = _seed(db, h.profile_id(db))

    responses = h.post_together(
        client,
        f"/career-path/{path_id}/apply-jd/{jd_id}",
        headers,
        # Released at the ownership read, which is the last point *before* the claim that both
        # requests always reach. Gating any later would be gating inside the mechanism under test:
        # once the claim holds the second request, it never reaches the merge's own statements at
        # all, so a gate on those would time out and both requests would fail with a broken
        # barrier rather than being measured. (That is not hypothetical -- it is what the first
        # version of this test did once the fix was in.)
        gate="FROM career_paths",
    )

    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    db.expire_all()
    titles = _titles(db, path_id)
    assert titles == ["Airflow", "SQL", "dbt"], (
        f"an overlapping replay left {titles}: the same skill is listed twice in the student's "
        f"career path, with the same generated skill_key, and nothing removes it"
    )
    # Same logical result for both, not merely one surviving mutation: the request that lost the
    # claim is answered with the path the winner produced, so neither student sees an error and
    # neither sees a half-applied plan.
    assert responses[0].json() == responses[1].json()


def test_two_different_job_descriptions_both_apply(client_and_sessions):
    """The collapse must be per resource, not per path. Applying a second, different job
    description to the same career path is a different operation and must still add its skills --
    a claim keyed on the path alone would silently swallow it."""
    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    path_id, first_jd = _seed(db, h.profile_id(db))

    second = JobDescription(profile_id=h.profile_id(db), raw_text="We need Kafka.", role_title="Engineer")
    db.add(second)
    db.commit()
    db.add(Requirement(jd_id=second.id, category="skill", value="Kafka"))
    db.commit()

    assert client.post(f"/career-path/{path_id}/apply-jd/{first_jd}", headers=headers).status_code == 200
    assert client.post(f"/career-path/{path_id}/apply-jd/{second.id}", headers=headers).status_code == 200

    db.expire_all()
    assert _titles(db, path_id) == ["Airflow", "Kafka", "SQL", "dbt"], (
        "the second job description's skills were not applied: the claim is collapsing two "
        "different operations into one"
    )


def test_a_claim_that_never_frees_is_a_conflict_not_a_server_error(client_and_sessions, monkeypatch):
    """The claim normally resolves by waiting -- the merge is a few queries with no model call in
    it. But run_business_once gives up after BUSINESS_WAIT (60s) and raises, and before Phase 30
    this route had no `except idempotency.IdempotencyError`, so that would have surfaced as a 500.
    Patched rather than waited out, because the alternative is a sixty-second test."""
    from app.services import idempotency

    client, sessions = client_and_sessions
    headers = h.signup(client)
    db = sessions()
    path_id, jd_id = _seed(db, h.profile_id(db))

    def never_frees(*_args, **_kwargs):
        raise idempotency.OperationInProgressError("That request is still being processed.")

    monkeypatch.setattr(idempotency, "run_business_once", never_frees)

    response = client.post(f"/career-path/{path_id}/apply-jd/{jd_id}", headers=headers)
    assert response.status_code == 409, response.text
