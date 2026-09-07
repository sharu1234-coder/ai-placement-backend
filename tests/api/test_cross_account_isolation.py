"""Cross-account isolation for the routes that had no runtime proof of it.

Why this file exists. Every feature in this app already had *one* cross-account test --
test_assessment_isolated_per_user, test_interview_isolated_per_user,
test_challenge_isolated_per_user, and so on. Each creates a resource as user A and checks that
user B gets a 404 from a single endpoint, almost always the feature's *read* entry point
(`/next-question`, or a topic's `/start`).

That leaves the sibling routes on the same resource id unproven, and the untested siblings are
the ones that mutate: submitting an answer into someone else's assessment or interview,
retrieving their synthesized question audio, matching against their job description. Each was
verified secure by reading the service layer -- every one routes through a `_get_owned_*`
helper before touching the resource -- but "secure by inspection" and "proven by a request"
are different claims, and only one of them survives a refactor that moves an ownership check.

The convention asserted here is the codebase's own: a nested resource owned by someone else is
indistinguishable from one that does not exist, so it is 404, never 403. A 403 would confirm to
an attacker that the id is real and belongs to somebody. See test_auth.py's contract test for
the cases where authorization denial genuinely *is* 403.
"""

import io

import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.assessment import AnswerGrade, GeneratedQuestion
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.resume import ParsedResume
from app.main import app

_MCQ = GeneratedQuestion(
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
_PARSED_JD = AIParsedJD(
    role_title="SDE",
    required_skills=[JDRequirementItem(value="SQL", is_explicit=True)],
    preferred_skills=[],
    responsibilities=[],
    tools_technologies=[],
    experience_expectation=None,
    education_eligibility=None,
    other_signals=[],
    parsing_confidence=0.9,
)
_PARSED_RESUME = ParsedResume(
    skills=["DSA", "OOP", "SQL"],
    education=[],
    experience=[],
    projects=[],
    certifications=[],
    ats_score=70.0,
    weak_sections=[],
    extraction_confidence=0.9,
)


class _FakeAI(AIProvider):
    """Answers whatever schema a route asks for, so one fake serves every feature below."""

    def generate_structured(self, prompt, schema):
        if schema is AIParsedJD:
            return _PARSED_JD
        if schema is ParsedResume:
            return _PARSED_RESUME
        if schema is GeneratedQuestion:
            return _MCQ
        if schema is AnswerGrade:
            return AnswerGrade(is_correct=True, score=0.8, feedback="Good answer.")
        # Interview question generation uses its own schema; construct it lazily so this file
        # does not need to import every AI schema module just to name one type.
        if schema.__name__ == "GeneratedInterviewQuestion":
            return schema(question_text="Tell me about a project you built.", based_on="Project")
        if schema.__name__ == "GeneratedPlacementPathExplanations":
            # Placement Path asks for per-topic explanation text. An empty list is a valid
            # response the service already handles (it falls back to its hand-authored
            # template per topic), and none of these tests assert on explanation copy.
            return schema(explanations=[])
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"

    def transcribe(self, audio: bytes) -> str:
        return "a transcribed answer"

    def synthesize_speech(self, text: str) -> bytes:
        return b"RIFF0000WAVEfake"


@pytest.fixture()
def fake_ai():
    provider = _FakeAI()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


def _register(client, email):
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def owner(client):
    return _register(client, "owner@example.com")


@pytest.fixture()
def intruder(client):
    """A fully legitimate, authenticated account. The point is that authentication alone must
    never be enough to reach another account's rows."""
    return _register(client, "intruder@example.com")


@pytest.fixture()
def sde_role_id(client):
    return next(r["id"] for r in client.get("/roles").json() if r["name"] == "SDE")


@pytest.fixture()
def owner_resume(client, owner):
    """A completed Resume + ResumeAnalysis inserted directly for the owner account.

    Placement Alarm and Placement Path both gate on having an analyzed resume, and real PDF
    parsing is not what these tests are about. Same technique test_placement_alarm.py and
    test_placement_path.py already use for their own fixtures.
    """
    from app.api.deps import get_db
    from app.models.profile import StudentProfile
    from app.models.resume import Resume
    from app.models.resume_analysis import ResumeAnalysis
    from app.models.user import User

    db = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == "owner@example.com").first()
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if profile is None:
        profile = StudentProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        db.refresh(profile)

    resume = Resume(
        profile_id=profile.id, file_path="x.pdf", raw_text="text", parse_status="completed"
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=["SQL", "Python", "Machine Learning"],
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=70.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()
    # Read the id out before closing: after db.close() the instance is detached and touching
    # any attribute raises DetachedInstanceError.
    resume_id = resume.id
    db.close()
    return resume_id


def _assert_denied(response, route):
    """404, not 403: an owned-by-someone-else id must be indistinguishable from a missing one."""
    assert response.status_code == 404, (
        f"{route} returned {response.status_code} to a different account; expected 404. "
        f"Body: {response.text[:200]}"
    )


# --------------------------------------------------------------------------- assessments


def test_intruder_cannot_answer_or_read_another_accounts_assessment(
    client, owner, intruder, sde_role_id, fake_ai
):
    """test_assessment_isolated_per_user covers /next-question only. The write path matters
    more: submitting an answer mutates the owner's attempt rows and therefore their score."""
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=owner
    ).json()["id"]
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=owner
    ).json()["id"]
    question = client.get(
        f"/assessments/{assessment_id}/next-question", headers=owner
    ).json()["question"]

    _assert_denied(
        client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": question["id"], "student_answer": "Structured Query Language"},
            headers=intruder,
        ),
        "POST /assessments/{id}/answer",
    )
    _assert_denied(
        client.get(f"/assessments/{assessment_id}/result", headers=intruder),
        "GET /assessments/{id}/result",
    )


