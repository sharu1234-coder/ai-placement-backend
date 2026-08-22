from typing import Optional

import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.resume import ParsedResume, ResumeEducation, ResumeExperience
from app.core.config import settings
from app.main import app
from tests.fixtures.pdf_helpers import make_empty_pdf, make_pdf_with_text

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["Python", "SQL"],
    education=[ResumeEducation(institution="IIT Bombay", degree="B.Tech")],
    experience=[ResumeExperience(company="Acme Corp", title="Intern")],
    projects=[],
    certifications=[],
    ats_score=72.0,
    weak_sections=["no quantified achievements"],
    extraction_confidence=0.85,
)


class FakeAIProvider(AIProvider):
    def __init__(self, parsed: Optional[ParsedResume] = None, should_fail: bool = False):
        self._parsed = parsed
        self._should_fail = should_fail

    def generate_structured(self, prompt, schema):
        if self._should_fail:
            raise AIGenerationError("fake failure")
        return self._parsed

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "resume@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "resume@example.com", "password": "supersecret123"}
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


def test_upload_resume_success(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    pdf_bytes = make_pdf_with_text("Jane Doe - Python developer with SQL experience.")
    resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["parse_status"] == "completed"
    assert body["analysis"]["skills"] == ["Python", "SQL"]
    assert body["analysis"]["ats_score"] == 72.0


def test_upload_rejects_non_pdf(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resp = client.post(
        "/resumes",
        files={"file": ("resume.txt", b"not a pdf", "text/plain")},
        headers=auth_headers,
    )
    assert resp.status_code == 415


def test_upload_rejects_unreadable_pdf(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", b"%PDF-not-really-a-pdf", "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_upload_rejects_empty_text_pdf(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_empty_pdf(), "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_upload_requires_auth(client):
    resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("hello"), "application/pdf")},
    )
    assert resp.status_code == 403


def test_ai_failure_still_creates_resume_with_failed_status(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(should_fail=True))

    resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Some resume text."), "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["parse_status"] == "failed"
    assert body["analysis"] is None


def test_get_resume_analysis(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    upload_resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Some resume text."), "application/pdf")},
        headers=auth_headers,
    )
    resume_id = upload_resp.json()["id"]

    resp = client.get(f"/resumes/{resume_id}/analysis", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["skills"] == ["Python", "SQL"]


def test_get_resume_analysis_not_found(client, auth_headers):
    resp = client.get("/resumes/999999/analysis", headers=auth_headers)
    assert resp.status_code == 404


def test_get_resume_analysis_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    upload_resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Some resume text."), "application/pdf")},
        headers=auth_headers,
    )
    resume_id = upload_resp.json()["id"]

    client.post("/auth/signup", json={"email": "other@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get(
        f"/resumes/{resume_id}/analysis", headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404
