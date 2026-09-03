import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.interview import GeneratedInterviewEvaluation, GeneratedInterviewQuestion
from app.main import app

SAMPLE_GENERATED_QUESTION = GeneratedInterviewQuestion(
    question_text="Tell me about a challenging bug you fixed and how you debugged it.",
    based_on="Technical: debugging",
)

# technical_correctness is deliberately weak (< 0.6); everything else is comfortably above
# threshold, so only interview:technical_correctness should ever get a WeaknessRecord.
WEAK_EVALUATION = GeneratedInterviewEvaluation(
    technical_correctness=0.3,
    relevance=0.75,
    completeness=0.7,
    reasoning=0.8,
    structure=0.7,
    clarity=0.85,
    grammar=0.9,
    role_jd_coverage=0.7,
    overall_score=0.6,
    feedback="Technical depth needs work; everything else is solid.",
)

GOOD_EVALUATION = GeneratedInterviewEvaluation(
    technical_correctness=0.9,
    relevance=0.8,
    completeness=0.8,
    reasoning=0.8,
    structure=0.8,
    clarity=0.85,
    grammar=0.9,
    role_jd_coverage=0.8,
    overall_score=0.83,
    feedback="Strong technical answer this time.",
)


class FakeAIProvider(AIProvider):
    def __init__(self, evaluation: GeneratedInterviewEvaluation):
        self._evaluation = evaluation

    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewQuestion:
            return SAMPLE_GENERATED_QUESTION
        if schema is GeneratedInterviewEvaluation:
            return self._evaluation
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"

    def transcribe(self, audio: bytes) -> str:
        return "transcript"

    def synthesize_speech(self, text: str) -> bytes:
        return b"FAKEWAVDATA"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "weak@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "weak@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "weak2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "weak2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def role_selected(client, auth_headers):
    roles = client.get("/roles").json()
    role_id = next(r["id"] for r in roles if r["name"] == "SDE")
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)
    return role_id


def _run_full_interview(client, auth_headers, evaluation: GeneratedInterviewEvaluation):
    provider = FakeAIProvider(evaluation)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    try:
        interview_id = client.post("/interviews", json={"type": "technical"}, headers=auth_headers).json()["id"]
        for _ in range(20):
            next_q = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
            if next_q["completed"]:
                break
            question = next_q["question"]
            client.post(
                f"/interviews/{interview_id}/answer",
                json={"interview_question_id": question["id"], "answer_text": "A thoughtful, complete answer."},
                headers=auth_headers,
            )
        result = client.get(f"/interviews/{interview_id}/evaluation", headers=auth_headers)
        assert result.status_code == 200
        return interview_id
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)


def test_requires_auth(client):
    assert client.get("/weaknesses").status_code == 403


def test_no_interviews_yet_returns_empty_list(client, auth_headers):
    resp = client.get("/weaknesses", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_a_weak_dimension_creates_a_NEW_record_after_one_interview(client, auth_headers, role_selected):
    _run_full_interview(client, auth_headers, WEAK_EVALUATION)

    resp = client.get("/weaknesses", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    record = body[0]
    assert record["skill_key"] == "interview:technical_correctness"
    assert record["status"] == "NEW"
    assert record["occurrences"] == 1


def test_repeated_weak_interviews_advance_to_REPEATED(client, auth_headers, role_selected):
    _run_full_interview(client, auth_headers, WEAK_EVALUATION)
    _run_full_interview(client, auth_headers, WEAK_EVALUATION)

    body = client.get("/weaknesses", headers=auth_headers).json()
    record = next(r for r in body if r["skill_key"] == "interview:technical_correctness")
    assert record["status"] == "REPEATED"
    assert record["occurrences"] == 2


def test_a_single_good_followup_interview_does_not_close_it(client, auth_headers, role_selected):
    _run_full_interview(client, auth_headers, WEAK_EVALUATION)
    _run_full_interview(client, auth_headers, GOOD_EVALUATION)

    body = client.get("/weaknesses", headers=auth_headers).json()
    record = next(r for r in body if r["skill_key"] == "interview:technical_correctness")
    assert record["status"] == "IMPROVING"
    assert record["status"] != "CLOSED"


def test_isolated_per_profile(client, auth_headers, other_auth_headers, role_selected):
    roles = client.get("/roles").json()
    role_id = next(r["id"] for r in roles if r["name"] == "SDE")
    client.post("/users/me/role", json={"role_id": role_id}, headers=other_auth_headers)

    _run_full_interview(client, auth_headers, WEAK_EVALUATION)

    mine = client.get("/weaknesses", headers=auth_headers).json()
    theirs = client.get("/weaknesses", headers=other_auth_headers).json()
    assert len(mine) == 1
    assert theirs == []
