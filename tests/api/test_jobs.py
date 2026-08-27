from typing import Optional

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.job_preparation_plan import (
    GeneratedCategoryReason,
    GeneratedInterviewQuestion,
    GeneratedJobPreparationContent,
)
from app.ai.schemas.resume import ParsedResume
from app.core.config import settings
from app.main import app

SAMPLE_PREP_CONTENT = GeneratedJobPreparationContent(
    category_reasons=[
        GeneratedCategoryReason(category="DSA", reason="Strong DSA fundamentals are expected for this role."),
        GeneratedCategoryReason(category="SQL", reason="SQL is explicitly required by this role."),
    ],
    interview_questions=[
        GeneratedInterviewQuestion(bucket="technical", question="How would you optimize a slow SQL query?"),
        GeneratedInterviewQuestion(bucket="sql", question="Explain the difference between INNER and LEFT JOIN."),
    ],
)

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
    def __init__(
        self,
        jd: Optional[AIParsedJD] = None,
        resume: Optional[ParsedResume] = None,
        prep_content: Optional[GeneratedJobPreparationContent] = None,
    ):
        self._jd = jd
        self._resume = resume
        self._prep_content = prep_content or SAMPLE_PREP_CONTENT

    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            return self._jd
        if schema is ParsedResume:
            return self._resume
        if schema is GeneratedJobPreparationContent:
            return self._prep_content
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


def test_blank_role_title_detects_no_role(client, auth_headers, use_fake_ai):
    # Regression: a blank AI-returned role_title used to make `"" in name_lower` trivially
    # True for every role, silently "detecting" whichever role was iterated first.
    blank_title_jd = SAMPLE_PARSED_JD.model_copy(update={"role_title": "  "})
    use_fake_ai(FakeAIProvider(jd=blank_title_jd))

    resp = client.post("/jobs", json={"raw_text": "an unclear job description"}, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["detected_role_id"] is None


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


def test_delete_job_description(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.delete(f"/jobs/{jd_id}", headers=auth_headers)
    assert resp.status_code == 204
    assert client.get(f"/jobs/{jd_id}/requirements", headers=auth_headers).status_code == 404


def test_delete_job_description_not_found(client, auth_headers):
    resp = client.delete("/jobs/999999", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_job_description_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    client.post("/auth/signup", json={"email": "other-delete-jd@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-delete-jd@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.delete(f"/jobs/{jd_id}", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 404


def test_delete_job_description_blocked_when_blueprint_exists(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    blueprint_resp = client.post("/assessments/blueprint", json={"jd_id": jd_id}, headers=auth_headers)
    assert blueprint_resp.status_code == 201

    resp = client.delete(f"/jobs/{jd_id}", headers=auth_headers)
    assert resp.status_code == 409


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


def test_list_jobs_empty(client, auth_headers):
    resp = client.get("/jobs", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_jobs_most_recent_first_and_plan_flag(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD, resume=SAMPLE_PARSED_RESUME))

    from tests.fixtures.pdf_helpers import make_pdf_with_text

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    )

    first_id = client.post("/jobs", json={"raw_text": "first jd"}, headers=auth_headers).json()["id"]
    second_id = client.post("/jobs", json={"raw_text": "second jd"}, headers=auth_headers).json()["id"]
    client.post(f"/jobs/{second_id}/preparation-plan", json={}, headers=auth_headers)

    resp = client.get("/jobs", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body] == [second_id, first_id]
    assert body[0]["has_preparation_plan"] is True
    assert body[1]["has_preparation_plan"] is False


def test_list_jobs_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers)

    client.post("/auth/signup", json={"email": "other-list-jobs@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-list-jobs@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get("/jobs", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_generate_preparation_plan_requires_resume(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.post(f"/jobs/{jd_id}/preparation-plan", json={}, headers=auth_headers)
    assert resp.status_code == 422


def test_generate_preparation_plan_not_found(client, auth_headers):
    resp = client.post("/jobs/999999/preparation-plan", json={}, headers=auth_headers)
    assert resp.status_code == 404


def test_generate_preparation_plan_success(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD, resume=SAMPLE_PARSED_RESUME))

    from tests.fixtures.pdf_helpers import make_pdf_with_text

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    )
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.post(
        f"/jobs/{jd_id}/preparation-plan",
        json={"company_name": "Acme Corp", "job_title": "Data Analyst"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["jd_id"] == jd_id
    assert body["company_name"] == "Acme Corp"
    assert body["job_summary"]["company_name"] == "Acme Corp"
    assert body["job_summary"]["job_title"] == "Data Analyst"

    gaps = {g["skill"]: g["status"] for g in body["preparation_gaps"]}
    assert gaps["SQL"] == "strong"
    assert gaps["Excel"] == "strong"
    assert gaps["Python"] == "missing"

    categories = {c["category"] for c in body["preparation_plan"]}
    # Always-relevant baseline categories.
    assert {"DSA", "Aptitude", "Programming", "Core CS", "Technical", "HR", "Resume"} <= categories
    # SQL is one of the JD's own required skills -> should be included as relevant.
    assert "SQL" in categories

    assert len(body["interview_prep"]) == 2
    assert body["required_skills"]  # skill/tool requirements, categorized


def test_get_preparation_plan_404_before_generation(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD))
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    resp = client.get(f"/jobs/{jd_id}/preparation-plan", headers=auth_headers)
    assert resp.status_code == 404


def test_get_preparation_plan_after_generation(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD, resume=SAMPLE_PARSED_RESUME))

    from tests.fixtures.pdf_helpers import make_pdf_with_text

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    )
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]
    generated = client.post(f"/jobs/{jd_id}/preparation-plan", json={}, headers=auth_headers).json()

    resp = client.get(f"/jobs/{jd_id}/preparation-plan", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == generated["id"]


def test_preparation_plan_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(jd=SAMPLE_PARSED_JD, resume=SAMPLE_PARSED_RESUME))

    from tests.fixtures.pdf_helpers import make_pdf_with_text

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    )
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=auth_headers).json()["id"]

    client.post("/auth/signup", json={"email": "other-prep-plan@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-prep-plan@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.post(f"/jobs/{jd_id}/preparation-plan", json={}, headers=other_headers)
    assert resp.status_code == 404
