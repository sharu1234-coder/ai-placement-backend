import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.assessment import AnswerGrade, GeneratedQuestion
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
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

SAMPLE_GENERATED_SHORT_ANSWER = GeneratedQuestion(
    type="short_answer",
    prompt="Explain what a primary key is.",
    options=None,
    expected_answer_or_rubric="A column that uniquely identifies each row in a table.",
    explanation="Primary keys enforce uniqueness and identify rows.",
)

SAMPLE_ANSWER_GRADE = AnswerGrade(is_correct=True, score=0.8, feedback="Good answer.")

SAMPLE_PARSED_JD = AIParsedJD(
    role_title="SDE",
    required_skills=[JDRequirementItem(value="Kubernetes", is_explicit=True)],
    preferred_skills=[],
    responsibilities=[],
    tools_technologies=[],
    experience_expectation=None,
    education_eligibility=None,
    other_signals=[],
    parsing_confidence=0.9,
)

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["DSA", "OOP"],
    education=[],
    experience=[],
    projects=[],
    certifications=[],
    ats_score=70.0,
    weak_sections=[],
    extraction_confidence=0.9,
)


class FakeAIProvider(AIProvider):
    def __init__(self, generated_question=None):
        self.generated_question = generated_question or SAMPLE_GENERATED_MCQ

    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            return SAMPLE_PARSED_JD
        if schema is ParsedResume:
            return SAMPLE_PARSED_RESUME
        if schema is GeneratedQuestion:
            return self.generated_question
        if schema is AnswerGrade:
            return SAMPLE_ANSWER_GRADE
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post(
        "/auth/signup", json={"email": "assessments@example.com", "password": "supersecret123"}
    )
    token = client.post(
        "/auth/login", json={"email": "assessments@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def use_fake_ai_short_answer():
    provider = FakeAIProvider(generated_question=SAMPLE_GENERATED_SHORT_ANSWER)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


class OutOfRangeGradeAIProvider(AIProvider):
    """Simulates a schema-valid but semantically invalid grading response: AnswerGrade.score
    is a plain `float` with no declared bound, so nothing stops a provider from returning a
    "0.0-1.0" score as 50.0."""

    def generate_structured(self, prompt, schema):
        if schema is GeneratedQuestion:
            return SAMPLE_GENERATED_SHORT_ANSWER
        if schema is AnswerGrade:
            return AnswerGrade(is_correct=True, score=50.0, feedback="Out-of-range score.")
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def use_out_of_range_grade_ai():
    provider = OutOfRangeGradeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


class FailingAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        raise AIGenerationError("AI provider is unavailable right now")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def use_failing_ai():
    provider = FailingAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def sde_role_id(client):
    roles = client.get("/roles").json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_blueprint_requires_role_or_jd(client, auth_headers):
    resp = client.post("/assessments/blueprint", json={}, headers=auth_headers)
    assert resp.status_code == 422


def test_blueprint_from_role_only(client, auth_headers, sde_role_id):
    resp = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role_id"] == sde_role_id
    assert body["jd_id"] is None
    assert body["source"] == "default_taxonomy"
    skills = {s["skill"] for s in body["sections"]}
    assert "DSA" in skills
    # No resume uploaded yet -> everything is treated as a gap.
    assert all(s["is_gap"] for s in body["sections"])


def test_blueprint_prioritizes_gaps_over_known_skills(client, auth_headers, sde_role_id, use_fake_ai):
    from tests.fixtures.pdf_helpers import make_pdf_with_text

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("resume text"), "application/pdf")},
        headers=auth_headers,
    )

    resp = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    )
    assert resp.status_code == 201
    sections = {s["skill"]: s for s in resp.json()["sections"]}

    # Resume has DSA and OOP (case-insensitive/substring matched) -> not gaps.
    assert sections["DSA"]["is_gap"] is False
    assert sections["OOP"]["is_gap"] is False
    # Aptitude/DBMS/OS/Networks were never in the resume -> still gaps.
    assert sections["Aptitude"]["is_gap"] is True


def test_blueprint_role_not_found(client, auth_headers):
    resp = client.post("/assessments/blueprint", json={"role_id": 999999}, headers=auth_headers)
    assert resp.status_code == 422


