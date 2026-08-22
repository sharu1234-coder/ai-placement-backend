import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
from app.main import app

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
    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            return SAMPLE_PARSED_JD
        if schema is ParsedResume:
            return SAMPLE_PARSED_RESUME
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