# --------------------------------------------------------------------------- interviews


def test_intruder_cannot_answer_or_read_another_accounts_interview(
    client, owner, intruder, sde_role_id, fake_ai
):
    client.post("/users/me/role", json={"role_id": sde_role_id}, headers=owner)
    interview_id = client.post("/interviews", json={}, headers=owner).json()["id"]
    question = client.get(
        f"/interviews/{interview_id}/next-question", headers=owner
    ).json()["question"]

    _assert_denied(
        client.post(
            f"/interviews/{interview_id}/answer",
            json={"interview_question_id": question["id"], "answer_text": "An answer."},
            headers=intruder,
        ),
        "POST /interviews/{id}/answer",
    )
    _assert_denied(
        client.get(f"/interviews/{interview_id}/evaluation", headers=intruder),
        "GET /interviews/{id}/evaluation",
    )


def test_intruder_cannot_retrieve_another_accounts_question_audio(
    client, owner, intruder, sde_role_id, fake_ai
):
    """Voice is the most sensitive content this app handles. The audio routes were covered for
    the owner and for an unknown question id, but never for a different real account."""
    client.post("/users/me/role", json={"role_id": sde_role_id}, headers=owner)
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=owner).json()["id"]
    question = client.get(
        f"/interviews/{interview_id}/next-question", headers=owner
    ).json()["question"]

    _assert_denied(
        client.get(
            f"/interviews/{interview_id}/questions/{question['id']}/audio", headers=intruder
        ),
        "GET /interviews/{id}/questions/{qid}/audio",
    )
    # An *accepted* content type is used on purpose. The route's content-type allowlist and
    # size cap run before the ownership check, so an "audio/wav" upload returns 415 and would
    # prove nothing about authorization. Sending a format the route accepts is what forces the
    # request all the way to the ownership check being asserted here.
    _assert_denied(
        client.post(
            f"/interviews/{interview_id}/answer/audio",
            data={"interview_question_id": str(question["id"])},
            files={"file": ("a.m4a", io.BytesIO(b"\x00\x00\x00\x20ftypM4A fake"), "audio/m4a")},
            headers=intruder,
        ),
        "POST /interviews/{id}/answer/audio",
    )


# --------------------------------------------------------------------------- job descriptions


def test_intruder_cannot_match_or_plan_against_another_accounts_jd(
    client, owner, intruder, fake_ai
):
    jd_id = client.post("/jobs", json={"raw_text": "some jd text"}, headers=owner).json()["id"]

    _assert_denied(client.post(f"/jobs/{jd_id}/match", headers=intruder), "POST /jobs/{id}/match")
    # A *valid* body is sent deliberately. Request-schema validation runs before the ownership
    # check, so omitting the body returns 422 and would prove nothing about authorization --
    # that 422 leaks only that the route exists and the body was wrong, never anything about
    # the resource or its owner. The assertion that matters is the one below it: a
    # well-formed request from a different account still cannot reach the row.
    _assert_denied(
        client.post(
            f"/jobs/{jd_id}/preparation-plan",
            json={"company_name": "Acme", "job_title": "SDE"},
            headers=intruder,
        ),
        "POST /jobs/{id}/preparation-plan",
    )
    _assert_denied(
        client.get(f"/jobs/{jd_id}/preparation-plan", headers=intruder),
        "GET /jobs/{id}/preparation-plan",
    )
    _assert_denied(
        client.get(f"/jobs/{jd_id}/requirements", headers=intruder),
        "GET /jobs/{id}/requirements",
    )
    _assert_denied(
        client.delete(f"/jobs/{jd_id}", headers=intruder),
        "DELETE /jobs/{id}",
    )


# --------------------------------------------------------------------------- placement alarm


def test_intruder_cannot_answer_another_accounts_alarm_challenge(
    client, owner, intruder, owner_resume, fake_ai
):
    """test_challenge_isolated_per_user covers the GET. This covers the write, which advances
    the owner's attempt counter and can mark their challenge answered correctly."""
    created = client.post(
        "/placement-alarm/challenges", json={"client_alarm_id": "alarm-1"}, headers=owner
    )
    assert created.status_code == 201, created.text
    challenge_id = created.json()["challenge_id"]
    state = client.get(f"/placement-alarm/challenges/{challenge_id}", headers=owner).json()
    question_id = state["current_question"]["id"]

    _assert_denied(
        client.post(
            f"/placement-alarm/challenges/{challenge_id}/answer",
            json={"question_id": question_id, "student_answer": "Structured Query Language"},
            headers=intruder,
        ),
        "POST /placement-alarm/challenges/{id}/answer",
    )


# --------------------------------------------------------------------------- placement path


def test_intruder_cannot_mutate_another_accounts_path_topic(
    client, owner, intruder, owner_resume, fake_ai
):
    """test_topic_isolated_per_user covers /start. /practice and the PATCH are the ones that
    change the owner's recorded progress."""
    body = client.post("/placement-path/generate", headers=owner)
    assert body.status_code in (200, 201), body.text
    topics = [t for stage in body.json()["stages"] for t in stage["topics"]]
    topic_id = topics[0]["id"]

    _assert_denied(
        client.post(f"/placement-path/topics/{topic_id}/practice", headers=intruder),
        "POST /placement-path/topics/{id}/practice",
    )
    _assert_denied(
        client.patch(
            f"/placement-path/topics/{topic_id}", json={"status": "completed"}, headers=intruder
        ),
        "PATCH /placement-path/topics/{id}",
    )
