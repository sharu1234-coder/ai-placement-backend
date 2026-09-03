import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.assessment import GeneratedQuestion
from app.main import app

SAMPLE_MCQ = GeneratedQuestion(
    type="mcq", prompt="Pick the right one.", options=["A", "B", "C"],
    expected_answer_or_rubric="A", explanation="A is correct.",
)


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is GeneratedQuestion:
            return SAMPLE_MCQ
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "arena@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "arena@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "arena2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "arena2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _answer_all(client, headers, assessment_id, correct=True):
    answer = "A" if correct else "Z"
    while True:
        next_q = client.get(f"/assessments/{assessment_id}/next-question", headers=headers).json()
        if next_q["completed"]:
            return
        client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": next_q["question"]["id"], "student_answer": answer},
            headers=headers,
        )


def test_requires_auth(client):
    assert client.get("/arena/challenges").status_code == 403


def test_list_challenges_returns_the_static_catalog(client, auth_headers):
    resp = client.get("/arena/challenges", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 5
    assert all(c["personal_best_score"] is None for c in body)


def test_start_unknown_challenge_returns_404(client, auth_headers):
    resp = client.post("/arena/challenges/not_a_real_challenge/attempt", headers=auth_headers)
    assert resp.status_code == 404


def test_completing_before_answering_returns_409(client, auth_headers, use_fake_ai):
    start = client.post("/arena/challenges/sql_sprint/attempt", headers=auth_headers).json()
    resp = client.post(
        f"/arena/attempts/{start['arena_attempt_id']}/complete",
        json={"time_taken_seconds": 5},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_negative_time_taken_seconds_is_rejected(client, auth_headers, use_fake_ai):
    start = client.post("/arena/challenges/sql_sprint/attempt", headers=auth_headers).json()
    resp = client.post(
        f"/arena/attempts/{start['arena_attempt_id']}/complete",
        json={"time_taken_seconds": -5},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_full_lifecycle_via_the_existing_assessment_endpoints(client, auth_headers, use_fake_ai):
    start = client.post("/arena/challenges/sql_sprint/attempt", headers=auth_headers).json()
    assert start["challenge_key"] == "sql_sprint"

    _answer_all(client, auth_headers, start["assessment_id"], correct=True)

    result = client.post(
        f"/arena/attempts/{start['arena_attempt_id']}/complete",
        json={"time_taken_seconds": 120},
        headers=auth_headers,
    )
    assert result.status_code == 200
    body = result.json()
    assert body["score"] == 1.0
    assert body["is_personal_best"] is True
    assert body["timed_out"] is False

    challenges = client.get("/arena/challenges", headers=auth_headers).json()
    sql_sprint = next(c for c in challenges if c["key"] == "sql_sprint")
    assert sql_sprint["personal_best_score"] == 1.0

    history = client.get("/arena/challenges/sql_sprint/history", headers=auth_headers).json()
    assert history["personal_best_score"] == 1.0
    assert len(history["attempts"]) == 1


def test_duplicate_completion_returns_the_same_result(client, auth_headers, use_fake_ai):
    start = client.post("/arena/challenges/sql_sprint/attempt", headers=auth_headers).json()
    _answer_all(client, auth_headers, start["assessment_id"], correct=True)

    first = client.post(
        f"/arena/attempts/{start['arena_attempt_id']}/complete",
        json={"time_taken_seconds": 100},
        headers=auth_headers,
    ).json()
    second = client.post(
        f"/arena/attempts/{start['arena_attempt_id']}/complete",
        json={"time_taken_seconds": 999},
        headers=auth_headers,
    ).json()
    assert second == first


def test_isolated_per_profile(client, auth_headers, other_auth_headers, use_fake_ai):
    start = client.post("/arena/challenges/sql_sprint/attempt", headers=auth_headers).json()

    resp = client.post(
        f"/arena/attempts/{start['arena_attempt_id']}/complete",
        json={"time_taken_seconds": 10},
        headers=other_auth_headers,
    )
    assert resp.status_code == 404

    other_challenges = client.get("/arena/challenges", headers=other_auth_headers).json()
    assert all(c["personal_best_score"] is None for c in other_challenges)

    other_history = client.get("/arena/challenges/sql_sprint/history", headers=other_auth_headers).json()
    assert other_history["attempts"] == []


def test_unknown_challenge_history_returns_404(client, auth_headers):
    resp = client.get("/arena/challenges/not_a_real_challenge/history", headers=auth_headers)
    assert resp.status_code == 404
