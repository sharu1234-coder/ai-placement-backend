"""Who the application believes the client is, and who may tell it.

This is the HIGH-3 surface: `request.client.host` feeds `app/core/rate_limit.py`, so whoever
controls that value controls the authentication rate limits. Nothing in `app/` reads a
forwarded header -- the decision belongs entirely to `uvicorn.middleware.proxy_headers`, which
sits *outside* the ASGI application.

**Why these tests do not use `TestClient`.** Starlette's `TestClient` never installs
`ProxyHeadersMiddleware`. A test that posts `X-Forwarded-For` through it is not exercising any
trust decision: the header is ignored, the client is always `"testclient"`, and the test passes
identically whether the real deployment trusts one address or every address on the internet.
`tests/api/test_action_rate_limits.py` and `tests/unit/test_rate_limit_identity.py` do send
spoofed headers, and what they prove is real but different -- that an *authenticated* route
keys on the server-resolved user id. The unauthenticated routes, where the address is the only
identity available, had no coverage at all.

So these tests drive the middleware object directly, with hand-built ASGI scopes, at the
pinned version in `requirements.txt`. The scopes are the ones Uvicorn would construct: `client`
is the real socket peer, which cannot be forged by a caller, and `headers` are whatever the
caller sent.
"""

from __future__ import annotations

import asyncio
import ipaddress

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core.config import (
    FORWARDED_ALLOW_IPS_ENV,
    TRUSTED_PROXY_IPS,
    forwarded_allow_ips_fault,
)

#: A caller-chosen value. Any test asserting this is NOT the resolved client is asserting that
#: spoofing failed.
SPOOFED = "203.0.113.9"


def resolve_client(trusted: str, *, peer: str, forwarded: object = None) -> str | None:
    """The address the application would see, given a trust policy and one request.

    `peer` is the socket address, which a caller cannot choose. `forwarded` is the
    `X-Forwarded-For` header, which a caller can choose freely -- pass a list to send the
    header more than once, the way a chain of proxies each appending would.
    """
    seen: dict[str, object] = {}

    async def app(scope, receive, send):
        client = scope.get("client")
        seen["host"] = client[0] if client else None
        seen["scheme"] = scope.get("scheme")

    headers: list[tuple[bytes, bytes]] = []
    if forwarded is not None:
        values = forwarded if isinstance(forwarded, list) else [forwarded]
        headers.extend((b"x-forwarded-for", v.encode("latin1")) for v in values)

    scope = {
        "type": "http",
        "scheme": "http",
        "headers": headers,
        "client": (peer, 54321),
    }
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))
    return seen["host"]  # type: ignore[return-value]


class TestASpoofedHeaderFromAnUntrustedPeer:
    """The attack in its simplest form: a caller claiming to be somebody else."""

    def test_the_header_is_ignored_entirely(self):
        """The peer is not trusted, so the middleware does not even look at the header.

        This is the case that holds when the deployment is correct and someone reaches the
        app from anywhere other than Nginx -- which, with Uvicorn bound to loopback, means
        another process on the same host.
        """
        assert resolve_client(TRUSTED_PROXY_IPS, peer="198.51.100.4", forwarded=SPOOFED) == (
            "198.51.100.4"
        )

    def test_rotating_the_header_cannot_move_the_key(self):
        """The rate-limit key is derived from this value, so a stable value is the point."""
        seen = {
            resolve_client(TRUSTED_PROXY_IPS, peer="198.51.100.4", forwarded=f"10.0.0.{n}")
            for n in range(20)
        }
        assert seen == {"198.51.100.4"}, "20 forged headers must yield one key, not 20"


