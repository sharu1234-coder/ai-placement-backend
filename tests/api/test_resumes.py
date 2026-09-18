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


def test_get_my_latest_resume_reflects_uploaded_file_size(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))
    pdf_bytes = make_pdf_with_text("Jane Doe resume text for size check.")

    client.post(
        "/resumes",
        files={"file": ("resume.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers,
    )

    resp = client.get("/resumes/me/latest", headers=auth_headers)
    assert resp.json()["file_size_bytes"] == len(pdf_bytes)


def test_get_my_latest_resume_none_before_upload(client, auth_headers):
    resp = client.get("/resumes/me/latest", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() is None


def test_get_my_latest_resume_after_upload(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))
    upload_id = client.post(
        "/resumes",
        files={"file": ("Jane_Resume.pdf", make_pdf_with_text("resume text"), "application/pdf")},
        headers=auth_headers,
    ).json()["id"]

    resp = client.get("/resumes/me/latest", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == upload_id
    assert body["filename"] == "Jane_Resume.pdf"
    assert body["parse_status"] == "completed"


def test_get_my_latest_resume_returns_the_most_recent(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))
    client.post(
        "/resumes",
        files={"file": ("first.pdf", make_pdf_with_text("resume text 1"), "application/pdf")},
        headers=auth_headers,
    )
    second_id = client.post(
        "/resumes",
        files={"file": ("second.pdf", make_pdf_with_text("resume text 2"), "application/pdf")},
        headers=auth_headers,
    ).json()["id"]

    resp = client.get("/resumes/me/latest", headers=auth_headers)
    assert resp.json()["id"] == second_id
    assert resp.json()["filename"] == "second.pdf"


def test_upload_rejects_non_pdf(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resp = client.post(
        "/resumes",
        files={"file": ("resume.txt", b"not a pdf", "text/plain")},
        headers=auth_headers,
    )
    assert resp.status_code == 415


def test_upload_rejects_spoofed_content_type(client, auth_headers, use_fake_ai):
    """Content-Type is entirely client-controlled -- claiming application/pdf on bytes that
    aren't a PDF at all must still be rejected (checked against the actual bytes, not just
    the header the client chose to send)."""
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", b"this is definitely not a pdf", "application/pdf")},
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
    assert resp.status_code == 401


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


def test_delete_resume(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    upload_resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Some resume text."), "application/pdf")},
        headers=auth_headers,
    )
    resume_id = upload_resp.json()["id"]

    resp = client.delete(f"/resumes/{resume_id}", headers=auth_headers)
    assert resp.status_code == 204

    assert client.get(f"/resumes/{resume_id}/analysis", headers=auth_headers).status_code == 404
    # Deleting again should 404 -- it's actually gone, not just hidden.
    assert client.delete(f"/resumes/{resume_id}", headers=auth_headers).status_code == 404


def test_delete_resume_not_found(client, auth_headers):
    resp = client.delete("/resumes/999999", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_resume_isolated_per_user(client, auth_headers, use_fake_ai):
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    upload_resp = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Some resume text."), "application/pdf")},
        headers=auth_headers,
    )
    resume_id = upload_resp.json()["id"]

    client.post("/auth/signup", json={"email": "other-delete@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-delete@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.delete(f"/resumes/{resume_id}", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 404
    assert client.get(f"/resumes/{resume_id}/analysis", headers=auth_headers).status_code == 200


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


def test_get_resume_file_returns_the_uploaded_pdf(client, auth_headers, use_fake_ai):
    """The app previews the student's own resume, so the bytes must come back unchanged."""
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))
    pdf_bytes = make_pdf_with_text("Jane Doe - Python developer.")

    resume_id = client.post(
        "/resumes",
        files={"file": ("my cv.pdf", pdf_bytes, "application/pdf")},
        headers=auth_headers,
    ).json()["id"]

    resp = client.get(f"/resumes/{resume_id}/file", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.content == pdf_bytes
    assert resp.headers["content-type"] == "application/pdf"
    # Shown in an in-app viewer, not downloaded, and under the student's own filename.
    assert resp.headers["content-disposition"] == 'inline; filename="my cv.pdf"'
    # A resume is personal data: no shared cache may keep a copy.
    assert resp.headers["cache-control"] == "private, no-store"


def test_get_resume_file_requires_auth(client):
    assert client.get("/resumes/1/file").status_code == 401


def test_get_resume_file_not_found(client, auth_headers):
    assert client.get("/resumes/999999/file", headers=auth_headers).status_code == 404


def test_get_resume_file_isolated_per_user(client, auth_headers, use_fake_ai):
    """Another student's resume_id must be indistinguishable from one that never existed."""
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resume_id = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Private resume text."), "application/pdf")},
        headers=auth_headers,
    ).json()["id"]

    client.post("/auth/signup", json={"email": "other-file@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-file@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get(f"/resumes/{resume_id}/file", headers={"Authorization": f"Bearer {other_token}"})

    assert resp.status_code == 404
    # 404, not 403: a 403 would confirm the id exists. And the owner still has access.
    assert b"Private resume" not in resp.content
    assert client.get(f"/resumes/{resume_id}/file", headers=auth_headers).status_code == 200


def test_get_resume_file_header_cannot_be_injected_via_filename(client, auth_headers, use_fake_ai):
    """The filename is student-supplied, so it must not be able to forge a second header."""
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resume_id = client.post(
        "/resumes",
        files={
            "file": (
                'evil";\r\nX-Injected: yes\r\n.pdf',
                make_pdf_with_text("Resume text."),
                "application/pdf",
            )
        },
        headers=auth_headers,
    ).json()["id"]

    resp = client.get(f"/resumes/{resume_id}/file", headers=auth_headers)

    assert resp.status_code == 200
    assert "x-injected" not in resp.headers
    disposition = resp.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    assert disposition.count('"') == 2


def test_get_resume_file_reports_unavailable_when_storage_fails(client, auth_headers, use_fake_ai, monkeypatch):
    """A readable DB row whose bytes are gone is an outage, not a missing resume -- and the
    error must not leak the storage key."""
    use_fake_ai(FakeAIProvider(parsed=SAMPLE_PARSED_RESUME))

    resume_id = client.post(
        "/resumes",
        files={"file": ("resume.pdf", make_pdf_with_text("Resume text."), "application/pdf")},
        headers=auth_headers,
    ).json()["id"]

    from app.storage import local_provider

    def _boom(self, key):
        raise OSError(f"cannot read {key}")

    monkeypatch.setattr(local_provider.LocalStorageProvider, "load", _boom)

    resp = client.get(f"/resumes/{resume_id}/file", headers=auth_headers)

    assert resp.status_code == 503
    assert "uploads" not in resp.text and ".pdf" not in resp.text
