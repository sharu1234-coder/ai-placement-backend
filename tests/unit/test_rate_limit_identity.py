"""F-04 and F-05: what a rate limit is keyed by, and what shares a budget.

These build their own tiny FastAPI apps rather than driving the real routers. The behaviour
under test belongs to slowapi's wiring -- which key function it calls, when it calls it
relative to FastAPI's dependency resolution, and whether two endpoints share a counter -- and a
purpose-built app exercises exactly that without needing twenty real AI generations or a
seeded resume per assertion. The real routers are covered by the API tests; what those cannot
show is a second user, a second IP, or the 21st request.

The ordering assumption in app/core/rate_limit.user_or_ip_key is load-bearing and is asserted
here rather than trusted: if a future slowapi release evaluated route limits in the middleware
instead of the decorator's wrapper, the authenticated subject would not be set yet and every
authenticated route would silently fall back to IP keying. test_the_key_function_runs_after
_dependencies is what would catch that.
"""

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.rate_limit import RATE_LIMIT_SUBJECT_ATTR, user_or_ip_key


def _build_app(limit: str = "3/hour"):
    """An app shaped like the real one: same limiter wiring, same middleware, same handler."""
    limiter = Limiter(key_func=user_or_ip_key)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    def authenticate(request: Request) -> int:
        """Stands in for get_current_user: publishes the subject only for a valid token."""
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer user-"):
            return 0
        user_id = int(token.removeprefix("Bearer user-"))
        setattr(request.state, RATE_LIMIT_SUBJECT_ATTR, user_id)
        return user_id

    @app.get("/private")
    @limiter.limit(limit)
    def private(request: Request, user_id: int = Depends(authenticate)):
        return {"user_id": user_id}

    @app.post("/public")
    @limiter.limit(limit)
    def public(request: Request):
        return {"ok": True}

    # Two ways to reach one expensive operation -- the /jobs and /jobs/upload shape.
    @app.post("/parse/text")
    @limiter.shared_limit(limit, scope="jd_parse")
    def parse_text(request: Request, user_id: int = Depends(authenticate)):
        return {"via": "text"}

    @app.post("/parse/pdf")
    @limiter.shared_limit(limit, scope="jd_parse")
    def parse_pdf(request: Request, user_id: int = Depends(authenticate)):
        return {"via": "pdf"}

    # A separate expensive operation, to prove the shared scope does not swallow everything.
    @app.post("/plan")
    @limiter.limit(limit)
    def plan(request: Request, user_id: int = Depends(authenticate)):
        return {"ok": True}

    return app


def _as(user: int) -> dict:
    return {"Authorization": f"Bearer user-{user}"}


@pytest.fixture()
def client():
    return TestClient(_build_app())


class TestTheKeyItself:
    def test_an_authenticated_request_is_keyed_by_user(self):
        request = type("R", (), {"state": type("S", (), {})(), "client": None, "headers": {}})()
        setattr(request.state, RATE_LIMIT_SUBJECT_ATTR, 42)
        assert user_or_ip_key(request) == "user:42"

    def test_user_and_ip_keys_cannot_collide(self):
        """A user id of 42 and an address of "42" must not land in one bucket."""
        authed = type("R", (), {"state": type("S", (), {})(), "client": None, "headers": {}})()
        setattr(authed.state, RATE_LIMIT_SUBJECT_ATTR, 42)
        assert user_or_ip_key(authed).startswith("user:")

    def test_the_key_function_runs_after_dependencies(self):
        """The ordering user_or_ip_key depends on, asserted rather than assumed.

        If slowapi ever evaluated route limits before dependency resolution, the subject would
        be unset and this would silently degrade to IP keying for every logged-in student --
        losing the fix without losing a single test that only checks counting behaviour.

        The recording key function is installed when the limiter is built, not swapped in
        afterwards: each @limiter.limit captures the key function at decoration time, so a
        later reassignment of Limiter._key_func is simply not consulted.
        """
        seen = []

        def recording(request: Request) -> str:
            key = user_or_ip_key(request)
            seen.append(key)
            return key

        limiter = Limiter(key_func=recording)
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        def authenticate(request: Request) -> int:
            setattr(request.state, RATE_LIMIT_SUBJECT_ATTR, 7)
            return 7

        @app.get("/private")
        @limiter.limit("3/hour")
        def private(request: Request, user_id: int = Depends(authenticate)):
            return {"user_id": user_id}

        assert TestClient(app).get("/private").status_code == 200
        assert seen == ["user:7"], f"expected the authenticated subject, got {seen}"