class TestTheTrustedProxyOnLoopback:
    """The deployed path: Nginx on 127.0.0.1 speaking for a real client."""

    def test_an_overwritten_header_gives_the_real_client(self):
        """What the committed Nginx config produces.

        `proxy_set_header X-Forwarded-For $remote_addr` replaces whatever the caller sent with
        the socket address Nginx itself observed, so exactly one address arrives and no
        caller-supplied byte survives.
        """
        assert resolve_client(TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded="198.51.100.7") == (
            "198.51.100.7"
        )

    def test_an_appended_chain_still_resolves_to_the_real_client(self):
        """Defence in depth for the config we did NOT use.

        `$proxy_add_x_forwarded_for` appends to the caller's header rather than replacing it,
        so a forged value survives in front of the real one. Uvicorn walks the list from the
        right and returns the first untrusted address, which is the one Nginx appended. The
        forged prefix loses. This is why the single-proxy topology is safe either way -- and
        why the config still overwrites, so that correctness does not depend on this walk.
        """
        assert resolve_client(
            TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded=f"{SPOOFED}, 198.51.100.7"
        ) == "198.51.100.7"

    def test_a_repeated_header_is_joined_not_chosen_between(self):
        """Two `X-Forwarded-For` headers are one comma-joined list to the middleware."""
        assert resolve_client(
            TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded=[SPOOFED, "198.51.100.7"]
        ) == "198.51.100.7"

    def test_no_header_leaves_the_proxy_as_the_client(self):
        """The misconfiguration that looks like an outage.

        If Nginx forwards nothing, every request resolves to 127.0.0.1 and every
        unauthenticated caller shares one bucket -- login becomes 5/minute for the whole user
        base. Pinned here so the symptom is recognisable from a test name.
        """
        assert resolve_client(TRUSTED_PROXY_IPS, peer="127.0.0.1") == "127.0.0.1"

    def test_a_chain_of_only_trusted_hops_falls_back_rather_than_failing_open(self):
        """Every entry trusted means the client was itself a proxy; Uvicorn takes the first."""
        assert resolve_client(
            TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded="127.0.0.1, 127.0.0.1"
        ) == "127.0.0.1"


class TestWhyTheWildcardIsForbidden:
    """Executable evidence for the rule, rather than a comment asserting it."""

    def test_a_caller_chooses_their_own_identity(self):
        """With `"*"`, Uvicorn takes the LEFTMOST value -- the one the caller wrote first.

        Note the peer is not a proxy at all here. `always_trust` skips the peer check, so this
        works from anywhere, which is what makes it catastrophic rather than merely wrong.
        """
        assert resolve_client("*", peer="198.51.100.4", forwarded=SPOOFED) == SPOOFED

    def test_every_request_can_have_a_fresh_identity(self):
        """The precise consequence: IP-keyed limits stop applying to anyone."""
        seen = {
            resolve_client("*", peer="198.51.100.4", forwarded=f"10.0.0.{n}") for n in range(20)
        }
        assert len(seen) == 20, "each forged header bought its own rate-limit bucket"

    def test_an_appended_chain_hands_over_the_forged_prefix(self):
        """Even with a correct Nginx appending the truth, `"*"` prefers the forgery."""
        assert resolve_client(
            "*", peer="127.0.0.1", forwarded=f"{SPOOFED}, 198.51.100.7"
        ) == SPOOFED

    def test_the_production_guard_refuses_it(self):
        assert "trusts every peer" in (forwarded_allow_ips_fault("*") or "")


class TestAddressFamilies:
    def test_ipv4_resolves(self):
        assert resolve_client(TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded="198.51.100.7") == (
            "198.51.100.7"
        )

    def test_ipv6_resolves(self):
        assert resolve_client(
            TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded="2001:db8::1"
        ) == "2001:db8::1"

    def test_a_bracketed_ipv6_with_a_port_drops_the_port(self):
        """Uvicorn parses `[addr]:port` and keys on the address, not the ephemeral port.

        A port in the key would be a per-connection identity, which would defeat every
        IP-keyed limit as thoroughly as a spoofable header does.
        """
        assert resolve_client(
            TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded="[2001:db8::1]:51234"
        ) == "2001:db8::1"

    def test_an_ipv4_with_a_port_drops_the_port(self):
        assert resolve_client(
            TRUSTED_PROXY_IPS, peer="127.0.0.1", forwarded="198.51.100.7:51234"
        ) == "198.51.100.7"

    def test_an_ipv6_loopback_peer_is_not_the_ipv4_policy(self):
        """`::1` is a different address from `127.0.0.1` and is not covered by the policy.

        Deliberate: Nginx proxies to `127.0.0.1:8000`, so `::1` is not a hop this topology
        has. If it ever becomes one, the policy must change in `app/core/config.py` and this
        test must change with it -- which is the point of asserting it.
        """
        assert resolve_client(TRUSTED_PROXY_IPS, peer="::1", forwarded=SPOOFED) == "::1"


