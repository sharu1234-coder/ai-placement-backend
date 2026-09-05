import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.assessment import AnswerGrade, GeneratedQuestion
from app.ai.schemas.placement_path import GeneratedPlacementPathExplanations, GeneratedTopicExplanation
from app.main import app


class FakeAIProvider(AIProvider):
    """Branches on schema, mirroring test_roadmap.py's precedent for a fixture that must
    serve more than one AI-schema type across a single flow (explanations at generation
    time, then questions/grading through the existing assessment endpoints)."""

    def __init__(self, fail_explanations: bool = False):
        self.fail_explanations = fail_explanations

    def generate_structured(self, prompt, schema):
        if schema is GeneratedPlacementPathExplanations:
            if self.fail_explanations:
                raise AIGenerationError("simulated AI failure")
            return GeneratedPlacementPathExplanations(
                explanations=[GeneratedTopicExplanation(topic_key="sql_basics", why_it_matters="Core SQL skill.")]
            )
        if schema is GeneratedQuestion:
            return GeneratedQuestion(
                type="mcq",
                prompt="What does SQL stand for?",
                options=["Structured Query Language", "Simple Query Logic", "Sequential Query List", "Standard Q"],
                expected_answer_or_rubric="Structured Query Language",
                explanation="SQL stands for Structured Query Language.",
            )
        if schema is AnswerGrade:
            return AnswerGrade(is_correct=True, score=1.0, feedback="Correct.")
        raise AssertionError(f"Unexpected schema requested: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "path@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "path@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def use_failing_ai():
    provider = FakeAIProvider(fail_explanations=True)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


def _insert_resume(client, email, skills):
    from app.api.deps import get_db
    from app.models.profile import StudentProfile
    from app.models.resume import Resume
    from app.models.resume_analysis import ResumeAnalysis
    from app.models.user import User

    db = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == email).first()
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if profile is None:
        profile = StudentProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        db.refresh(profile)

    resume = Resume(profile_id=profile.id, file_path="x.pdf", raw_text="text", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)

    analysis = ResumeAnalysis(
        resume_id=resume.id,
        extracted_skills=skills,
        extracted_education=[],
        extracted_experience=[],
        extracted_projects=[],
        extracted_certifications=[],
        ats_score=70.0,
        weak_sections=[],
        extraction_confidence=0.9,
    )
    db.add(analysis)
    db.commit()
    db.close()


@pytest.fixture()
def resume_with_skills(client, auth_headers):
    _insert_resume(client, "path@example.com", ["SQL", "Python", "Cooking", "Leadership"])


def test_generate_requires_completed_resume(client, auth_headers, use_fake_ai):
    resp = client.post("/placement-path/generate", headers=auth_headers)
    assert resp.status_code == 422


def test_generate_requires_placement_relevant_skills(client, auth_headers, use_fake_ai):
    _insert_resume(client, "path@example.com", ["Cooking", "Photography", "Leadership"])
    resp = client.post("/placement-path/generate", headers=auth_headers)
    assert resp.status_code == 422


