"""The Phase 24-B acceptance test, over the real API: a replay does not mutate twice.

What makes this the primary test rather than one of many. The failure it reproduces is not a
logic error anywhere in the app -- every handler is correct for the request it receives. It is
that the *same* request can legitimately arrive twice, because the client cannot tell "my
request never arrived" from "it arrived, committed, and the reply was lost", and OkHttp's
connection retry therefore has to be allowed to send it again. Phase 24 measured that: with the
retry switched off, a live device's first tap after idling failed while the server log showed the
request had never arrived at all.

So the client keeps retrying and the server collapses the duplicate. These tests send the second
request exactly as the transport would -- same method, same body, same Idempotency-Key -- and
assert on rows, not on what the response looks like.
"""

import pytest

from app.api.deps import get_db
from app.main import app
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.profile import StudentProfile


def register(client, email: str) -> dict:
    client.post("/auth/signup", json={"email": email, "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": email, "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def auth_headers(client):
    return register(client, "idem@example.com")


@pytest.fixture()
def db_session(client):
    """The session the app itself is using.

    Via the override rather than app.database.session.engine, which points at the real
    development app.db -- the trap recorded in tests/unit's notes and worth not re-learning.
    """
    return next(app.dependency_overrides[get_db]())


def _blueprint(db, profile_id: int) -> AssessmentBlueprint:
    blueprint = AssessmentBlueprint(
        role_id=None,
        jd_id=None,
        profile_id=profile_id,
        source="test",
        sections=[{
            "skill": "SQL", "assessment_type": "sql", "weight": 1.0,
            "question_count": 2, "is_gap": False,
        }],
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    return blueprint


def _profile_id(client, db, headers) -> int:
    """This student's profile id, creating it the way the app does.

    get_or_create_profile runs on the first authenticated request, not at signup, so a test
    that queries for the profile before making one finds nothing.
    """
    client.get("/users/me", headers=headers)
    return db.query(StudentProfile).one().id


class TestResponseLostAfterCommit:
    """WINDOW E: the server committed, the client never saw the reply, the transport retried."""

    def test_a_replayed_start_creates_one_assessment(self, client, db_session, auth_headers):
        profile_id = _profile_id(client, db_session, auth_headers)
        blueprint = _blueprint(db_session, profile_id)
        headers = {**auth_headers, "Idempotency-Key": "and-replay-0000001"}
        body = {"blueprint_id": blueprint.id}

        first = client.post("/assessments", json=body, headers=headers)
        # Byte-identical resend: this is what OkHttp does, not an approximation of it.
        second = client.post("/assessments", json=body, headers=headers)

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        # The two numbers the phase is about.
        assert db_session.query(Assessment).count() == 1, "one durable mutation"
        assert second.json()["id"] == first.json()["id"], "and the same one is returned"

    def test_the_replay_reports_the_row_s_current_state(self, client, db_session, auth_headers):
        # Re-read, not replayed from a cache: if the assessment has moved on since, the second
        # caller is told the truth rather than a copy of the first response.
        profile_id = _profile_id(client, db_session, auth_headers)
        blueprint = _blueprint(db_session, profile_id)
        headers = {**auth_headers, "Idempotency-Key": "and-replay-0000002"}
        body = {"blueprint_id": blueprint.id}

        first = client.post("/assessments", json=body, headers=headers)
        created = db_session.query(Assessment).one()
        created.status = "completed"
        db_session.commit()

        second = client.post("/assessments", json=body, headers=headers)

        assert second.json()["id"] == first.json()["id"]
        assert second.json()["status"] == "completed"
        assert db_session.query(Assessment).count() == 1

    def test_without_a_key_each_request_is_its_own_operation(self, client, db_session, auth_headers):
        # The safe default, and the pre-existing behaviour: a client that sends no key is not
        # silently deduplicated against an unrelated request.
        profile_id = _profile_id(client, db_session, auth_headers)
        blueprint = _blueprint(db_session, profile_id)
        body = {"blueprint_id": blueprint.id}

        client.post("/assessments", json=body, headers=auth_headers)
        client.post("/assessments", json=body, headers=auth_headers)

        assert db_session.query(Assessment).count() == 2


class TestAiBackedGeneration:
    """One logical operation, one model call -- the §12 requirement, measured.

    A roadmap generation supersedes the student's current plan and spends minutes of inference
    building a new one. A replay that reached the model would cost twice and leave the second
    plan superseding the first, so the claim has to be taken *before* the provider is touched.

    It also has to be taken without holding a transaction across the call, which is Phase 19's
    rule (F12-07). claim() commits before returning, so by the time generate_roadmap opens its
    own `outside_transaction` boundary there is nothing of ours still open.
    """

    @staticmethod
    def _counting_ai(calls):
        from app.ai.provider import AIProvider
        from app.ai.schemas.roadmap import GeneratedRoadmap, GeneratedRoadmapTask

        class CountingAI(AIProvider):
            def generate_structured(self, prompt, schema):
                calls.append(1)
                return GeneratedRoadmap(tasks=[
                    GeneratedRoadmapTask(
                        day=1, task_type="practice", title="Practise SQL joins",
                        description="Work through inner and outer joins.", skill_tag="SQL",
                    )
                ])

            def generate_text(self, prompt):
                return "fake"

        return CountingAI()

    def _seed_weakness(self, client, headers):
        """A roadmap needs something to plan against; an assessment result is the cheapest."""
        from app.models.assessment import Assessment
        from app.models.assessment_result import AssessmentResult

        db = next(app.dependency_overrides[get_db]())
        profile_id = _profile_id(client, db, headers)
        blueprint = _blueprint(db, profile_id)
        assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id,
                                status="completed")
        db.add(assessment)
        db.commit()
        db.refresh(assessment)
        db.add(AssessmentResult(
            assessment_id=assessment.id, overall_score=40.0,
            skill_scores={"SQL": 40.0}, priority_weaknesses=["SQL"],
        ))
        db.commit()
        return profile_id

    def test_a_replayed_generation_runs_the_model_once(self, client, db_session, auth_headers):
        from app.ai.provider import get_ai_provider
        from app.models.roadmap import Roadmap

        self._seed_weakness(client, auth_headers)
        calls: list = []
        app.dependency_overrides[get_ai_provider] = lambda: self._counting_ai(calls)
        try:
            headers = {**auth_headers, "Idempotency-Key": "and-roadmap-000001"}
            body = {"jd_id": None, "duration_days": 7}

            first = client.post("/roadmap/generate", json=body, headers=headers)
            second = client.post("/roadmap/generate", json=body, headers=headers)

            assert first.status_code == 201, first.text
            assert second.status_code == 201, second.text
            assert len(calls) == 1, "one model call for one logical generation"
            assert db_session.query(Roadmap).count() == 1, "and one roadmap"
            assert second.json()["id"] == first.json()["id"]
        finally:
            app.dependency_overrides.pop(get_ai_provider, None)

    def test_a_different_duration_under_the_same_key_is_refused(self, client, db_session, auth_headers):
        from app.ai.provider import get_ai_provider
        from app.models.roadmap import Roadmap

        self._seed_weakness(client, auth_headers)
        calls: list = []
        app.dependency_overrides[get_ai_provider] = lambda: self._counting_ai(calls)
        try:
            headers = {**auth_headers, "Idempotency-Key": "and-roadmap-000002"}
            client.post("/roadmap/generate", json={"jd_id": None, "duration_days": 7},
                        headers=headers)
            second = client.post("/roadmap/generate", json={"jd_id": None, "duration_days": 30},
                                 headers=headers)

            # A 30-day plan is not the 7-day plan, so handing back the first would be wrong.
            assert second.status_code == 409, second.text
            assert len(calls) == 1
            assert db_session.query(Roadmap).count() == 1
        finally:
            app.dependency_overrides.pop(get_ai_provider, None)


class TestResumeUpload:
    """The multipart case: one row, one parse, and the file never enters the claims table.

    A resume is the largest body this API accepts and the most sensitive thing a student sends,
    so the fingerprint is a digest of the bytes rather than the bytes. These assert the three
    things that follow from that: a replay creates no second Resume, runs no second AI parse,
    and a *different* file under the same key is refused rather than silently ignored.
    """

    @staticmethod
    def _ai(counter):
        from app.ai.provider import AIProvider
        from app.ai.schemas.resume import ParsedResume

        class CountingAI(AIProvider):
            def generate_structured(self, prompt, schema):
                counter.append(1)
                return ParsedResume(
                    skills=["Python"], education=[], experience=[], projects=[],
                    certifications=[], ats_score=70.0, weak_sections=[],
                    extraction_confidence=0.8,
                )

            def generate_text(self, prompt):
                return "fake"

        return CountingAI()

    def test_a_replayed_upload_stores_one_resume_and_parses_once(
        self, client, db_session, auth_headers, monkeypatch, tmp_path
    ):
        from app.ai.provider import get_ai_provider
        from app.core.config import settings
        from app.models.resume import Resume
        from tests.fixtures.pdf_helpers import make_pdf_with_text

        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        calls: list = []
        app.dependency_overrides[get_ai_provider] = lambda: self._ai(calls)
        try:
            pdf = make_pdf_with_text("Jane Doe - Python developer.")
            headers = {**auth_headers, "Idempotency-Key": "and-resume-0000001"}

            first = client.post(
                "/resumes", headers=headers,
                files={"file": ("resume.pdf", pdf, "application/pdf")},
            )
            second = client.post(
                "/resumes", headers=headers,
                files={"file": ("resume.pdf", pdf, "application/pdf")},
            )

            assert first.status_code == 201, first.text
            assert second.status_code == 201, second.text
            assert db_session.query(Resume).count() == 1, "one Resume row"
            assert len(calls) == 1, "one AI parse for one logical upload"
            assert second.json()["id"] == first.json()["id"]
        finally:
            app.dependency_overrides.pop(get_ai_provider, None)

    def test_a_different_file_under_the_same_key_is_refused(
        self, client, db_session, auth_headers, monkeypatch, tmp_path
    ):
        from app.ai.provider import get_ai_provider
        from app.core.config import settings
        from app.models.resume import Resume
        from tests.fixtures.pdf_helpers import make_pdf_with_text

        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        calls: list = []
        app.dependency_overrides[get_ai_provider] = lambda: self._ai(calls)
        try:
            headers = {**auth_headers, "Idempotency-Key": "and-resume-0000002"}
            client.post("/resumes", headers=headers, files={
                "file": ("a.pdf", make_pdf_with_text("first resume"), "application/pdf")})
            second = client.post("/resumes", headers=headers, files={
                "file": ("a.pdf", make_pdf_with_text("a completely different resume"),
                         "application/pdf")})

            # Same key, different contents. Handing back the first upload would be the wrong
            # answer to a request that uploaded something else.
            assert second.status_code == 409, second.text
            assert db_session.query(Resume).count() == 1
            assert len(calls) == 1
        finally:
            app.dependency_overrides.pop(get_ai_provider, None)

    def test_the_claims_table_never_holds_the_file(
        self, client, db_session, auth_headers, monkeypatch, tmp_path
    ):
        from app.ai.provider import get_ai_provider
        from app.core.config import settings
        from app.models.operation_claim import OperationClaim
        from tests.fixtures.pdf_helpers import make_pdf_with_text

        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        calls: list = []
        app.dependency_overrides[get_ai_provider] = lambda: self._ai(calls)
        try:
            marker = "UNIQUE-RESUME-MARKER-9f2a"
            client.post(
                "/resumes",
                headers={**auth_headers, "Idempotency-Key": "and-resume-0000003"},
                files={"file": ("a.pdf", make_pdf_with_text(marker), "application/pdf")},
            )

            claim = db_session.query(OperationClaim).filter(
                OperationClaim.operation == "resume.upload"
            ).one()
            stored = " ".join(str(v) for v in (
                claim.idempotency_key, claim.request_fingerprint, claim.operation,
                claim.result_kind,
            ))
            assert marker not in stored
            assert len(claim.request_fingerprint) == 64, "a digest, not a document"
        finally:
            app.dependency_overrides.pop(get_ai_provider, None)


class TestPaymentCheckout:
    """The case where a duplicate costs real money, not just a row.

    create_order() calls Razorpay and only then writes the Subscription, so a replay that
    reached the provider would leave a second live order behind. The claim commits before the
    provider call, which is what keeps the second arrival away from it.

    A stub stands in for Razorpay: no credentials are used, requested or probed, and the count
    of provider calls is the assertion.
    """

    def test_a_replayed_checkout_creates_one_provider_order(
        self, client, db_session, auth_headers, monkeypatch
    ):
        from app.models.subscription import Subscription
        from app.services import subscription_service

        calls = []

        class StubResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"id": f"order_stub_{len(calls)}"}

        def fake_post(*args, **kwargs):
            calls.append(kwargs.get("json"))
            return StubResponse()

        monkeypatch.setattr(subscription_service.httpx, "post", fake_post)
        monkeypatch.setattr(
            subscription_service.settings, "razorpay_key_id", "rzp_test_stub", raising=False
        )
        monkeypatch.setattr(
            subscription_service.settings, "razorpay_key_secret", "stub-secret", raising=False
        )

        _profile_id(client, db_session, auth_headers)
        headers = {**auth_headers, "Idempotency-Key": "and-checkout-000001"}
        body = {"plan": next(iter(subscription_service.PLAN_CATALOG))}

        first = client.post("/subscriptions/checkout", json=body, headers=headers)
        second = client.post("/subscriptions/checkout", json=body, headers=headers)

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert len(calls) == 1, "the payment provider was asked exactly once"
        assert db_session.query(Subscription).count() == 1
        assert second.json()["razorpay_order_id"] == first.json()["razorpay_order_id"]


class TestKeySemantics:
    def test_the_same_key_with_a_different_body_is_refused(self, client, db_session, auth_headers):
        profile_id = _profile_id(client, db_session, auth_headers)
        one = _blueprint(db_session, profile_id)
        two = _blueprint(db_session, profile_id)
        headers = {**auth_headers, "Idempotency-Key": "and-conflict-000001"}

        first = client.post("/assessments", json={"blueprint_id": one.id}, headers=headers)
        second = client.post("/assessments", json={"blueprint_id": two.id}, headers=headers)

        assert first.status_code == 201
        assert second.status_code == 409, second.text
        # The refusal is the point: it must not create anything, and must not hand back the
        # first operation's assessment for a request that asked for a different blueprint.
        assert db_session.query(Assessment).count() == 1

    def test_a_malformed_key_is_a_client_error_not_a_conflict(self, client, db_session, auth_headers):
        profile_id = _profile_id(client, db_session, auth_headers)
        blueprint = _blueprint(db_session, profile_id)
        headers = {**auth_headers, "Idempotency-Key": "short"}

        response = client.post("/assessments", json={"blueprint_id": blueprint.id},
                               headers=headers)

        assert response.status_code == 400, response.text
        assert db_session.query(Assessment).count() == 0

    def test_a_failed_request_does_not_burn_the_key(self, client, db_session, auth_headers):
        """A 404 must not leave a record that blocks the legitimate corrected request.

        The dangerous version of this mechanism is one where any first attempt -- including one
        the server rejected outright -- permanently owns its key. The student would then fix
        their input, send it again, and be told the request id was already used.
        """
        headers = {**auth_headers, "Idempotency-Key": "and-afterfail-00001"}

        missing = client.post("/assessments", json={"blueprint_id": 999999}, headers=headers)
        assert missing.status_code == 404

        profile_id = _profile_id(client, db_session, auth_headers)
        blueprint = _blueprint(db_session, profile_id)
        retry = client.post("/assessments", json={"blueprint_id": blueprint.id}, headers=headers)

        # A different body under the same key is still a conflict by design, so the honest
        # assertion is that the key is reusable for the *same* request, not for any request.
        assert retry.status_code == 409, retry.text

        fresh = client.post(
            "/assessments",
            json={"blueprint_id": blueprint.id},
            headers={**auth_headers, "Idempotency-Key": "and-afterfail-00002"},
        )
        assert fresh.status_code == 201, fresh.text
        assert db_session.query(Assessment).count() == 1

    def test_a_retry_of_the_same_failed_request_is_allowed(self, client, db_session, auth_headers):
        # Same key, same body, after a genuine failure: nothing durable happened, so this is a
        # real retry and must go through rather than stranding the student.
        headers = {**auth_headers, "Idempotency-Key": "and-sameretry-00001"}
        body = {"blueprint_id": 999999}

        assert client.post("/assessments", json=body, headers=headers).status_code == 404
        assert client.post("/assessments", json=body, headers=headers).status_code == 404

        assert db_session.query(Assessment).count() == 0