class TestAMalformedPolicyTrustsNothing:
    """Uvicorn's silent-literal behaviour, demonstrated -- then refused by the guard.

    `_TrustedHosts` keeps anything it cannot parse in a set of string literals, which can never
    equal a parsed client address. So a typo does not fail loudly: it trusts nothing, every
    request resolves to Nginx, and the whole user base shares one authentication bucket. These
    tests are why `forwarded_allow_ips_fault` parses strictly instead of deferring to Uvicorn.
    """

    @pytest.mark.parametrize("typo", ["127.0.0..1", "127.0.0.1/33", "localhost", "127,0,0,1"])
    def test_a_typo_silently_trusts_no_one(self, typo):
        assert resolve_client(typo, peer="127.0.0.1", forwarded="198.51.100.7") == "127.0.0.1"

    @pytest.mark.parametrize("typo", ["127.0.0..1", "127.0.0.1/33", "localhost", "127,0,0,1"])
    def test_the_guard_rejects_what_uvicorn_would_swallow(self, typo):
        assert forwarded_allow_ips_fault(typo) is not None, (
            f"{typo!r} must be refused at startup, not discovered as an outage"
        )

    def test_an_empty_policy_trusts_no_one(self):
        assert resolve_client("", peer="127.0.0.1", forwarded="198.51.100.7") == "127.0.0.1"
        assert forwarded_allow_ips_fault("") is not None


class TestTheProductionGuardDecisionTable:
    """`forwarded_allow_ips_fault` is pure, so the whole policy is testable as a table."""

    def test_the_policy_value_is_accepted(self):
        assert forwarded_allow_ips_fault(TRUSTED_PROXY_IPS) is None

    def test_unset_is_refused_even_though_uvicorn_would_default_to_the_same_value(self):
        """Uvicorn's own default is `127.0.0.1`, so unset happens to behave correctly today.

        Refused anyway: a security control that is right by coincidence is not a control, and
        the coincidence is a pinned dependency's default which a version bump may change.
        """
        fault = forwarded_allow_ips_fault(None)
        assert fault is not None and "not set" in fault

    @pytest.mark.parametrize(
        "value",
        ["0.0.0.0/0", "::/0", "127.0.0.0/8", "10.0.0.0/24", "0.0.0.0/0,127.0.0.1"],
    )
    def test_broad_ranges_are_refused(self, value):
        assert forwarded_allow_ips_fault(value) is not None

    def test_a_single_host_cidr_is_refused_in_favour_of_the_bare_address(self):
        """`127.0.0.1/32` is semantically identical and still refused.

        One spelling of the policy keeps the configured value diffable against the documented
        one. This is strictness for reviewability, not for security.
        """
        fault = forwarded_allow_ips_fault("127.0.0.1/32")
        assert fault is not None and "bare address" in fault

    @pytest.mark.parametrize("value", ["10.0.0.5", "198.51.100.7", "::1", "2001:db8::1"])
    def test_an_address_that_is_not_this_topologys_proxy_is_refused(self, value):
        assert forwarded_allow_ips_fault(value) is not None

    @pytest.mark.parametrize("value", ["127.0.0.1,", ",127.0.0.1", "127.0.0.1,,127.0.0.1"])
    def test_a_stray_comma_is_refused(self, value):
        """It would become an unparseable empty literal inside Uvicorn."""
        assert forwarded_allow_ips_fault(value) is not None

    def test_extra_trusted_entries_are_refused(self):
        assert forwarded_allow_ips_fault("127.0.0.1,10.0.0.5") is not None

    def test_surrounding_whitespace_is_tolerated(self):
        """An env file with a trailing space is a typo, not a policy change."""
        assert forwarded_allow_ips_fault("  127.0.0.1  ") is None

    def test_no_fault_message_names_the_variable_wrongly(self):
        for value in ["*", "", "0.0.0.0/0", "localhost", "10.0.0.5"]:
            assert FORWARDED_ALLOW_IPS_ENV in (forwarded_allow_ips_fault(value) or "")