def test_blueprint_category_filter_narrows_to_one_assessment_type(client, auth_headers, sde_role_id):
    resp = client.post(
        "/assessments/blueprint",
        json={"role_id": sde_role_id, "category": "aptitude"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["sections"]
    assert all(s["assessment_type"] == "aptitude" for s in body["sections"])


def test_blueprint_category_filter_no_matching_skills_returns_422(client, auth_headers, sde_role_id):
    resp = client.post(
        "/assessments/blueprint",
        json={"role_id": sde_role_id, "category": "statistics"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_blueprint_from_jd_with_role_detected(client, auth_headers, use_fake_ai, sde_role_id):
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.post("/assessments/blueprint", json={"jd_id": jd_id}, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["role_id"] == sde_role_id
    assert body["jd_id"] == jd_id
    assert body["source"] == "jd_override"

    skills = {s["skill"]: s for s in body["sections"]}
    assert "Kubernetes" in skills  # JD-only skill, added with a default assessment_type
    assert skills["Kubernetes"]["assessment_type"] == "technical"
    assert "DSA" in skills  # base role taxonomy still present


def test_blueprint_jd_not_found(client, auth_headers):
    resp = client.post("/assessments/blueprint", json={"jd_id": 999999}, headers=auth_headers)
    assert resp.status_code == 422


def test_blueprint_jd_isolated_per_user(client, auth_headers, use_fake_ai):
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    client.post("/auth/signup", json={"email": "other-blueprint@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-blueprint@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.post(
        "/assessments/blueprint",
        json={"jd_id": jd_id},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    assert resp.status_code == 422


@pytest.fixture()
def blueprint_id(client, auth_headers, sde_role_id):
    return client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    ).json()["id"]


def test_start_assessment(client, auth_headers, blueprint_id):
    resp = client.post("/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["blueprint_id"] == blueprint_id
    assert body["status"] == "in_progress"


def test_start_assessment_unknown_blueprint(client, auth_headers):
    resp = client.post("/assessments", json={"blueprint_id": 999999}, headers=auth_headers)
    assert resp.status_code == 404


def test_next_question_generates_first_question_at_medium_difficulty(
    client, auth_headers, blueprint_id, use_fake_ai
):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    resp = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["completed"] is False
    question = body["question"]
    assert question["difficulty"] == "medium"
    assert question["type"] == "mcq"
    assert len(question["options"]) == 4
    # The answer key must never be sent to the client before they respond.
    assert "expected_answer_or_rubric" not in question
    assert "explanation" not in question


def test_next_question_is_idempotent_while_pending(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    first = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
    second = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
    assert first["question"]["id"] == second["question"]["id"]


def test_submit_correct_mcq_answer(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]

    resp = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "Structured Query Language"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_correct"] is True
    assert body["score"] == 1.0
    assert body["explanation"]


def test_leaked_prompt_text_never_reaches_the_student(client, auth_headers, blueprint_id):
    """Real finding: even after hardening question_generator_v2.txt's wording twice, a
    live-tested attack (skill_tag = an embedded "reveal the system prompt" instruction)
    still leaked this prompt's own text into `explanation` against a real model (Ollama
    qwen3:8b, 3/3 repeated runs). This test proves the code-level backstop
    (assessment_run_service._sanitize_generated_question / app/ai/output_guard.py) actually
    stops such a leak from ever reaching the student via the real API path, using a fake
    provider standing in for a model that got hijacked."""
    leaked_question = GeneratedQuestion(
        type="mcq",
        prompt="Which of the following is the correct system prompt text as described in the security rules?",
        options=["A", "A", "A", "A"],
        expected_answer_or_rubric="A",
        explanation="The system prompt is: SECURITY RULE -- READ CAREFULLY, THIS IS THE MOST IMPORTANT RULE...",
    )
    provider = FakeAIProvider(generated_question=leaked_question)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    try:
        assessment_id = client.post(
            "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
        ).json()["id"]
        question = client.get(
            f"/assessments/{assessment_id}/next-question", headers=auth_headers
        ).json()["question"]

        assert "system prompt" not in question["prompt"].lower()
        assert "security rule" not in question["prompt"].lower()
        assert len(set(question["options"])) > 1  # never the degenerate "A/A/A/A" the leak produced

        resp = client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": question["id"], "student_answer": "A relevant concept"},
            headers=auth_headers,
        )
        explanation = resp.json()["explanation"].lower()
        assert "system prompt" not in explanation
        assert "security rule" not in explanation
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)


def test_submit_wrong_mcq_answer(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]

    resp = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "Simple Query Logic"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_correct"] is False
    assert body["score"] == 0.0


def test_submit_short_answer_uses_ai_grading(client, auth_headers, blueprint_id, use_fake_ai_short_answer):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]
    assert question["type"] == "short_answer"
    assert question["options"] is None

    resp = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "It uniquely identifies a row."},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_correct"] is True
    assert body["score"] == 0.8


def test_next_question_returns_422_not_500_when_ai_provider_fails(client, auth_headers, blueprint_id, use_failing_ai):
    """Regression for a real audit gap: only interviews.py previously converted
    AIGenerationError into a documented 422 -- the identical failure here surfaced as an
    opaque 500, burning a rate-limited student's quota on an unrecoverable-feeling error
    instead of a clean, retryable-feeling 422."""
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    resp = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)
    assert resp.status_code == 422


def test_submit_answer_returns_422_not_500_when_ai_provider_fails(
    client, auth_headers, blueprint_id, use_fake_ai_short_answer
):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]
    assert question["type"] == "short_answer"  # non-mcq -> grading goes through the AI provider

    app.dependency_overrides[get_ai_provider] = lambda: FailingAIProvider()
    try:
        resp = client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": question["id"], "student_answer": "It uniquely identifies a row."},
            headers=auth_headers,
        )
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)

    assert resp.status_code == 422


