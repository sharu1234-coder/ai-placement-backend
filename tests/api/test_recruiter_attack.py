import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.project_builder import GeneratedProjectProof, GeneratedStageGuidanceSet
from app.ai.schemas.recruiter_attack import GeneratedRecruiterAttackEvaluation, GeneratedRecruiterAttackQuestion
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
from app.main import app
from app.services.project_builder.stages import STAGE_ORDER
from tests.fixtures.pdf_helpers import make_pdf_with_text

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["Python"], education=[], experience=[], projects=[], certifications=[],
    ats_score=70.0, weak_sections=[], extraction_confidence=0.8,
)


class FakeAIProvider(AIProvider):
    """Handles every schema this phase's flows touch: project-builder creation/completion
    (to produce a real completed build to attack), resume parsing (to produce a real resume
    to pull a claim from), and the two recruiter-attack schemas themselves."""

    def generate_structured(self, prompt, schema):
        if schema is GeneratedStageGuidanceSet:
            return GeneratedStageGuidanceSet(stages=[])  # deterministic fallback guidance
        if schema is GeneratedProjectProof:
            return GeneratedProjectProof(
                resume_bullet="Built it.", readme_markdown="# Project", explanation_60s="60s.",
                explanation_5min="5min.", defense_questions=["Why?"],
            )
        if schema is ParsedResume:
            return SAMPLE_PARSED_RESUME
        if schema is GeneratedRecruiterAttackQuestion:
            return GeneratedRecruiterAttackQuestion(question_text="Defend this claim.", based_on="Python")
        if schema is GeneratedRecruiterAttackEvaluation:
            return GeneratedRecruiterAttackEvaluation(
                technical_defense=0.8, project_ownership=0.75, specificity=0.7, communication=0.85,
                overall_score=0.78, feedback="Clear, specific, and well-defended.",
            )
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "attack@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "attack@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "attack2@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "attack2@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _completed_build_id(client, headers) -> int:
    build_id = client.post(
        "/project-builder", json={"title": "Realtime Pipeline", "stack_summary": "Kafka, Python"}, headers=headers
    ).json()["id"]
    for stage_key in STAGE_ORDER:
        client.post(f"/project-builder/{build_id}/stages/{stage_key}/complete", json={}, headers=headers)
    return build_id


def _finish_session(client, headers, session_id: int) -> None:
    while True:
        next_turn = client.get(f"/recruiter-attack/sessions/{session_id}/next-turn", headers=headers).json()
        if next_turn["completed"]:
            return
        client.post(
            f"/recruiter-attack/sessions/{session_id}/answer",
            json={"turn_id": next_turn["turn"]["id"], "answer_text": "A specific, real answer."},
            headers=headers,
        )


def test_requires_auth(client):
    assert client.get("/recruiter-attack/sessions").status_code == 401


def test_start_session_rejects_incomplete_project(client, auth_headers, use_fake_ai):
    build_id = client.post("/project-builder", json={"title": "Unfinished"}, headers=auth_headers).json()["id"]
    resp = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_start_session_rejects_a_claim_not_on_the_resume(client, auth_headers, use_fake_ai):
    resp = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "resume_claim", "claim_text": "led a rocket launch team"},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_full_lifecycle_from_a_completed_project(client, auth_headers, use_fake_ai):
    build_id = _completed_build_id(client, auth_headers)

    session = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    )
    assert session.status_code == 201
    session_body = session.json()
    assert session_body["status"] == "in_progress"
    assert session_body["source_label"] == "Realtime Pipeline"

    _finish_session(client, auth_headers, session_body["id"])

    listed = client.get("/recruiter-attack/sessions", headers=auth_headers).json()
    assert listed[0]["status"] == "completed"

    results = client.get(f"/recruiter-attack/sessions/{session_body['id']}/results", headers=auth_headers)
    assert results.status_code == 200
    body = results.json()
    assert body["overall_score"] == 0.78
    assert body["feedback"]


def test_full_lifecycle_from_a_real_resume_claim(client, auth_headers, use_fake_ai):
    pdf_bytes = make_pdf_with_text("Led a team of 5 engineers to ship a fraud-detection pipeline.")
    client.post("/resumes", files={"file": ("resume.pdf", pdf_bytes, "application/pdf")}, headers=auth_headers)

    session = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "resume_claim", "claim_text": "fraud-detection pipeline"},
        headers=auth_headers,
    )
    assert session.status_code == 201
    assert session.json()["source_label"] == "fraud-detection pipeline"


def test_results_not_ready_before_completion(client, auth_headers, use_fake_ai):
    build_id = _completed_build_id(client, auth_headers)
    session_id = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    ).json()["id"]

    resp = client.get(f"/recruiter-attack/sessions/{session_id}/results", headers=auth_headers)
    assert resp.status_code == 409


def test_answering_an_already_answered_turn_conflicts(client, auth_headers, use_fake_ai):
    build_id = _completed_build_id(client, auth_headers)
    session_id = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    ).json()["id"]
    turn = client.get(f"/recruiter-attack/sessions/{session_id}/next-turn", headers=auth_headers).json()["turn"]

    first = client.post(
        f"/recruiter-attack/sessions/{session_id}/answer",
        json={"turn_id": turn["id"], "answer_text": "First answer."},
        headers=auth_headers,
    )
    assert first.status_code == 200

    second = client.post(
        f"/recruiter-attack/sessions/{session_id}/answer",
        json={"turn_id": turn["id"], "answer_text": "Second answer."},
        headers=auth_headers,
    )
    assert second.status_code == 409


def test_empty_answer_is_accepted(client, auth_headers, use_fake_ai):
    build_id = _completed_build_id(client, auth_headers)
    session_id = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    ).json()["id"]
    turn = client.get(f"/recruiter-attack/sessions/{session_id}/next-turn", headers=auth_headers).json()["turn"]

    resp = client.post(
        f"/recruiter-attack/sessions/{session_id}/answer",
        json={"turn_id": turn["id"], "answer_text": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["answer_text"] == ""


def test_isolated_per_profile(client, auth_headers, other_auth_headers, use_fake_ai):
    build_id = _completed_build_id(client, auth_headers)
    session_id = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    ).json()["id"]

    assert (
        client.get(f"/recruiter-attack/sessions/{session_id}/next-turn", headers=other_auth_headers).status_code
        == 404
    )
    assert client.get("/recruiter-attack/sessions", headers=other_auth_headers).json() == []

    # Another profile also can't attack someone else's project build.
    resp = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=other_auth_headers,
    )
    assert resp.status_code == 422


def test_completed_session_becomes_evidence_for_a_matching_skill(client, auth_headers, use_fake_ai):
    """Integration check mirroring project_builder's equivalent test: a completed, graded
    Recruiter Attack session must automatically feed Evidence Engine, no separate step."""
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

    session_id = client.post(
        "/recruiter-attack/sessions",
        json={"source_type": "project_build", "project_build_id": build_id},
        headers=auth_headers,
    ).json()["id"]
    _finish_session(client, auth_headers, session_id)
    client.get(f"/recruiter-attack/sessions/{session_id}/results", headers=auth_headers)

    after = client.get("/skill-passport/DSA", headers=auth_headers).json()
    assert any(item["source"] == "recruiter_attack" for item in after["evidence"])
