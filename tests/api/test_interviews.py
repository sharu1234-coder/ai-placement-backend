import pytest

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.interview import GeneratedInterviewEvaluation, GeneratedInterviewQuestion
from app.main import app

SAMPLE_GENERATED_QUESTION = GeneratedInterviewQuestion(
    question_text="Tell me about a challenging bug you fixed and how you debugged it.",
    based_on="Technical: debugging",
)

SAMPLE_GENERATED_EVALUATION = GeneratedInterviewEvaluation(
    technical_correctness=0.8,
    relevance=0.75,
    completeness=0.7,
    reasoning=0.8,
    structure=0.7,
    clarity=0.85,
    grammar=0.9,
    role_jd_coverage=0.7,
    overall_score=0.78,
    feedback="Solid technical grasp; be more concise and close with a clear conclusion.",
)


SAMPLE_TRANSCRIPT = "Um, so I basically used a debugger and, like, bisected the change."

# Bytes 4:8 spell "ftyp" -- the minimal structural marker submit_audio_answer's magic-byte
# check (app/api/interviews.py) requires of a real M4A/MP4 file. The rest is arbitrary; this
# is a fake payload for testing business logic, not a real audio file.
FAKE_M4A_BYTES = b"\x00\x00\x00\x00ftyp-fake-audio-bytes"


class FakeAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        if schema is GeneratedInterviewQuestion:
            return SAMPLE_GENERATED_QUESTION
        if schema is GeneratedInterviewEvaluation:
            return SAMPLE_GENERATED_EVALUATION
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        return "fake text"

    def transcribe(self, audio: bytes) -> str:
        return SAMPLE_TRANSCRIPT

    def synthesize_speech(self, text: str) -> bytes:
        return b"FAKEWAVDATA"


