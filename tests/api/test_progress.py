import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.assessment import GeneratedQuestion
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


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is GeneratedQuestion:
            return SAMPLE_GENERATED_MCQ
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "progress@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "progress@example.com", "password": "supersecret123"}
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
def sde_role_id(client):
    roles = client.get("/roles").json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_dashboard_empty_for_new_user(client, auth_headers):
    resp = client.get("/progress/dashboard", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["performance_score"] is None
    assert body["completed_assessments"] == 0
    assert body["completed_interviews"] == 0
    assert body["roadmap_tasks_total"] == 0
    assert body["top_weaknesses"] == []
    assert body["trend"] == []


def test_dashboard_reflects_completed_assessment(client, auth_headers, sde_role_id, use_fake_ai):
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    ).json()["id"]
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
            json={"question_id": question["id"], "student_answer": "Simple Query Logic"},
            headers=auth_headers,
        )
    client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)

    resp = client.get("/progress/dashboard", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["performance_score"] == 0.0
    assert body["completed_assessments"] == 1
    assert body["completed_interviews"] == 0
    assert len(body["trend"]) == 1
    assert body["trend"][0]["kind"] == "assessment"
    assert "SDE" in body["trend"][0]["label"]
    assert set(body["top_weaknesses"]) <= {"Aptitude", "DSA", "OOP", "DBMS", "OS", "Networks"}
    assert len(body["top_weaknesses"]) > 0


def test_dashboard_isolated_per_user(client, auth_headers, sde_role_id, use_fake_ai):
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    ).json()["id"]
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
            json={"question_id": question["id"], "student_answer": "Simple Query Logic"},
            headers=auth_headers,
        )
    client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)

    client.post("/auth/signup", json={"email": "other-progress@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-progress@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get("/progress/dashboard", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["completed_assessments"] == 0
    assert body["performance_score"] is None
