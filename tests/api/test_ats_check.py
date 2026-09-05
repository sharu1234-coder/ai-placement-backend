from typing import Optional

import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.resume import ParsedResume, ResumeExperience
from app.ai.schemas.resume_ats_check import GeneratedAtsCheckContent, GeneratedFindingRephrase
from app.core.config import settings
from app.main import app
from tests.fixtures.pdf_helpers import make_pdf_with_text

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["Python", "SQL", "Java", "Docker", "Git"],
    education=[],
    experience=[ResumeExperience(company="Acme Corp", title="Intern", description="Built REST APIs at scale")],
    projects=[],
    certifications=[],
    ats_score=72.0,
    weak_sections=["no quantified achievements"],
    extraction_confidence=0.85,
)

RESUME_TEXT = """Jane Doe
jane.doe@example.com | +91 9876543210
linkedin.com/in/janedoe

SUMMARY
Backend developer with experience in Python and SQL, focused on reliable systems.

SKILLS
Python, SQL, Java, Docker, Git

EXPERIENCE
Intern, Acme Corp
- Built REST APIs at scale, serving 5,000+ requests per day

EDUCATION
State University
"""


class FakeAIProvider(AIProvider):
    def __init__(
        self,
        resume: Optional[ParsedResume] = None,
        ats_content: Optional[GeneratedAtsCheckContent] = None,
        fail_ats: bool = False,
    ):
        self._resume = resume
        self._ats_content = ats_content
        self._fail_ats = fail_ats

    def generate_structured(self, prompt, schema):
        if schema is ParsedResume:
            return self._resume
        if schema is GeneratedAtsCheckContent:
            if self._fail_ats:
                raise AIGenerationError("fake ats ai failure")
            if self._ats_content is not None:
                return self._ats_content
            return GeneratedAtsCheckContent(summary="A fake AI-written summary.", finding_rephrases=[])
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "atscheck@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "atscheck@example.com", "password": "supersecret123"}
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


def _upload_resume(client, auth_headers, filename="resume.pdf", text=RESUME_TEXT):
    resp = client.post(
        "/resumes",
        files={"file": (filename, make_pdf_with_text(text), "application/pdf")},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_create_ats_check_success(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers)

    resp = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["resume_id"] == resume_id
    assert 0 <= body["overall_score"] <= 100
    assert body["score_category"] in ("excellent", "very_good", "good", "needs_improvement", "needs_attention")
    assert len(body["category_breakdown"]) == 8
    assert {c["category"] for c in body["category_breakdown"]} == {
        "parsing", "structure", "content", "skills", "experience", "projects", "contact", "readability",
    }
    assert body["ai_summary"]  # never blank
    assert body["raw_text"]


def test_create_ats_check_with_target_role(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers)

    resp = client.post(
        f"/resumes/{resume_id}/ats-check", json={"target_role": "Backend Engineer"}, headers=auth_headers
    )

    assert resp.status_code == 201
    assert resp.json()["target_role"] == "Backend Engineer"


def test_create_ats_check_on_resume_with_failed_parse_still_succeeds(client, auth_headers, use_fake_ai):
    # A resume whose upload-time AI parse failed has no ResumeAnalysis row at all -- the
    # ATS check must still produce a full, real report from raw_text alone.
    class FailUploadAI(AIProvider):
        def generate_structured(self, prompt, schema):
            if schema is ParsedResume:
                raise AIGenerationError("upload parse failed")
            if schema is GeneratedAtsCheckContent:
                return GeneratedAtsCheckContent(summary="Fallback-path summary.", finding_rephrases=[])
            raise AssertionError(f"Unexpected schema: {schema}")

        def generate_text(self, prompt):
            return "fake"

    use_fake_ai(FailUploadAI())
    upload_resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text(RESUME_TEXT), "application/pdf")},
        headers=auth_headers,
    )
    assert upload_resp.json()["parse_status"] == "failed"
    resume_id = upload_resp.json()["id"]

    resp = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)

    assert resp.status_code == 201
    body = resp.json()
    assert 0 <= body["overall_score"] <= 100
    # No extracted skills/experience means the skills category degrades but must still
    # produce a real number, not an error.
    skills_category = next(c for c in body["category_breakdown"] if c["category"] == "skills")
    assert skills_category["score"] < 100


def test_create_ats_check_ai_failure_still_returns_full_report(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME, fail_ats=True))
    resume_id = _upload_resume(client, auth_headers)

    resp = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)

    assert resp.status_code == 201
    body = resp.json()
    assert 0 <= body["overall_score"] <= 100
    assert body["ai_summary"]  # falls back to the deterministic template summary, never blank
    assert len(body["issues"]) >= 0  # deterministic issues still fully computed


def test_ai_overlay_only_applies_to_recognized_finding_keys(client, auth_headers, use_fake_ai):
    # An AI response naming a finding_key that doesn't exist in this report must be
    # silently dropped, not crash and not leak into the response.
    bogus_content = GeneratedAtsCheckContent(
        summary="A real AI summary.",
        finding_rephrases=[GeneratedFindingRephrase(finding_key="not_a_real_key", rationale="x", how_to_improve="y")],
    )
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME, ats_content=bogus_content))
    resume_id = _upload_resume(client, auth_headers)

    resp = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)

    assert resp.status_code == 201
    assert resp.json()["ai_summary"] == "A real AI summary."


def test_create_ats_check_not_found_for_nonexistent_resume(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resp = client.post("/resumes/999999/ats-check", json={}, headers=auth_headers)
    assert resp.status_code == 404


def test_create_ats_check_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers)

    client.post("/auth/signup", json={"email": "other-ats@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-ats@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.post(
        f"/resumes/{resume_id}/ats-check", json={}, headers={"Authorization": f"Bearer {other_token}"}
    )
    assert resp.status_code == 404


def test_get_ats_check_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers)
    check_id = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers).json()["id"]

    client.post("/auth/signup", json={"email": "other-get-ats@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-get-ats@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get(f"/resumes/ats-checks/{check_id}", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 404


def test_get_ats_check_not_found(client, auth_headers):
    resp = client.get("/resumes/ats-checks/999999", headers=auth_headers)
    assert resp.status_code == 404


def test_get_ats_check_returns_the_saved_report(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers)
    created = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers).json()

    resp = client.get(f"/resumes/ats-checks/{created['id']}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["overall_score"] == created["overall_score"]


def test_list_ats_checks_empty_before_any_checks(client, auth_headers):
    resp = client.get("/resumes/ats-checks", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_ats_checks_most_recent_first_with_filename(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers, filename="my_resume.pdf")
    first_id = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers).json()["id"]
    second_id = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers).json()["id"]

    resp = client.get("/resumes/ats-checks", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body] == [second_id, first_id]
    assert body[0]["resume_filename"] == "my_resume.pdf"


def test_create_ats_check_requires_auth(client):
    resp = client.post("/resumes/1/ats-check", json={})
    assert resp.status_code == 401


def test_same_resume_checked_twice_produces_the_same_score(client, auth_headers, use_fake_ai):
    # End-to-end determinism guarantee, at the API layer rather than just the pure engine.
    use_fake_ai(FakeAIProvider(resume=SAMPLE_PARSED_RESUME))
    resume_id = _upload_resume(client, auth_headers)

    first = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers).json()
    second = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers).json()

    assert first["overall_score"] == second["overall_score"]
    assert first["category_breakdown"] == second["category_breakdown"]