def test_generate_returns_structured_path(client, auth_headers, resume_with_skills, use_fake_ai):
    resp = client.post("/placement-path/generate", headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "active"
    assert body["total_topics"] > 0
    assert body["completed_topics"] == 0
    stage_keys = {s["stage"] for s in body["stages"]}
    # SQL is a domain track (matched) -- role_specific is out of scope for v1, so it must
    # never appear.
    assert "role_specific" not in stage_keys
    assert body["next_best_action"] is not None


def test_generate_excludes_irrelevant_resume_skills(client, auth_headers, resume_with_skills, use_fake_ai):
    resp = client.post("/placement-path/generate", headers=auth_headers)
    body = resp.json()
    all_titles = [t["title"] for s in body["stages"] for t in s["topics"]]
    assert not any("Cooking" in t or "Leadership" in t for t in all_titles)


def test_generate_only_includes_matched_domain_tracks(client, auth_headers, resume_with_skills, use_fake_ai):
    resp = client.post("/placement-path/generate", headers=auth_headers)
    body = resp.json()
    all_track_keys = {t["track_key"] for s in body["stages"] for t in s["topics"]}
    assert "sql" in all_track_keys
    assert "ml_data_science" not in all_track_keys  # never evidenced on this resume


def test_generate_ai_failure_still_succeeds_with_fallback_text(client, auth_headers, resume_with_skills, use_failing_ai):
    resp = client.post("/placement-path/generate", headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    all_topics = [t for s in body["stages"] for t in s["topics"]]
    assert all(t["why_it_matters"] for t in all_topics)


def test_get_current_null_before_generation(client, auth_headers):
    resp = client.get("/placement-path/current", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_current_populated_after_generation(client, auth_headers, resume_with_skills, use_fake_ai):
    client.post("/placement-path/generate", headers=auth_headers)
    resp = client.get("/placement-path/current", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["total_topics"] > 0


def _first_topic(body, track_key):
    return next(t for s in body["stages"] for t in s["topics"] if t["track_key"] == track_key)


def test_first_topic_in_track_is_never_locked(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "sql")
    assert topic["status"] != "locked"


def test_second_topic_in_track_is_locked(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    sql_topics = [t for s in body["stages"] for t in s["topics"] if t["track_key"] == "sql"]
    filtering_topic = next(t for t in sql_topics if t["title"] == "SQL Filtering")
    assert filtering_topic["status"] == "locked"


def test_start_topic_transitions_to_in_progress(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "sql")
    resp = client.post(f"/placement-path/topics/{topic['id']}/start", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"


def test_start_locked_topic_returns_409(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    sql_topics = [t for s in body["stages"] for t in s["topics"] if t["track_key"] == "sql"]
    locked_topic = next(t for t in sql_topics if t["status"] == "locked")
    resp = client.post(f"/placement-path/topics/{locked_topic['id']}/start", headers=auth_headers)
    assert resp.status_code == 409


def test_practice_returns_blueprint_id_and_is_idempotent(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "sql")
    first = client.post(f"/placement-path/topics/{topic['id']}/practice", headers=auth_headers)
    assert first.status_code == 200
    second = client.post(f"/placement-path/topics/{topic['id']}/practice", headers=auth_headers)
    assert second.json()["blueprint_id"] == first.json()["blueprint_id"]


def test_practice_uses_topics_own_granular_skill_tag(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "sql")
    assert topic["title"] == "SQL Basics"
    assert topic["skill_tag"] == "SQL Basics"  # not just the broad "SQL"

    practice = client.post(f"/placement-path/topics/{topic['id']}/practice", headers=auth_headers).json()
    assessment = client.post(
        "/assessments", json={"blueprint_id": practice["blueprint_id"]}, headers=auth_headers
    ).json()
    next_q = client.get(f"/assessments/{assessment['id']}/next-question", headers=auth_headers).json()
    assert next_q["question"]["skill_tag"] == "SQL Basics"


def test_manual_complete_rejected_when_topic_has_practice_path(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "sql")
    resp = client.patch(f"/placement-path/topics/{topic['id']}", json={"status": "completed"}, headers=auth_headers)
    assert resp.status_code == 422


def test_manual_complete_accepted_for_checkpoint_stage_topic(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "interview_prep")
    resp = client.patch(f"/placement-path/topics/{topic['id']}", json={"status": "completed"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["completed_via"] == "manual"


def test_topic_completes_via_passing_assessment(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "sql")
    practice = client.post(f"/placement-path/topics/{topic['id']}/practice", headers=auth_headers).json()
    blueprint_id = practice["blueprint_id"]

    assessment = client.post("/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers).json()
    assessment_id = assessment["id"]

    # PRACTICE_QUESTION_COUNT questions to answer, then one more next-question call to let
    # the assessment engine notice every section is satisfied and flip status to "completed"
    # (get_next_question only makes that transition on the call *after* the last answer).
    for _ in range(4):
        next_q = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": next_q["question"]["id"], "student_answer": "Structured Query Language"},
            headers=auth_headers,
        )
    client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)

    current = client.get("/placement-path/current", headers=auth_headers).json()
    updated_topic = _first_topic(current, "sql")
    assert updated_topic["status"] == "completed"
    assert updated_topic["completed_via"] == "assessment"


def test_regenerate_preserves_completed_progress(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic = _first_topic(body, "interview_prep")
    client.patch(f"/placement-path/topics/{topic['id']}", json={"status": "completed"}, headers=auth_headers)

    regenerated = client.post("/placement-path/generate", headers=auth_headers).json()
    assert regenerated["id"] != body["id"]
    new_topic = _first_topic(regenerated, "interview_prep")
    assert new_topic["topic_key"] == topic["topic_key"]
    assert new_topic["status"] == "completed"


def test_history_empty_before_generation(client, auth_headers):
    resp = client.get("/placement-path/history", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_history_does_not_retroactively_reconcile_superseded_paths(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    # A superseded path is a frozen historical snapshot -- passing an assessment against a
    # blueprint carried forward to the new active path must not retroactively flip the old,
    # superseded topic's status too (only get_current_placement_path/the active path reconciles
    # live).
    first = client.post("/placement-path/generate", headers=auth_headers).json()
    old_topic = _first_topic(first, "sql")
    client.post(f"/placement-path/topics/{old_topic['id']}/practice", headers=auth_headers)

    second = client.post("/placement-path/generate", headers=auth_headers).json()
    new_topic = _first_topic(second, "sql")
    practice = client.post(f"/placement-path/topics/{new_topic['id']}/practice", headers=auth_headers).json()
    blueprint_id = practice["blueprint_id"]

    assessment = client.post("/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers).json()
    assessment_id = assessment["id"]
    for _ in range(4):
        next_q = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": next_q["question"]["id"], "student_answer": "Structured Query Language"},
            headers=auth_headers,
        )
    client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)

    current = client.get("/placement-path/current", headers=auth_headers).json()
    assert _first_topic(current, "sql")["status"] == "completed"

    history = client.get("/placement-path/history", headers=auth_headers).json()
    superseded_entry = next(p for p in history if p["id"] == first["id"])
    assert _first_topic(superseded_entry, "sql")["status"] != "completed"


def test_history_includes_superseded_and_active_most_recent_first(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    first = client.post("/placement-path/generate", headers=auth_headers).json()
    second = client.post("/placement-path/generate", headers=auth_headers).json()

    resp = client.get("/placement-path/history", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == [second["id"], first["id"]]
    assert {p["status"] for p in body} == {"active", "superseded"}
    assert all(p["created_at"] for p in body)


def test_history_requires_auth(client):
    resp = client.get("/placement-path/history")
    assert resp.status_code == 401


def test_topic_isolated_per_user(client, auth_headers, resume_with_skills, use_fake_ai):
    body = client.post("/placement-path/generate", headers=auth_headers).json()
    topic_id = _first_topic(body, "sql")["id"]

    client.post("/auth/signup", json={"email": "other-path@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-path@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.post(f"/placement-path/topics/{topic_id}/start", headers=other_headers)
    assert resp.status_code == 404
