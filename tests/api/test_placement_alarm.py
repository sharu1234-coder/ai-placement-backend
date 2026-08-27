import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.assessment import GeneratedQuestion
from app.main import app

SAMPLE_GENERATED_MCQ = GeneratedQuestion(
    type="mcq",
    prompt="What does SQL stand for?",
    options=[
        "Structured Query Language",
        "Simple Query Logic",
        "Sequential Query List",
        "Standard Question Language",
    ],
    expected_answer_or_rubric="Structured Query Language",
    explanation="SQL stands for Structured Query Language.",
)


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        assert schema is GeneratedQuestion
        return SAMPLE_GENERATED_MCQ

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post(
        "/auth/signup", json={"email": "alarm@example.com", "password": "supersecret123"}
    )
    token = client.post(
        "/auth/login", json={"email": "alarm@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


class FlakyAIProvider(AIProvider):
    """Fails on the Nth call to generate_structured (1-indexed) -- simulates a real AI
    provider failing partway through a 6-question challenge generation."""

    def __init__(self, fail_on_call: int):
        self.fail_on_call = fail_on_call
        self.calls = 0

    def generate_structured(self, prompt, schema):
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise AIGenerationError("simulated AI failure")
        return SAMPLE_GENERATED_MCQ

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def resume_with_skills(client, auth_headers):
    """Bypasses real PDF parsing/AI resume analysis (out of scope here) by inserting a
    completed Resume + ResumeAnalysis row directly, mirroring what a real upload would
    produce -- same technique other test modules use when the AI-parsing path itself isn't
    under test."""
    from app.api.deps import get_db
    from app.models.profile import StudentProfile
    from app.models.resume import Resume
    from app.models.resume_analysis import ResumeAnalysis
    from app.models.user import User

    db = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == "alarm@example.com").first()
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
        extracted_skills=["SQL", "Python", "Machine Learning"],
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


def test_eligibility_false_with_no_resume(client, auth_headers):
    resp = client.get("/placement-alarm/eligibility", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["eligible"] is False


def test_eligibility_true_with_completed_resume(client, auth_headers, resume_with_skills):
    resp = client.get("/placement-alarm/eligibility", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["eligible"] is True


def test_start_challenge_requires_completed_resume(client, auth_headers):
    resp = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    )
    assert resp.status_code == 422


def test_start_challenge_generates_exactly_six_questions(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    resp = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "in_progress"
    assert body["completed_count"] == 0
    assert body["current_question"]["index"] == 0
    assert len(body["current_question"]["options"]) == 4
    # Answer key must never reach the client before answering.
    assert "expected_answer_or_rubric" not in body["current_question"]


def test_start_challenge_is_idempotent_for_same_client_alarm_id(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    first = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()
    second = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()
    assert first["challenge_id"] == second["challenge_id"]
    assert first["current_question"]["id"] == second["current_question"]["id"]


def test_start_challenge_ai_failure_returns_503_not_500(client, auth_headers, resume_with_skills):
    flaky = FlakyAIProvider(fail_on_call=3)
    app.dependency_overrides[get_ai_provider] = lambda: flaky
    try:
        resp = client.post(
            "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
        )
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)
    assert resp.status_code == 503


def test_start_challenge_ai_failure_leaves_a_clean_retryable_state(
    client, auth_headers, resume_with_skills
):
    # Regression: a mid-generation AI failure used to leave a permanently stuck in_progress
    # challenge that every retry (matched by client_alarm_id) would keep returning as-is,
    # forever short of its required 6 questions. The broken challenge must instead be rolled
    # back so a retry starts clean and can actually succeed.
    flaky = FlakyAIProvider(fail_on_call=3)
    app.dependency_overrides[get_ai_provider] = lambda: flaky
    try:
        failed = client.post(
            "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
        )
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)
    assert failed.status_code == 503

    app.dependency_overrides[get_ai_provider] = lambda: FakeAIProvider()
    try:
        retried = client.post(
            "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
        )
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)
    assert retried.status_code == 201
    assert retried.json()["completed_count"] == 0
    assert retried.json()["current_question"]["index"] == 0


def test_wrong_answer_does_not_advance_and_reveals_answer(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    challenge = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()
    challenge_id = challenge["challenge_id"]
    question_id = challenge["current_question"]["id"]

    resp = client.post(
        f"/placement-alarm/challenges/{challenge_id}/answer",
        json={"question_id": question_id, "student_answer": "Simple Query Logic"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_correct"] is False
    assert body["completed_count"] == 0
    assert body["challenge_completed"] is False
    assert body["correct_answer"] == "Structured Query Language"
    assert body["explanation"]

    # Still on the same question -- retry, not skip.
    state = client.get(f"/placement-alarm/challenges/{challenge_id}", headers=auth_headers).json()
    assert state["current_question"]["id"] == question_id


def test_correct_answer_after_retry_advances_progress(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    challenge = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()
    challenge_id = challenge["challenge_id"]
    question_id = challenge["current_question"]["id"]

    client.post(
        f"/placement-alarm/challenges/{challenge_id}/answer",
        json={"question_id": question_id, "student_answer": "wrong"},
        headers=auth_headers,
    )
    resp = client.post(
        f"/placement-alarm/challenges/{challenge_id}/answer",
        json={"question_id": question_id, "student_answer": "Structured Query Language"},
        headers=auth_headers,
    )
    body = resp.json()
    assert body["is_correct"] is True
    assert body["correct_answer"] is None
    assert body["explanation"] is None
    assert body["completed_count"] == 1
    assert body["challenge_completed"] is False


def test_already_correct_question_cannot_be_resubmitted(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    challenge = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()
    challenge_id = challenge["challenge_id"]
    question_id = challenge["current_question"]["id"]
    client.post(
        f"/placement-alarm/challenges/{challenge_id}/answer",
        json={"question_id": question_id, "student_answer": "Structured Query Language"},
        headers=auth_headers,
    )

    resp = client.post(
        f"/placement-alarm/challenges/{challenge_id}/answer",
        json={"question_id": question_id, "student_answer": "Structured Query Language"},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_full_challenge_completes_at_six_of_six(
    client, auth_headers, resume_with_skills, use_fake_ai
):
    challenge = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()
    challenge_id = challenge["challenge_id"]

    completed = False
    for _ in range(6):
        state = client.get(f"/placement-alarm/challenges/{challenge_id}", headers=auth_headers).json()
        question_id = state["current_question"]["id"]
        resp = client.post(
            f"/placement-alarm/challenges/{challenge_id}/answer",
            json={"question_id": question_id, "student_answer": "Structured Query Language"},
            headers=auth_headers,
        )
        body = resp.json()
        completed = body["challenge_completed"]

    assert completed is True
    final_state = client.get(f"/placement-alarm/challenges/{challenge_id}", headers=auth_headers).json()
    assert final_state["status"] == "completed"
    assert final_state["completed_count"] == 6
    assert final_state["current_question"] is None


def test_challenge_isolated_per_user(client, auth_headers, resume_with_skills, use_fake_ai):
    challenge_id = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=auth_headers
    ).json()["challenge_id"]

    client.post("/auth/signup", json={"email": "other-alarm@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-alarm@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.get(f"/placement-alarm/challenges/{challenge_id}", headers=other_headers)
    assert resp.status_code == 404