class TestTheLauncherIsTheOnlySupportedStart:
    """`app/server.py` exists so the trusted-proxy policy is not a command-line argument."""

    def test_the_config_it_builds_matches_the_policy(self):
        from app import server

        config = server.build_config()
        assert config.host == "127.0.0.1", "a public bind would bypass Nginx entirely"
        assert config.port == 8000
        assert config.workers == 1, "more workers multiply every authentication limit"
        assert config.proxy_headers is True
        assert config.forwarded_allow_ips in (TRUSTED_PROXY_IPS, [TRUSTED_PROXY_IPS])

    def test_it_refuses_arguments(self):
        """Accepting one would reopen exactly the hole this module closes."""
        from app import server

        assert server.main(["--forwarded-allow-ips=*"]) == 2
        assert server.main(["--host", "0.0.0.0"]) == 2
        assert server.main(["--workers", "4"]) == 2

    def test_it_refuses_an_environment_that_contradicts_it(self, monkeypatch):
        """Two places stating different policies is a conflict, not a precedence question."""
        from app import server

        monkeypatch.setenv(FORWARDED_ALLOW_IPS_ENV, "*")
        with pytest.raises(server.ProxyPolicyError, match="contradicts"):
            server.align_environment()

    def test_it_sets_the_policy_when_the_environment_is_silent(self, monkeypatch):
        """`align_environment` writes to os.environ, so this test has to clean up after it.

        It mutates the real process environment -- that is its job, since `app.main` reads the
        variable at import. `monkeypatch.delenv` only records the state it removed, so it will
        not undo a value written afterwards by the code under test. Setting the variable
        through monkeypatch first means there IS a recorded prior state to restore.

        This matters beyond tidiness: an earlier version of this test leaked
        FORWARDED_ALLOW_IPS into the process, and tests/unit/test_config_validation.py then
        passed in the same run only because this file happened to run first. That is a false
        pass, and it hid the fact that the new guard had added a production requirement those
        tests did not state.
        """
        import os

        from app import server

        monkeypatch.setenv(FORWARDED_ALLOW_IPS_ENV, "placeholder-so-undo-restores-this")
        monkeypatch.delenv(FORWARDED_ALLOW_IPS_ENV)
        server.align_environment()
        assert os.environ[FORWARDED_ALLOW_IPS_ENV] == TRUSTED_PROXY_IPS

    def test_it_leaves_an_already_correct_environment_alone(self, monkeypatch):
        from app import server

        monkeypatch.setenv(FORWARDED_ALLOW_IPS_ENV, f"  {TRUSTED_PROXY_IPS}  ")
        server.align_environment()

    def test_the_middleware_check_rejects_a_wildcard_build(self):
        """The strongest check: the real middleware object, not the config that made it."""
        from app import server

        class _Fake:
            loaded_app = ProxyHeadersMiddleware(lambda *a: None, trusted_hosts="*")

        with pytest.raises(server.ProxyPolicyError, match="trust every peer"):
            server.verify_loaded_middleware(_Fake())

    def test_the_middleware_check_rejects_unparsed_literals(self):
        """The silent-typo state, refused where it would otherwise never be noticed."""
        from app import server

        class _Fake:
            loaded_app = ProxyHeadersMiddleware(lambda *a: None, trusted_hosts="127.0.0..1")

        with pytest.raises(server.ProxyPolicyError, match="trust literals"):
            server.verify_loaded_middleware(_Fake())

    def test_the_middleware_check_rejects_an_unwrapped_app(self):
        from app import server

        class _Fake:
            loaded_app = lambda *a: None  # noqa: E731

        with pytest.raises(server.ProxyPolicyError, match="not wrapped"):
            server.verify_loaded_middleware(_Fake())

    def test_the_middleware_check_accepts_the_real_policy(self):
        from app import server

        class _Fake:
            loaded_app = ProxyHeadersMiddleware(
                lambda *a: None, trusted_hosts=TRUSTED_PROXY_IPS
            )

        server.verify_loaded_middleware(_Fake())


