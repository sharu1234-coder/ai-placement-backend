import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.assessment import GeneratedQuestion
from app.ai.schemas.roadmap import GeneratedRoadmap, GeneratedRoadmapTask
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

SAMPLE_GENERATED_ROADMAP = GeneratedRoadmap(
    tasks=[
        GeneratedRoadmapTask(
            day=1, task_type="learn", skill_tag="DSA", title="Review arrays",
            description="Read about arrays and practice 3 problems.",
        ),
        GeneratedRoadmapTask(
            day=3, task_type="practice", skill_tag="DSA", title="Practice problems",
            description="Solve 5 medium array problems.",
        ),
        GeneratedRoadmapTask(
            day=7, task_type="reassessment", skill_tag="DSA", title="Retest",
            description="Retake a DSA assessment.",
        ),
    ]
)


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is GeneratedQuestion:
            return SAMPLE_GENERATED_MCQ
        if schema is GeneratedRoadmap:
            return SAMPLE_GENERATED_ROADMAP
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "roadmap@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "roadmap@example.com", "password": "supersecret123"}
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


def _complete_assessment_with_wrong_answers(client, auth_headers, role_id):
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": role_id}, headers=auth_headers
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
    return assessment_id


def test_generate_requires_weakness_data(client, auth_headers, sde_role_id):
    resp = client.post("/roadmap/generate", json={}, headers=auth_headers)
    assert resp.status_code == 422


def test_generate_unknown_jd_rejected(client, auth_headers):
    resp = client.post("/roadmap/generate", json={"jd_id": 999999}, headers=auth_headers)
    assert resp.status_code == 422


def test_generate_from_assessment_weaknesses(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)

    resp = client.post("/roadmap/generate", json={"duration_days": 7}, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["duration_days"] == 7
    assert body["status"] == "active"
    assert body["jd_id"] is None
    assert len(body["tasks"]) == 3
    assert body["tasks"][0]["day_number"] == 1
    assert body["tasks"][0]["status"] == "pending"
    assert body["tasks"][0]["skill_tag"] == "DSA"


def test_generate_rejects_invalid_duration(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)

    resp = client.post("/roadmap/generate", json={"duration_days": 21}, headers=auth_headers)
    assert resp.status_code == 422


def test_get_current_roadmap_none_before_generation(client, auth_headers):
    resp = client.get("/roadmap/current", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_current_roadmap_after_generation(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    generated = client.post("/roadmap/generate", json={}, headers=auth_headers).json()

    current = client.get("/roadmap/current", headers=auth_headers).json()
    assert current["id"] == generated["id"]


def test_generate_supersedes_previous_active_roadmap(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    first = client.post("/roadmap/generate", json={}, headers=auth_headers).json()

    second = client.post("/roadmap/generate", json={}, headers=auth_headers).json()
    assert second["id"] != first["id"]

    current = client.get("/roadmap/current", headers=auth_headers).json()
    assert current["id"] == second["id"]


def test_update_task_status(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    roadmap = client.post("/roadmap/generate", json={}, headers=auth_headers).json()
    task_id = roadmap["tasks"][0]["id"]

    resp = client.patch(f"/roadmap/tasks/{task_id}", json={"status": "done"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "done"


def test_update_task_not_found(client, auth_headers):
    resp = client.patch("/roadmap/tasks/999999", json={"status": "done"}, headers=auth_headers)
    assert resp.status_code == 404


def test_update_task_isolated_per_user(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    roadmap = client.post("/roadmap/generate", json={}, headers=auth_headers).json()
    task_id = roadmap["tasks"][0]["id"]

    client.post("/auth/signup", json={"email": "other-roadmap@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-roadmap@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.patch(
        f"/roadmap/tasks/{task_id}", json={"status": "done"}, headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404


def test_reassess_generates_new_roadmap_and_supersedes_old(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    first = client.post("/roadmap/generate", json={}, headers=auth_headers).json()

    resp = client.post(f"/roadmap/{first['id']}/reassess", headers=auth_headers)
    assert resp.status_code == 200
    second = resp.json()
    assert second["id"] != first["id"]
    assert second["duration_days"] == first["duration_days"]

    current = client.get("/roadmap/current", headers=auth_headers).json()
    assert current["id"] == second["id"]


def test_reassess_not_found(client, auth_headers):
    resp = client.post("/roadmap/999999/reassess", headers=auth_headers)
    assert resp.status_code == 404


def test_reassess_isolated_per_user(client, auth_headers, sde_role_id, use_fake_ai):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    roadmap = client.post("/roadmap/generate", json={}, headers=auth_headers).json()

    client.post("/auth/signup", json={"email": "other-reassess@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-reassess@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.post(
        f"/roadmap/{roadmap['id']}/reassess", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404