class TestAuthenticatedRoutesAreKeyedByUser:
    def test_two_users_do_not_consume_each_others_budget(self, client):
        """The NAT case: two students on one campus address must not share a bucket."""
        for _ in range(3):
            assert client.get("/private", headers=_as(1)).status_code == 200
        assert client.get("/private", headers=_as(1)).status_code == 429

        # Same client, therefore the same IP. A second student is unaffected.
        assert client.get("/private", headers=_as(2)).status_code == 200

    def test_a_whole_campus_of_students_is_not_locked_out_by_one_of_them(self, client):
        """Generalises the above: the previous IP key made this fail at the 4th student."""
        for user in range(1, 6):
            for _ in range(3):
                assert client.get("/private", headers=_as(user)).status_code == 200
            assert client.get("/private", headers=_as(user)).status_code == 429, (
                f"user {user} should have spent their own budget"
            )

    def test_one_user_cannot_escape_their_budget_by_changing_ip(self, client):
        """The other direction: rotating addresses used to reset the counter entirely."""
        for n in range(3):
            resp = client.get(
                "/private", headers={**_as(9), "X-Forwarded-For": f"10.0.0.{n}"}
            )
            assert resp.status_code == 200
        blocked = client.get(
            "/private", headers={**_as(9), "X-Forwarded-For": "10.0.0.250"}
        )
        assert blocked.status_code == 429, "a new address must not buy a new budget"


class TestUnauthenticatedRoutesFallBackToIp:
    def test_an_unauthenticated_route_still_enforces_a_limit(self, client):
        """Login, signup and password reset have no account to key on -- the address is the
        only identity, and those limits must still bite."""
        for _ in range(3):
            assert client.post("/public").status_code == 200
        assert client.post("/public").status_code == 429

    def test_an_invalid_token_does_not_grant_a_fresh_user_bucket(self, client):
        """A request whose token fails validation publishes no subject, so it is limited by
        address rather than escaping limits altogether."""
        bad = {"Authorization": "Bearer not-a-real-token"}
        for _ in range(3):
            assert client.get("/private", headers=bad).status_code == 200
        assert client.get("/private", headers=bad).status_code == 429


class TestSharedJdParseBudget:
    def test_the_two_entry_paths_share_one_budget(self, client):
        """F-05: 3 parses total across both transports, not 3 each."""
        assert client.post("/parse/text", headers=_as(5)).status_code == 200
        assert client.post("/parse/pdf", headers=_as(5)).status_code == 200
        assert client.post("/parse/text", headers=_as(5)).status_code == 200
        assert client.post("/parse/pdf", headers=_as(5)).status_code == 429

    def test_alternating_transport_cannot_double_the_allowance(self, client):
        """The exact bypass the upload route's docstring claimed to prevent and did not."""
        allowed = 0
        for n in range(8):
            path = "/parse/text" if n % 2 == 0 else "/parse/pdf"
            if client.post(path, headers=_as(6)).status_code == 200:
                allowed += 1
        assert allowed == 3, f"alternating bought {allowed} parses, budget is 3"

    def test_the_shared_budget_is_still_per_user(self, client):
        for _ in range(3):
            client.post("/parse/text", headers=_as(11))
        assert client.post("/parse/text", headers=_as(11)).status_code == 429
        assert client.post("/parse/pdf", headers=_as(12)).status_code == 200

    def test_an_unrelated_route_keeps_its_own_budget(self, client):
        """Exhausting the parse scope must not spend the plan route's separate allowance."""
        for _ in range(4):
            client.post("/parse/text", headers=_as(13))
        assert client.post("/parse/pdf", headers=_as(13)).status_code == 429
        assert client.post("/plan", headers=_as(13)).status_code == 200
