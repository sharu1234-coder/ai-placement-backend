"""Item 19/20: every AI-calling endpoint must be rate limited (cost/DoS control -- these
calls hit a real paid/local AI provider). One test per endpoint, each calling it just past
its configured limit and checking the next call gets 429. Where the endpoint's own business
logic 404s/422s before ever reaching the AI provider (a nonexistent interview/assessment/
roadmap id, or a fresh profile with no weakness data yet), that's used instead of a fake AI
provider -- slowapi counts every call that reaches the route regardless of what the route
itself returns, so this is a faster, equally valid way to drive the counter."""

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.jd import AIParsedJD
from app.ai.schemas.resume import ParsedResume
from app.ai.schemas.resume_ats_check import GeneratedAtsCheckContent
from app.core.config import settings
from app.main import app

SAMPLE_PARSED_JD = AIParsedJD(
    role_title="Data Analyst Intern",
    required_skills=[],
    preferred_skills=[],
    responsibilities=[],
    tools_technologies=[],
    experience_expectation="0-1 years",
    education_eligibility="Bachelor's degree",
    other_signals=[],
    parsing_confidence=0.9,
)

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["Python"],
    education=[],
    experience=[],
    projects=[],
    certifications=[],
    ats_score=70.0,
    weak_sections=[],
    extraction_confidence=0.9,
)


SAMPLE_ATS_CONTENT = GeneratedAtsCheckContent(summary="A fake AI summary.", finding_rephrases=[])


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            return SAMPLE_PARSED_JD
        if schema is ParsedResume:
            return SAMPLE_PARSED_RESUME
        if schema is GeneratedAtsCheckContent:
            return SAMPLE_ATS_CONTENT
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))


@pytest.fixture(autouse=True)
def use_fake_ai():
    app.dependency_overrides[get_ai_provider] = lambda: FakeAIProvider()
    yield
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "airatelimit@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "airatelimit@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_resume_upload_rate_limited(client, auth_headers):
    from tests.fixtures.pdf_helpers import make_pdf_with_text

    for _ in range(10):
        resp = client.post(
            "/resumes",
            files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
            headers=auth_headers,
        )
        assert resp.status_code == 201

    blocked = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    )
    assert blocked.status_code == 429


def test_create_ats_check_rate_limited(client, auth_headers):
    from tests.fixtures.pdf_helpers import make_pdf_with_text

    resume_id = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("some resume text"), "application/pdf")},
        headers=auth_headers,
    ).json()["id"]

    for _ in range(20):
        resp = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)
        assert resp.status_code == 201

    blocked = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)
    assert blocked.status_code == 429


def test_create_job_description_rate_limited(client, auth_headers):
    for _ in range(20):
        resp = client.post("/jobs", json={"raw_text": "a job description"}, headers=auth_headers)
        assert resp.status_code == 201

    blocked = client.post("/jobs", json={"raw_text": "a job description"}, headers=auth_headers)
    assert blocked.status_code == 429


def test_interview_next_question_rate_limited(client, auth_headers):
    for _ in range(60):
        resp = client.get("/interviews/999999/next-question", headers=auth_headers)
        assert resp.status_code == 404

    blocked = client.get("/interviews/999999/next-question", headers=auth_headers)
    assert blocked.status_code == 429


def test_interview_evaluation_rate_limited(client, auth_headers):
    for _ in range(60):
        resp = client.get("/interviews/999999/evaluation", headers=auth_headers)
        assert resp.status_code == 404

    blocked = client.get("/interviews/999999/evaluation", headers=auth_headers)
    assert blocked.status_code == 429


def test_interview_question_audio_rate_limited(client, auth_headers):
    for _ in range(60):
        resp = client.get("/interviews/999999/questions/999999/audio", headers=auth_headers)
        assert resp.status_code == 404

    blocked = client.get("/interviews/999999/questions/999999/audio", headers=auth_headers)
    assert blocked.status_code == 429


def test_submit_audio_answer_rate_limited(client, auth_headers):
    for _ in range(60):
        resp = client.post(
            "/interviews/999999/answer/audio",
            data={"interview_question_id": "1"},
            files={"file": ("answer.m4a", b"\x00\x00\x00\x00ftyp-fake", "audio/mp4")},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    blocked = client.post(
        "/interviews/999999/answer/audio",
        data={"interview_question_id": "1"},
        files={"file": ("answer.m4a", b"\x00\x00\x00\x00ftyp-fake", "audio/mp4")},
        headers=auth_headers,
    )
    assert blocked.status_code == 429


def test_assessment_next_question_rate_limited(client, auth_headers):
    for _ in range(60):
        resp = client.get("/assessments/999999/next-question", headers=auth_headers)
        assert resp.status_code == 404

    blocked = client.get("/assessments/999999/next-question", headers=auth_headers)
    assert blocked.status_code == 429


def test_assessment_submit_answer_rate_limited(client, auth_headers):
    for _ in range(60):
        resp = client.post(
            "/assessments/999999/answer",
            json={"question_id": 1, "student_answer": "an answer"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    blocked = client.post(
        "/assessments/999999/answer",
        json={"question_id": 1, "student_answer": "an answer"},
        headers=auth_headers,
    )
    assert blocked.status_code == 429


def test_roadmap_generate_rate_limited(client, auth_headers):
    # Fresh profile, no jd_id -> compute_weaknesses finds nothing -> 422 before the AI call
    # is ever made, same fast-rejection-still-counts approach as the other endpoints above.
    for _ in range(10):
        resp = client.post(
            "/roadmap/generate", json={"jd_id": None, "duration_days": 7}, headers=auth_headers
        )
        assert resp.status_code == 422

    blocked = client.post("/roadmap/generate", json={"jd_id": None, "duration_days": 7}, headers=auth_headers)
    assert blocked.status_code == 429


def test_roadmap_reassess_rate_limited(client, auth_headers):
    for _ in range(10):
        resp = client.post("/roadmap/999999/reassess", headers=auth_headers)
        assert resp.status_code == 404

    blocked = client.post("/roadmap/999999/reassess", headers=auth_headers)
    assert blocked.status_code == 429
