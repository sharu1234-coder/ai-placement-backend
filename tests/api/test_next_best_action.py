import hashlib
import hmac

import httpx
import pytest

from app.api.deps import get_db
from app.core.config import settings
from app.main import app
from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.profile import StudentProfile
from app.models.user import User
from app.services import subscription_service as svc
from app.services.career_strategy.gaps import _CATEGORY_DESTINATION

# Every destination Career Strategy can emit. Next Best Action reuses that vocabulary rather
# than inventing a second one, so anything it returns must already be in this table -- which is
# what guarantees the Android destination map can resolve it to a real route.
DESTINATIONS = set(_CATEGORY_DESTINATION.values()) | {"practice"}


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "nba@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "nba@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "nba2@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "nba2@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sde_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    return next(r["id"] for r in roles if r["name"] == "SDE")


def test_requires_auth(client):
    assert client.get("/next-best-action").status_code == 401


def test_no_role_has_no_recommendation(client, auth_headers):
    response = client.get("/next-best-action", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["has_recommendation"] is False
    assert body["tier"] is None


def test_target_role_with_no_evidence_yields_critical_role_gap(client, auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    response = client.get("/next-best-action", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["has_recommendation"] is True
    assert body["tier"] == "critical_role_gap"
    assert body["estimated_minutes"] is not None
    assert len(body["reasons"]) > 0


def test_isolated_per_profile(client, auth_headers, other_auth_headers):
    role_id = _sde_role_id(client, auth_headers)
    client.post("/users/me/role", json={"role_id": role_id}, headers=auth_headers)

    mine = client.get("/next-best-action", headers=auth_headers).json()
    theirs = client.get("/next-best-action", headers=other_auth_headers).json()
    assert mine["has_recommendation"] is True
    assert theirs["has_recommendation"] is False


# --- The ranked plan, and who may read it ---------------------------------------------------
# The top action is free: what to work on next is the product's promise, and a student who
# cannot see it has no reason to subscribe. The rest of the plan is Premium, enforced here
# rather than only in the client -- PaywallGate hides UI, and an authenticated free account can
# call this route directly with a copied token.


def _grant_premium(client, monkeypatch, headers, payment_id="pay_nba"):
    """The suite's established way to put a real, paid subscription row behind an account."""
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake_key_id")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_test_secret")
    fake_request = httpx.Request("POST", svc.RAZORPAY_ORDERS_URL)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: httpx.Response(
            200, json={"id": f"order_{payment_id}", "status": "created"}, request=fake_request
        ),
    )
    checkout = client.post("/subscriptions/checkout", json={"plan": "1_month"}, headers=headers).json()
    signature = hmac.new(
        settings.razorpay_key_secret.encode("utf-8"),
        f"{checkout['razorpay_order_id']}|{payment_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    verify = client.post(
        "/subscriptions/verify",
        json={
            "razorpay_order_id": checkout["razorpay_order_id"],
            "razorpay_payment_id": payment_id,
            "razorpay_signature": signature,
        },
        headers=headers,
    )
    assert verify.status_code == 200 and verify.json()["tier"] == "premium"


def _weak_interview(client, headers, email="nba@example.com"):
    """A second, independent signal, so the ladder has more than one tier to speak from.

    Written straight to the database because the HTTP route for it runs a real interview
    evaluation through the AI provider -- this test is about entitlement, not about grading.
    """
    # Same idiom the rest of this suite uses to reach past HTTP for a session.
    db_session = next(app.dependency_overrides[get_db]())
    user = db_session.query(User).filter(User.email == email).one()
    profile_id = db_session.query(StudentProfile).filter(StudentProfile.user_id == user.id).one().id
    interview = Interview(profile_id=profile_id, type="technical", mode="text", status="completed")
    db_session.add(interview)
    db_session.commit()
    db_session.refresh(interview)
    db_session.add(
        InterviewEvaluation(
            interview_id=interview.id,
            technical_correctness=0.3,
            relevance=0.9,
            completeness=0.9,
            reasoning=0.9,
            structure=0.9,
            clarity=0.9,
            role_jd_coverage=0.9,
            overall_score=0.7,
            feedback_text="ok",
        )
    )
    db_session.commit()


def test_free_student_keeps_the_top_action_but_not_the_rest(client, auth_headers):
    client.post("/users/me/role", json={"role_id": _sde_role_id(client, auth_headers)}, headers=auth_headers)
    _weak_interview(client, auth_headers)

    body = client.get("/next-best-action", headers=auth_headers).json()

    assert body["has_recommendation"] is True
    assert body["additional_actions"] == []
    assert body["locked_sections"] == ["additional_actions"]


def test_premium_student_gets_the_ranked_remainder(client, auth_headers, monkeypatch):
    client.post("/users/me/role", json={"role_id": _sde_role_id(client, auth_headers)}, headers=auth_headers)
    _weak_interview(client, auth_headers)
    _grant_premium(client, monkeypatch, auth_headers)

    body = client.get("/next-best-action", headers=auth_headers).json()

    assert body["locked_sections"] == []
    assert len(body["additional_actions"]) >= 1
    assert all(action["tier"] for action in body["additional_actions"])
    # The same subject never appears twice across the whole plan.
    skills = [body["skill"]] + [a["skill"] for a in body["additional_actions"]]
    named = [s for s in skills if s]
    assert len(named) == len(set(named))


def test_no_upgrade_prompt_when_there_is_nothing_further_to_unlock(client, auth_headers):
    """A student with one real recommendation is never sold a plan that does not exist."""
    client.post("/users/me/role", json={"role_id": _sde_role_id(client, auth_headers)}, headers=auth_headers)

    body = client.get("/next-best-action", headers=auth_headers).json()

    assert body["additional_actions"] == []
    assert body["locked_sections"] == []


def test_every_action_carries_its_confidence_and_a_real_destination(client, auth_headers):
    client.post("/users/me/role", json={"role_id": _sde_role_id(client, auth_headers)}, headers=auth_headers)

    body = client.get("/next-best-action", headers=auth_headers).json()

    assert body["confidence"] in ("low", "moderate", "high")
    assert body["destination"] in DESTINATIONS
