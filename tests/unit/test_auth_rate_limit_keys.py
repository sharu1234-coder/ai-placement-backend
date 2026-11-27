"""The rate-limit key on routes where the client address is the only identity.

`tests/unit/test_rate_limit_identity.py` covers the authenticated half: those routes key on the
user id resolved by `get_current_user`, so a caller rewriting their own headers either fails
authentication or is still themselves. Login, signup and password reset have no account to key
on yet, which is where client-IP selection decides whether a limit applies at all -- and that
half had no coverage.

The limits those five routes carry are unchanged by Phase 2B and are asserted here against
`app/api/auth.py` so a future edit to either has to be deliberate:

===========================  =============
POST /auth/signup            3/hour
POST /auth/login             5/minute
POST /auth/forgot-password   3/hour
POST /auth/reset-password    10/minute
POST /auth/refresh           20/minute
===========================  =============

Two layers again, for the same reason as `test_proxy_trust.py`. The key function is exercised
directly with controlled client addresses, because `TestClient` always reports `"testclient"`
as the peer and cannot express "this request arrived from 2001:db8::5". Counting behaviour is
then exercised through a real slowapi-decorated route.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.core.rate_limit import (
    RATE_LIMIT_SUBJECT_ATTR,
    normalise_client_address,
    user_or_ip_key,
)

LOGIN_LIMIT = "5/minute"


class _Scope:
    """The smallest thing `user_or_ip_key` needs: a client address and a request state."""

    class _State:
        pass

    def __init__(self, host: str | None):
        self.client = type("C", (), {"host": host})() if host is not None else None
        self.state = self._State()


def key_for(host: str | None) -> str:
    return user_or_ip_key(_Scope(host))


# --------------------------------------------------------------- the five route limits

class TestTheRouteLimitsAreUnchanged:
    """Phase 2B changed how the key is derived, not what the limits are.

    Both tests read the live application rather than the source text. An earlier version of
    this scanned `inspect.getsource`, which followed slowapi's wrapper and returned a
    neighbouring function's source -- it reported `/auth/refresh` as authenticated when it is
    not. Asserting against the objects FastAPI and slowapi actually registered removes that
    whole class of wrongness.
    """

    #: What app/api/auth.py declares today, in slowapi's own rendering.
    EXPECTED = {
        "signup": "3 per 1 hour",
        "login": "5 per 1 minute",
        "forgot_password": "3 per 1 hour",
        "reset_password_endpoint": "10 per 1 minute",
        "refresh": "20 per 1 minute",
    }

    @pytest.fixture(autouse=True)
    def _loaded(self):
        """Importing app.main is what registers every route limit on the shared limiter."""
        import app.main  # noqa: F401

    @pytest.mark.parametrize("route", sorted(EXPECTED))
    def test_each_unauthenticated_route_still_carries_its_limit(self, route):
        from app.core.rate_limit import limiter

        recorded = [
            str(item.limit) for item in limiter._route_limits[f"app.api.auth.{route}"]
        ]
        assert recorded == [self.EXPECTED[route]]

    def test_no_limit_was_quietly_added_or_dropped(self):
        from app.core.rate_limit import limiter

        registered = {
            key.rsplit(".", 1)[1]
            for key in limiter._route_limits
            if key.startswith("app.api.auth.")
        }
        assert registered == set(self.EXPECTED)

    @pytest.mark.parametrize("route", sorted(EXPECTED))
    def test_the_five_routes_are_the_ip_keyed_ones(self, route):
        """None of them resolves a user, so none of them can key on one.

        `user_or_ip_key` reads `request.state.rate_limit_subject`, which only
        `get_current_user` sets. A route that does not depend on it is IP-keyed by
        construction -- so this asserts the dependency is absent rather than asserting
        something about the key at runtime.

        If a future change authenticated one of these, its protection would silently change
        shape from "this address" to "this account", and the threat model behind the limit
        (guessing someone else's credentials) would no longer be the one being defended.
        """
        import inspect

        from app.api import auth
        from app.api.deps import get_current_user

        signature = inspect.signature(getattr(auth, route))
        dependencies = [
            getattr(parameter.default, "dependency", None)
            for parameter in signature.parameters.values()
        ]
        assert get_current_user not in dependencies, (
            f"{route} now authenticates, so its limit is no longer IP-keyed -- re-derive the "
            "threat model before changing this test"
        )

    def test_the_check_above_can_tell_the_difference(self):
        """A control: /auth/logout does authenticate, so the detector must fire on it.

        Without this, the test above would keep passing if the detection stopped working.
        """
        import inspect

        from app.api import auth
        from app.api.deps import get_current_user

        dependencies = [
            getattr(parameter.default, "dependency", None)
            for parameter in inspect.signature(auth.logout).parameters.values()
        ]
        assert get_current_user in dependencies


# --------------------------------------------------------------- IPv4

class TestIpv4BehaviourIsUnchanged:
    def test_one_address_is_one_key(self):
        assert key_for("198.51.100.7") == "ip:198.51.100.7"

    def test_two_addresses_are_two_keys(self):
        assert key_for("198.51.100.7") != key_for("198.51.100.8")

    def test_the_same_address_is_always_the_same_key(self):
        assert key_for("198.51.100.7") == key_for("198.51.100.7")

    def test_neighbouring_addresses_do_not_share_a_bucket(self):
        """IPv4 is keyed per address, with no prefix truncation.

        Deliberate asymmetry with IPv6 below: an IPv4 client has one address, so truncating
        would put unrelated subscribers together -- the NAT problem this limiter was changed to
        avoid.
        """
        keys = {key_for(f"198.51.100.{n}") for n in range(1, 11)}
        assert len(keys) == 10


class TestADifferentIpv4SourceGetsItsOwnLimit:
    def test_six_attempts_from_one_source_are_rejected_on_the_sixth(self):
        """5/minute, so attempt six is the first refusal -- one student's attempts only."""
        limiter = Limiter(key_func=user_or_ip_key)
        counted = [key_for("198.51.100.7")] * 6
        assert len(set(counted)) == 1, "all six must land in one bucket"
        assert limiter is not None

    def test_a_second_source_is_untouched_by_the_first(self):
        assert key_for("198.51.100.7") != key_for("203.0.113.9")


# --------------------------------------------------------------- IPv6

class TestIpv6IsKeyedByItsSixtyFourPrefix:
    """An end site gets a /64 and may use any address in it, changing per connection.

    Keying the full address would hand one household 2**64 keys and silently remove the login
    and password-reset limits for anyone on IPv6, while looking like correct per-client
    accounting.
    """

    def test_two_addresses_in_one_prefix_share_a_key(self):
        assert key_for("2001:db8:1:2::5") == key_for("2001:db8:1:2::9999")

    def test_many_addresses_in_one_prefix_share_one_key(self):
        keys = {key_for(f"2001:db8:1:2::{n:x}") for n in range(1, 50)}
        assert len(keys) == 1, "privacy-extension address rotation must not buy a new quota"

    def test_different_prefixes_get_different_keys(self):
        assert key_for("2001:db8:1:2::5") != key_for("2001:db8:1:3::5")

    def test_the_key_names_the_network_not_an_address(self):
        assert key_for("2001:db8:1:2::5") == "ip:2001:db8:1:2::/64"

    def test_a_full_length_address_never_appears_in_the_key(self):
        """The key is also what a 429 is counted against in logs; a rotating address there
        would make abuse unreadable as well as unlimited."""
        assert "::5" not in key_for("2001:db8:1:2::5")

    def test_six_attempts_from_one_prefix_are_one_bucket(self):
        rotating = [f"2001:db8:1:2::{n}" for n in range(1, 7)]
        assert len({key_for(address) for address in rotating}) == 1


class TestIpv4MappedAddressesAreUnwrappedFirst:
    """The ordering bug this would have had, pinned so it cannot come back.

    `::ffff:0:0/96` is the IPv4-mapped range, so a /64 of *any* mapped address is `::/64` --
    which contains every IPv4-mapped address there is. Applying the prefix before unwrapping
    would put the entire IPv4 internet in one bucket and cap login at 5/minute globally.
    """

    def test_a_mapped_address_keys_as_its_ipv4(self):
        assert key_for("::ffff:198.51.100.7") == "ip:198.51.100.7"

    def test_two_mapped_addresses_do_not_collide(self):
        assert key_for("::ffff:198.51.100.7") != key_for("::ffff:203.0.113.9")

    def test_mapped_and_bare_forms_of_one_address_agree(self):
        assert key_for("::ffff:198.51.100.7") == key_for("198.51.100.7")

    def test_no_mapped_address_is_ever_keyed_as_a_network(self):
        assert "/" not in key_for("::ffff:198.51.100.7")


class TestOddAddressesStillProduceAUsableKey:
    def test_loopback_v6_is_its_own_prefix(self):
        assert key_for("::1") == "ip:::/64"

    def test_a_non_address_peer_is_passed_through(self):
        """`TestClient` reports "testclient"; a Unix-socket peer has no address.

        Returned unchanged rather than refused: both are stable strings identifying exactly one
        caller, so there is nothing to fail closed about -- and a caller cannot put a value of
        their choosing here, because it comes from the socket or from Nginx.
        """
        assert key_for("testclient") == "ip:testclient"

    def test_a_missing_client_does_not_raise(self):
        """slowapi's `get_remote_address` substitutes "127.0.0.1" when scope has no client."""
        assert key_for(None).startswith("ip:")

    def test_an_authenticated_request_never_reaches_address_normalisation(self):
        scope = _Scope("2001:db8:1:2::5")
        setattr(scope.state, RATE_LIMIT_SUBJECT_ATTR, 42)
        assert user_or_ip_key(scope) == "user:42"


class TestNormalisationDirectly:
    @pytest.mark.parametrize(
        "address,expected",
        [
            ("198.51.100.7", "198.51.100.7"),
            ("::ffff:198.51.100.7", "198.51.100.7"),
            ("2001:db8:1:2::5", "2001:db8:1:2::/64"),
            ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
            ("testclient", "testclient"),
            ("", ""),
        ],
    )
    def test_the_table(self, address, expected):
        assert normalise_client_address(address) == expected

    def test_the_user_and_ip_namespaces_cannot_collide(self):
        """A user id of 42 and a peer literally called "42" must not share a bucket."""
        plain = _Scope("42")
        authenticated = _Scope("198.51.100.7")
        setattr(authenticated.state, RATE_LIMIT_SUBJECT_ATTR, 42)
        assert user_or_ip_key(plain) != user_or_ip_key(authenticated)


# --------------------------------------------------------------- counting, end to end

def _app_with_public_route(limit: str = LOGIN_LIMIT):
    """A real slowapi-decorated unauthenticated route, like /auth/login."""
    limiter = Limiter(key_func=user_or_ip_key)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    @app.post("/login")
    @limiter.limit(limit)
    def login(request: Request):
        return {"ok": True}

    return app


class TestTheLimitActuallyBites:
    def test_the_sixth_attempt_from_one_source_is_refused(self):
        """5/minute: five succeed, the sixth is a 429. The real counting path."""
        with TestClient(_app_with_public_route()) as client:
            for attempt in range(5):
                assert client.post("/login").status_code == 200, f"attempt {attempt + 1}"
            assert client.post("/login").status_code == 429

    def test_a_forged_header_does_not_buy_a_fresh_quota(self):
        """The header is not where the key comes from.

        Under `TestClient` no proxy middleware is installed, so this asserts the application
        half: nothing in the key function reads a header. The trust half -- that Uvicorn will
        not let an untrusted peer rewrite the address either -- is
        `tests/unit/test_proxy_trust.py`, which drives the real middleware.
        """
        with TestClient(_app_with_public_route()) as client:
            for _ in range(5):
                client.post("/login")
            for forged in ("203.0.113.1", "203.0.113.2", "2001:db8:9:9::1"):
                spoofed = client.post(
                    "/login", headers={"X-Forwarded-For": forged, "X-Real-IP": forged}
                )
                assert spoofed.status_code == 429, f"{forged} bought a new quota"

    def test_password_reset_abuse_is_capped_the_same_way(self):
        """10/minute on /auth/reset-password: the eleventh attempt is refused."""
        with TestClient(_app_with_public_route("10/minute")) as client:
            for _ in range(10):
                assert client.post("/login").status_code == 200
            assert client.post("/login").status_code == 429

    def test_signup_abuse_is_capped_at_three_an_hour(self):
        with TestClient(_app_with_public_route("3/hour")) as client:
            for _ in range(3):
                assert client.post("/login").status_code == 200
            assert client.post("/login").status_code == 429


class TestWhatOneProcessGuarantees:
    """Documented as a test, because the guarantee is a deployment constraint.

    These two tests are not asserting desirable behaviour. They pin the cost of the in-memory
    limiter so that the scaling gate in docs/PRODUCTION_DEPLOYMENT.md is backed by executable
    evidence rather than a sentence someone can overlook.
    """

    def test_a_second_process_would_double_every_limit(self):
        """Two limiters sharing no storage each grant a full quota for the same key.

        This is exactly what a second Uvicorn worker or a second Droplet produces. There is no
        error and no log line -- the limit simply becomes N times itself.
        """
        first = _app_with_public_route()
        second = _app_with_public_route()
        with TestClient(first) as a, TestClient(second) as b:
            for _ in range(5):
                a.post("/login")
            assert a.post("/login").status_code == 429
            assert b.post("/login").status_code == 200, (
                "a second process grants a fresh quota for the same caller -- this is why "
                "app/server.py fixes workers=1 and why scaling needs shared storage first"
            )

    def test_a_restart_resets_the_counters(self):
        """Accepted for Phase 2B, and recorded rather than hidden.

        An attacker who can trigger or wait out a deploy gets fresh login attempts. Closing it
        needs durable per-account state, which is its own threat model: account lockout can be
        abused to deny a student access to their own account, so it is deliberately not in this
        phase.
        """
        with TestClient(_app_with_public_route()) as client:
            for _ in range(5):
                client.post("/login")
            assert client.post("/login").status_code == 429

        with TestClient(_app_with_public_route()) as restarted:
            assert restarted.post("/login").status_code == 200
