from typing import Optional

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
from app.main import app

SAMPLE_PARSED_JD = AIParsedJD(
    role_title="Data Analyst Intern",
    required_skills=[
        JDRequirementItem(value="SQL", is_explicit=True),
        JDRequirementItem(value="Excel", is_explicit=True),
    ],
    preferred_skills=[JDRequirementItem(value="Python", is_explicit=True)],
    responsibilities=["Analyze sales data", "Build dashboards"],
    tools_technologies=["Power BI"],
    experience_expectation="0-1 years",
    education_eligibility="Bachelor's degree",
    other_signals=[],
    parsing_confidence=0.9,
)

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["SQL", "Excel", "Java"],
    education=[],
    experience=[],
    projects=[],
    certifications=[],
    ats_score=70.0,
    weak_sections=[],
    extraction_confidence=0.9,
)


class FakeAIProvider(AIProvider):
    def __init__(self, jd: Optional[AIParsedJD] = None, resume: Optional[ParsedResume] = None):
        self._jd = jd
        self._resume = resume

    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            return self._jd
        if schema is ParsedResume:
            return self._resume
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "jobs@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "jobs@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))


@pytest.fixture()
def use_fake_ai():
    def _use(provider: AIProvider):
        app.dependency_overrides[get_ai_provider] = lambda: provider
        return provider

    yield _use
    app.dependency_overrides.pop(get_ai_provider, None)


def test_create_job_description_success(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))

    resp = client.post(
        "/jobs", json={"raw_text": "We need a Data Analyst Intern with SQL and Excel."}, headers=auth_headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["role_title"] == "Data Analyst Intern"
    assert body["parsing_confidence"] == 0.9

    roles = client.get("/roles").json()
    data_analyst_id = next(r["id"] for r in roles if r["name"] == "Data Analyst")
    assert body["detected_role_id"] == data_analyst_id

    categories = {r["category"] for r in body["requirements"]}
    assert categories == {"skill", "tool", "responsibility", "experience", "education"}

    skill_reqs = {r["value"]: r["importance"] for r in body["requirements"] if r["category"] == "skill"}
    assert skill_reqs == {"SQL": "required", "Excel": "required", "Python": "preferred"}


def test_get_job_requirements(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))

    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.get(f"/jobs/{jd_id}/requirements", headers=auth_headers)
    assert resp.status_code == 200
    # 2 required skills + 1 preferred skill + 1 tool + 2 responsibilities + 1 experience + 1 education
    assert len(resp.json()) == 8


def test_get_requirements_not_found(client, auth_headers):
    resp = client.get("/jobs/999999/requirements", headers=auth_headers)
    assert resp.status_code == 404


def test_get_requirements_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    client.post("/auth/signup", json={"email": "other-jobs@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-jobs@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get(
        f"/jobs/{jd_id}/requirements", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404


def test_match_without_resume_returns_422(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.post(f"/jobs/{jd_id}/match", headers=auth_headers)
    assert resp.status_code == 422


def test_match_not_found(client, auth_headers):
    resp = client.post("/jobs/999999/match", headers=auth_headers)
    assert resp.status_code == 404


def test_match_with_resume_success(client, auth_headers, use_fake_ai):
    fake = FakeAIProvider(jd=SAMPLE_PARSED_JD, resume=SAMPLE_PARSED_RESUME)
    use_fake_ai(fake)

    from tests.fixtures.pdf_helpers import make_pdf_with_text

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    )

    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.post(f"/jobs/{jd_id}/match", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["matching_skills"]) == {"SQL", "Excel"}
    assert body["missing_skills"] == ["Python"]
    assert body["match_score"] == 80.0
    assert len(body["skill_gaps"]) == 1
    assert body["skill_gaps"][0] == {
        "id": body["skill_gaps"][0]["id"],
        "skill_name": "Python",
        "gap_severity": "medium",
        "source": "jd_match",
    }
