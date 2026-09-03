import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.project_builder import GeneratedProjectProof, GeneratedStageGuidanceSet
from app.main import app
from app.services.project_builder.stages import STAGE_ORDER

SAMPLE_PROOF = GeneratedProjectProof(
    resume_bullet="Built a real-time analytics pipeline using Kafka and Flink.",
    readme_markdown="# Realtime Pipeline",
    explanation_60s="A 60 second explanation.",
    explanation_5min="A 5 minute explanation.",
    defense_questions=["Why Kafka?", "How do you handle failures?"],
)


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is GeneratedStageGuidanceSet:
            return GeneratedStageGuidanceSet(stages=[])  # forces the deterministic fallback for every stage
        if schema is GeneratedProjectProof:
            return SAMPLE_PROOF
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
    client.post("/auth/signup", json={"email": "builder@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "builder@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "builder2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "builder2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_requires_auth(client):
    assert client.get("/project-builder").status_code == 403


def test_create_returns_all_ten_stages_not_started(client, auth_headers, use_fake_ai):
    resp = client.post(
        "/project-builder", json={"title": "Realtime Pipeline", "stack_summary": "Kafka, Flink"}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "not_started"
    assert len(body["stages"]) == 10
    assert [s["stage_key"] for s in body["stages"]] == STAGE_ORDER
    assert all(s["status"] == "not_started" for s in body["stages"])
    assert all(s["guidance_text"] for s in body["stages"])


def test_list_and_get(client, auth_headers, use_fake_ai):
    created = client.post("/project-builder", json={"title": "Realtime Pipeline"}, headers=auth_headers).json()

    listed = client.get("/project-builder", headers=auth_headers).json()
    assert len(listed) == 1
    assert listed[0]["id"] == created["id"]
    assert "stages" not in listed[0]  # list items are the lighter shape, no stage detail

    fetched = client.get(f"/project-builder/{created['id']}", headers=auth_headers).json()
    assert len(fetched["stages"]) == 10


def test_cannot_skip_a_stage(client, auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Project"}, headers=auth_headers).json()["id"]

    resp = client.post(f"/project-builder/{build_id}/stages/{STAGE_ORDER[1]}/start", headers=auth_headers)
    assert resp.status_code == 409

    resp = client.post(
        f"/project-builder/{build_id}/stages/{STAGE_ORDER[1]}/complete", json={}, headers=auth_headers
    )
    assert resp.status_code == 409


def test_unknown_stage_key_returns_404(client, auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Project"}, headers=auth_headers).json()["id"]
    resp = client.post(f"/project-builder/{build_id}/stages/not_a_real_stage/start", headers=auth_headers)
    assert resp.status_code == 404


def test_unknown_build_returns_404(client, auth_headers, use_fake_ai):
    resp = client.get("/project-builder/999999", headers=auth_headers)
    assert resp.status_code == 404
    resp = client.post(f"/project-builder/999999/stages/{STAGE_ORDER[0]}/start", headers=auth_headers)
    assert resp.status_code == 404


def test_proof_not_ready_before_completion(client, auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Project"}, headers=auth_headers).json()["id"]
    resp = client.get(f"/project-builder/{build_id}/proof", headers=auth_headers)
    assert resp.status_code == 409


def test_full_lifecycle_reaches_proof(client, auth_headers, use_fake_ai):
    build_id = client.post(
        "/project-builder", json={"title": "Realtime Pipeline", "stack_summary": "Kafka, Flink"}, headers=auth_headers
    ).json()["id"]

    for stage_key in STAGE_ORDER:
        resp = client.post(
            f"/project-builder/{build_id}/stages/{stage_key}/complete",
            json={"notes": f"Notes for {stage_key}"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    build = client.get(f"/project-builder/{build_id}", headers=auth_headers).json()
    assert build["status"] == "completed"

    proof = client.get(f"/project-builder/{build_id}/proof", headers=auth_headers).json()
    assert proof["resume_bullet"] == SAMPLE_PROOF.resume_bullet
    assert len(proof["defense_questions"]) == 2


def test_duplicate_completion_is_not_an_error(client, auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Project"}, headers=auth_headers).json()["id"]

    first = client.post(
        f"/project-builder/{build_id}/stages/{STAGE_ORDER[0]}/complete",
        json={"notes": "Original"},
        headers=auth_headers,
    ).json()
    second = client.post(
        f"/project-builder/{build_id}/stages/{STAGE_ORDER[0]}/complete",
        json={"notes": "Should be ignored"},
        headers=auth_headers,
    ).json()
    assert second["completed_at"] == first["completed_at"]
    assert second["notes"] == "Original"


def test_isolated_per_profile(client, auth_headers, other_auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Mine"}, headers=auth_headers).json()["id"]

    assert client.get(f"/project-builder/{build_id}", headers=other_auth_headers).status_code == 404
    assert (
        client.post(f"/project-builder/{build_id}/stages/{STAGE_ORDER[0]}/start", headers=other_auth_headers).status_code
        == 404
    )
    assert client.get("/project-builder", headers=other_auth_headers).json() == []


def test_completed_build_becomes_evidence_for_a_matching_skill(client, auth_headers, use_fake_ai):
    """Integration check for the plan's explicit requirement: a completed Project Builder
    build must automatically feed Evidence Engine as a new project source, no separate step."""
    roles = client.get("/roles").json()
    role_id = next(r["id"] for r in roles if r["name"] == "SDE")
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    before = client.get("/skill-passport/DSA", headers=auth_headers).json()
    assert before["state"] == "claimed"

    build_id = client.post(
        "/project-builder", json={"title": "DSA Visualizer", "stack_summary": "DSA, Python"}, headers=auth_headers
    ).json()["id"]
    for stage_key in STAGE_ORDER:
        client.post(f"/project-builder/{build_id}/stages/{stage_key}/complete", json={}, headers=auth_headers)

    after = client.get("/skill-passport/DSA", headers=auth_headers).json()
    assert after["state"] == "supported"
    assert any(item["source"] == "project" for item in after["evidence"])