class TestTheProductionStartupGuard:
    """End to end: `validate_for_production` must refuse to boot on a bad policy."""

    @pytest.fixture()
    def production(self, monkeypatch):
        """A minimally valid production environment, so only the proxy policy is under test."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "environment", "production")
        monkeypatch.setattr(settings, "jwt_secret_key", "x" * 64)
        monkeypatch.setattr(settings, "email_provider", "smtp")
        monkeypatch.setattr(settings, "debug", False)
        monkeypatch.setattr(settings, "allowed_hosts", "api.example.com")
        monkeypatch.setattr(settings, "database_url", "postgresql+psycopg2://u@h:5432/d")
        monkeypatch.setattr(settings, "storage_provider", "s3")
        monkeypatch.setattr(settings, "ai_provider", "anthropic")
        monkeypatch.setattr(settings, "stt_provider", "none")
        monkeypatch.setattr(settings, "tts_provider", "none")
        monkeypatch.setattr(settings, "razorpay_key_id", None)
        monkeypatch.setattr(settings, "token_encryption_key", None)
        monkeypatch.setattr(settings, "github_client_id", None)
        return monkeypatch

    def test_it_boots_with_the_policy_set(self, production):
        from app.core.config import validate_for_production

        production.setenv(FORWARDED_ALLOW_IPS_ENV, TRUSTED_PROXY_IPS)
        validate_for_production()

    @pytest.mark.parametrize("value", ["*", "", "0.0.0.0/0", "::/0", "127.0.0..1", "10.0.0.5"])
    def test_it_refuses_to_boot_on_an_unsafe_policy(self, production, value):
        from app.core.config import validate_for_production

        production.setenv(FORWARDED_ALLOW_IPS_ENV, value)
        with pytest.raises(RuntimeError, match=FORWARDED_ALLOW_IPS_ENV):
            validate_for_production()

    def test_it_refuses_to_boot_when_the_policy_is_unset(self, production):
        from app.core.config import validate_for_production

        production.delenv(FORWARDED_ALLOW_IPS_ENV, raising=False)
        with pytest.raises(RuntimeError, match="not set"):
            validate_for_production()

    def test_the_error_says_how_to_fix_it(self, production):
        from app.core.config import validate_for_production

        production.setenv(FORWARDED_ALLOW_IPS_ENV, "*")
        with pytest.raises(RuntimeError) as caught:
            validate_for_production()
        message = str(caught.value)
        assert "app/server.py" in message, "an operator needs the supported start command"
        assert TRUSTED_PROXY_IPS in message, "and the value to set"

    def test_development_is_unaffected(self, monkeypatch):
        """Every dev checkout and the whole test suite start with no policy set.

        The guard must not make local development require a proxy variable -- there is no
        proxy locally, and `validate_for_production` returns early for `development`.
        """
        from app.core.config import settings, validate_for_production

        monkeypatch.setattr(settings, "environment", "development")
        monkeypatch.delenv(FORWARDED_ALLOW_IPS_ENV, raising=False)
        validate_for_production()


def test_the_policy_is_a_single_parseable_loopback_address():
    """A guard rail on the constant itself, since everything above trusts it."""
    assert ipaddress.ip_address(TRUSTED_PROXY_IPS).is_loopback
    assert "," not in TRUSTED_PROXY_IPS and "/" not in TRUSTED_PROXY_IPS