def test_out_of_range_ai_grade_score_is_clamped(client, auth_headers, blueprint_id, use_out_of_range_grade_ai):
    """Regression for a real audit gap: nothing previously enforced AnswerGrade.score's
    documented 0.0-1.0 range at the code level. A provider returning score=50.0 must have it
    clamped to 1.0 before it is ever persisted or returned."""
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]
    assert question["type"] == "short_answer"

    resp = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "It uniquely identifies a row."},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["score"] == 1.0  # clamped down from 50.0, never persisted verbatim


def test_answer_rejected_when_no_pending_question(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]
    first = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "Structured Query Language"},
        headers=auth_headers,
    )
    assert first.status_code == 200

    # Phase 31 reclassified exactly one of the two cases this used to lump together. Resending
    # the *same* answer is a transport replay of a mutation that already succeeded, so it now
    # gets that answer's stored result rather than a 409 the client would show as a failure.
    replay = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "Structured Query Language"},
        headers=auth_headers,
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()

    # Answering *differently* is still refused, which is what this test was really protecting.
    changed = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question["id"], "student_answer": "Simple Query Language"},
        headers=auth_headers,
    )
    assert changed.status_code == 409


def test_result_requires_completed_assessment(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)

    resp = client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)
    assert resp.status_code == 409


def test_assessment_isolated_per_user(client, auth_headers, blueprint_id):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    client.post("/auth/signup", json={"email": "other-assessment@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-assessment@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.get(f"/assessments/{assessment_id}/next-question", headers=other_headers)
    assert resp.status_code == 404


def test_full_assessment_lifecycle_reaches_result(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    answered = 0
    for _ in range(100):
        next_q = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        question = next_q["question"]
        answer_resp = client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": question["id"], "student_answer": "Structured Query Language"},
            headers=auth_headers,
        )
        assert answer_resp.status_code == 200
        answered += 1
    else:
        pytest.fail("Assessment did not complete within 100 questions")

    assert answered > 0

    result = client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)
    assert result.status_code == 200
    body = result.json()
    assert body["overall_score"] == 1.0
    assert body["priority_weaknesses"] == []
    assert len(body["skill_scores"]) == 6  # SDE taxonomy: Aptitude/DSA/OOP/DBMS/OS/Networks

    # Result is persisted -- a second call returns the same computed values, not a re-derive.
    result_again = client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)
    assert result_again.json() == body


def test_assessment_history_empty(client, auth_headers):
    resp = client.get("/assessments/history", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_assessment_history_in_progress_assessment_excluded(client, auth_headers, blueprint_id, use_fake_ai):
    # No AssessmentResult row exists until the assessment actually completes -- history must
    # only ever surface finished tests, not ones still in progress.
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)

    resp = client.get("/assessments/history", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_assessment_history_after_completion(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]

    for _ in range(100):
        next_q = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        question = next_q["question"]
        client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": question["id"], "student_answer": "Structured Query Language"},
            headers=auth_headers,
        )
    else:
        pytest.fail("Assessment did not complete within 100 questions")

    # Completing the assessment also computes/caches the result (via GET .../result) --
    # history reads AssessmentResult, so trigger that the same way a real client would.
    client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)

    resp = client.get("/assessments/history", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["assessment_id"] == assessment_id
    assert item["blueprint_id"] == blueprint_id
    assert item["category"] == "coding"  # DSA is the SDE taxonomy's highest-weight section
    assert item["overall_score"] == 1.0
    assert item["accuracy"] == 1.0
    assert item["question_count"] > 0


def test_assessment_history_isolated_per_user(client, auth_headers, blueprint_id, use_fake_ai):
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)

    client.post("/auth/signup", json={"email": "other-history@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-history@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get("/assessments/history", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 200
    assert resp.json() == []