@pytest.fixture()
def auth_headers(client):
    client.post(
        "/auth/signup", json={"email": "interviews@example.com", "password": "supersecret123"}
    )
    token = client.post(
        "/auth/login", json={"email": "interviews@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


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


@pytest.fixture()
def role_selected(client, auth_headers, sde_role_id):
    client.post("/users/me/role", json={"role_id": sde_role_id}, headers=auth_headers)
    return sde_role_id


def test_start_interview_defaults(client, auth_headers, role_selected):
    resp = client.post("/interviews", json={}, headers=auth_headers)
    assert resp.status_code == 201
    body = resp.json()
    assert body["type"] == "technical"
    assert body["mode"] == "text"
    assert body["jd_id"] is None
    assert body["status"] == "in_progress"


def test_start_interview_voice_mode(client, auth_headers, role_selected):
    resp = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["mode"] == "voice"


def test_start_interview_unknown_jd_rejected(client, auth_headers, role_selected):
    resp = client.post("/interviews", json={"jd_id": 999999}, headers=auth_headers)
    assert resp.status_code == 422


@pytest.mark.parametrize("interview_type", ["technical", "hr", "project", "behavioral", "role_specific"])
def test_start_interview_accepts_all_category_types(client, auth_headers, role_selected, interview_type):
    resp = client.post("/interviews", json={"type": interview_type}, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["type"] == interview_type


def test_start_interview_rejects_unknown_type(client, auth_headers, role_selected):
    resp = client.post("/interviews", json={"type": "made_up"}, headers=auth_headers)
    assert resp.status_code == 422


def test_list_interviews_empty(client, auth_headers):
    resp = client.get("/interviews", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_interviews_returns_most_recent_first_with_no_score_before_evaluation(
    client, auth_headers, role_selected
):
    first_id = client.post("/interviews", json={"type": "technical"}, headers=auth_headers).json()["id"]
    second_id = client.post("/interviews", json={"type": "hr"}, headers=auth_headers).json()["id"]

    resp = client.get("/interviews", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [item["id"] for item in body] == [second_id, first_id]
    assert all(item["overall_score"] is None for item in body)


def test_list_interviews_isolated_per_user(client, auth_headers, role_selected):
    client.post("/interviews", json={"type": "technical"}, headers=auth_headers)

    client.post("/auth/signup", json={"email": "other-interviews@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-interviews@example.com", "password": "supersecret123"}
    ).json()["access_token"]

    resp = client.get("/interviews", headers={"Authorization": f"Bearer {other_token}"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_next_question_generates_first_question(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"type": "technical"}, headers=auth_headers).json()["id"]

    resp = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["completed"] is False
    question = body["question"]
    assert question["order_index"] == 1
    assert question["question_text"]
    assert question["based_on"]


def test_next_question_is_idempotent_while_pending(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]

    first = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
    second = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
    assert first["question"]["id"] == second["question"]["id"]


def test_submit_answer_then_advances_to_next_question(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    answer_resp = client.post(
        f"/interviews/{interview_id}/answer",
        json={"interview_question_id": question["id"], "answer_text": "I used a debugger and bisected the change."},
        headers=auth_headers,
    )
    assert answer_resp.status_code == 200
    assert answer_resp.json()["answer_text"] == "I used a debugger and bisected the change."

    next_resp = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
    assert next_resp["completed"] is False
    assert next_resp["question"]["order_index"] == 2
    assert next_resp["question"]["id"] != question["id"]


def test_answer_rejected_when_already_answered(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    client.post(
        f"/interviews/{interview_id}/answer",
        json={"interview_question_id": question["id"], "answer_text": "First answer."},
        headers=auth_headers,
    )
    resp = client.post(
        f"/interviews/{interview_id}/answer",
        json={"interview_question_id": question["id"], "answer_text": "Second answer."},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_answer_unknown_question_not_found(client, auth_headers, role_selected):
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]
    resp = client.post(
        f"/interviews/{interview_id}/answer",
        json={"interview_question_id": 999999, "answer_text": "Anything"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_question_audio_synthesizes_and_returns_wav(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    resp = client.get(f"/interviews/{interview_id}/questions/{question['id']}/audio", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content == b"FAKEWAVDATA"


def test_question_audio_unknown_question_not_found(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]
    resp = client.get(f"/interviews/{interview_id}/questions/999999/audio", headers=auth_headers)
    assert resp.status_code == 404


def test_submit_audio_answer_rejects_unsupported_content_type(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    resp = client.post(
        f"/interviews/{interview_id}/answer/audio",
        data={"interview_question_id": str(question["id"])},
        files={"file": ("answer.exe", b"not-audio-at-all", "application/octet-stream")},
        headers=auth_headers,
    )
    assert resp.status_code == 415


def test_submit_audio_answer_rejects_spoofed_content_type(client, auth_headers, role_selected, use_fake_ai):
    # Content-Type header says audio/mp4 (would pass the allowlist check), but the actual
    # bytes don't have the "ftyp" structural marker a real M4A/MP4 file starts with -- same
    # spoofing scenario resumes.py's %PDF- check exists to catch.
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    resp = client.post(
        f"/interviews/{interview_id}/answer/audio",
        data={"interview_question_id": str(question["id"])},
        files={"file": ("answer.m4a", b"not actually audio, just text", "audio/mp4")},
        headers=auth_headers,
    )
    assert resp.status_code == 415


def test_submit_audio_answer_transcribes_and_persists(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    resp = client.post(
        f"/interviews/{interview_id}/answer/audio",
        data={"interview_question_id": str(question["id"])},
        files={"file": ("answer.m4a", FAKE_M4A_BYTES, "audio/mp4")},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["answer_text"] == SAMPLE_TRANSCRIPT

    next_resp = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
    assert next_resp["completed"] is False
    assert next_resp["question"]["order_index"] == 2


def test_submit_audio_answer_rejected_when_already_answered(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]
    question = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()["question"]

    client.post(
        f"/interviews/{interview_id}/answer/audio",
        data={"interview_question_id": str(question["id"])},
        files={"file": ("answer.m4a", FAKE_M4A_BYTES, "audio/mp4")},
        headers=auth_headers,
    )
    resp = client.post(
        f"/interviews/{interview_id}/answer/audio",
        data={"interview_question_id": str(question["id"])},
        files={"file": ("answer2.m4a", FAKE_M4A_BYTES, "audio/mp4")},
        headers=auth_headers,
    )
    assert resp.status_code == 409


def test_evaluation_requires_completed_interview(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]
    client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers)

    resp = client.get(f"/interviews/{interview_id}/evaluation", headers=auth_headers)
    assert resp.status_code == 409


def test_interview_isolated_per_user(client, auth_headers, role_selected):
    interview_id = client.post("/interviews", json={}, headers=auth_headers).json()["id"]

    client.post("/auth/signup", json={"email": "other-interview@example.com", "password": "supersecret123"})
    other_token = client.post(
        "/auth/login", json={"email": "other-interview@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.get(f"/interviews/{interview_id}/next-question", headers=other_headers)
    assert resp.status_code == 404


def test_full_interview_lifecycle_reaches_evaluation(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"type": "technical"}, headers=auth_headers).json()["id"]

    answered = 0
    for _ in range(20):
        next_q = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        question = next_q["question"]
        answer_resp = client.post(
            f"/interviews/{interview_id}/answer",
            json={"interview_question_id": question["id"], "answer_text": "A thoughtful, complete answer."},
            headers=auth_headers,
        )
        assert answer_resp.status_code == 200
        answered += 1
    else:
        pytest.fail("Interview did not complete within 20 questions")

    assert answered == 5  # _QUESTIONS_PER_INTERVIEW

    result = client.get(f"/interviews/{interview_id}/evaluation", headers=auth_headers)
    assert result.status_code == 200
    body = result.json()
    assert 0.0 <= body["overall_score"] <= 1.0
    assert body["grammar"] is not None  # text mode
    assert body["speaking_pace"] is None  # voice-only field, None in text mode
    assert body["filler_word_rate"] is None  # voice-only field, None in text mode
    assert body["feedback"]

    # Result is persisted -- a second call returns the same computed values.
    result_again = client.get(f"/interviews/{interview_id}/evaluation", headers=auth_headers)
    assert result_again.json() == body


def test_full_voice_interview_lifecycle_scores_filler_words(client, auth_headers, role_selected, use_fake_ai):
    interview_id = client.post("/interviews", json={"mode": "voice"}, headers=auth_headers).json()["id"]

    for _ in range(20):
        next_q = client.get(f"/interviews/{interview_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        question = next_q["question"]
        answer_resp = client.post(
            f"/interviews/{interview_id}/answer/audio",
            data={"interview_question_id": str(question["id"])},
            files={"file": ("answer.m4a", FAKE_M4A_BYTES, "audio/mp4")},
            headers=auth_headers,
        )
        assert answer_resp.status_code == 200
        assert answer_resp.json()["answer_text"] == SAMPLE_TRANSCRIPT

    result = client.get(f"/interviews/{interview_id}/evaluation", headers=auth_headers)
    assert result.status_code == 200
    body = result.json()
    assert body["grammar"] is None  # voice mode -- grammar isn't judged, per the doc's schema
    assert body["speaking_pace"] is None  # not computable without persisting audio duration
    # SAMPLE_TRANSCRIPT ("Um, so I basically used ... like, bisected ...") has real fillers.
    assert body["filler_word_rate"] > 0.0
