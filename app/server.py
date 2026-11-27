"""The one supported way to start this API in production.

    python -m app.server

## Why a launcher instead of a documented command line

The trusted-proxy policy is a security control, and a command line is a bad place to keep one.
`uvicorn app.main:app --forwarded-allow-ips="*"` is a single flag that silently disables every
IP-keyed authentication limit in the application, and nothing inside the process can see that
it happened: `ProxyHeadersMiddleware` wraps the ASGI app from the outside, so the app cannot
inspect its own trust configuration at runtime. A guard in `validate_for_production` can
require the *environment* to state a correct policy, but it cannot stop a flag from
contradicting it.

So the flag is removed from the problem instead of being validated. This module takes no
arguments at all, refuses to run if given any, builds the `uvicorn.Config` itself from
constants, and then inspects the middleware object Uvicorn actually built to confirm it
matches the policy before serving a single request. The documented production command is
`python -m app.server`, and there is deliberately no second supported way.

## What is fixed here, and why each value

* ``host="127.0.0.1"`` -- the primary control, and the one that does not depend on anything
  being configured correctly. Nginx terminates TLS on the public interfaces and proxies to
  loopback; Uvicorn never holds a public socket, so a forged ``X-Forwarded-For`` has no path to
  the application that does not pass through Nginx, which overwrites it. The firewall is the
  second layer, not the first.
* ``port=8000`` -- matches ``proxy_pass http://127.0.0.1:8000`` in the committed Nginx config.
* ``proxy_headers=True`` -- required, or ``request.client.host`` is Nginx for every request and
  all unauthenticated callers share one rate-limit bucket. That failure is an availability
  problem rather than a spoofing one: login would cap at 5/minute for the entire user base.
* ``forwarded_allow_ips=TRUSTED_PROXY_IPS`` -- exactly one loopback address. Uvicorn then walks
  ``X-Forwarded-For`` from right to left and takes the first address that is not trusted, which
  is the real client. With ``"*"`` it would instead take the leftmost value, which the caller
  chooses.
* ``workers=1`` -- a security requirement in this topology, not a capacity choice. The rate
  limiter's counters live in the process (`app/core/rate_limit.py`), so N workers means every
  configured limit is really N times that limit. Raising this number silently multiplies the
  authentication limits and needs a shared counter store first. See the scaling gate in
  `docs/PRODUCTION_DEPLOYMENT.md`.

## What this module does not do

It does not create infrastructure, write configuration files, or deploy. It starts a server
when run, and when imported it does nothing but offer `build_config()` for tests to inspect.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional, Sequence

import uvicorn

from app.core.config import (
    FORWARDED_ALLOW_IPS_ENV,
    TRUSTED_PROXY_IPS,
    forwarded_allow_ips_fault,
)

logger = logging.getLogger(__name__)

#: Loopback only. See the module docstring -- this is the control that holds even if every
#: other layer is misconfigured.
BIND_HOST = "127.0.0.1"
BIND_PORT = 8000

#: One. Not a performance default: see the module docstring and the scaling gate.
WORKERS = 1


class ProxyPolicyError(RuntimeError):
    """The process may not serve traffic, because its client-IP trust cannot be trusted."""


def align_environment() -> None:
    """Make the environment state the same policy this module will hand Uvicorn.

    `app.main` calls `validate_for_production()` at import time, and that guard reads
    `FORWARDED_ALLOW_IPS` from the real environment. This module passes the policy to Uvicorn
    programmatically, so without this step a correct launcher start would fail its own guard
    for having said nothing.

    A value that is already present and already correct is left alone. A value that is present
    and *different* is a conflict, and this refuses rather than overwriting it: two places
    would then be stating different policies, and quietly picking one is how a deployment ends
    up protected by something nobody chose. An operator who put `"*"` in the unit file needs to
    be told, not corrected behind their back.
    """
    existing = os.environ.get(FORWARDED_ALLOW_IPS_ENV)
    if existing is None:
        os.environ[FORWARDED_ALLOW_IPS_ENV] = TRUSTED_PROXY_IPS
        return
    if existing.strip() == TRUSTED_PROXY_IPS:
        return
    raise ProxyPolicyError(
        f"{FORWARDED_ALLOW_IPS_ENV}={existing!r} in the environment contradicts this "
        f"launcher's policy of {TRUSTED_PROXY_IPS!r}. Refusing to start rather than choosing "
        "one silently. Remove the variable from the unit's environment file and let "
        "app/server.py set it, or correct it to the policy value."
    )


def build_config() -> uvicorn.Config:
    """The production `uvicorn.Config`, built from constants and then verified.

    Separate from `main()` so a test can assert the resolved values and the middleware without
    binding a socket.
    """
    fault = forwarded_allow_ips_fault(TRUSTED_PROXY_IPS)
    if fault is not None:  # pragma: no cover - only reachable if the constant itself is edited
        raise ProxyPolicyError(
            f"This module's own TRUSTED_PROXY_IPS is not an acceptable policy: {fault}"
        )

    config = uvicorn.Config(
        "app.main:app",
        host=BIND_HOST,
        port=BIND_PORT,
        workers=WORKERS,
        proxy_headers=True,
        forwarded_allow_ips=TRUSTED_PROXY_IPS,
        log_config=None,  # app/core/logging.py already configured logging; do not replace it.
    )
    _assert_config_matches_policy(config)
    return config


def _assert_config_matches_policy(config: uvicorn.Config) -> None:
    """Check what Uvicorn resolved, not what we asked for.

    The keyword arguments above are the intent; this is the verification. It exists because
    Uvicorn's defaults are version-dependent -- `proxy_headers` and `forwarded_allow_ips` have
    both changed shape across releases, and `forwarded_allow_ips` resolves through an
    environment variable when it is not passed. A pinned version bump should fail here loudly
    rather than change what this server trusts quietly.
    """
    if not config.proxy_headers:
        raise ProxyPolicyError(
            "Uvicorn resolved proxy_headers=False, so X-Forwarded-For would be ignored and "
            "every client would appear to be Nginx."
        )
    resolved = config.forwarded_allow_ips
    expected = [TRUSTED_PROXY_IPS]
    if resolved not in (TRUSTED_PROXY_IPS, expected):
        raise ProxyPolicyError(
            f"Uvicorn resolved forwarded_allow_ips={resolved!r}, not {TRUSTED_PROXY_IPS!r}."
        )
    if config.host != BIND_HOST:
        raise ProxyPolicyError(
            f"Uvicorn resolved host={config.host!r}, not {BIND_HOST!r}. A public bind would let "
            "a caller reach the application without passing through Nginx, and spoof "
            "X-Forwarded-For freely."
        )
    if config.port != BIND_PORT:
        raise ProxyPolicyError(f"Uvicorn resolved port={config.port!r}, not {BIND_PORT!r}.")
    if config.workers != WORKERS:
        raise ProxyPolicyError(
            f"Uvicorn resolved workers={config.workers!r}, not {WORKERS}. More than one worker "
            "multiplies every authentication rate limit by the worker count, because the "
            "counters are per process."
        )


def verify_loaded_middleware(config: uvicorn.Config) -> None:
    """Inspect the real `ProxyHeadersMiddleware` instance that will serve traffic.

    This is the strongest check available in-process, and the reason the launcher exists: it
    reads the middleware object's own trust container rather than the configuration that was
    meant to produce it. `_TrustedHosts` keeps three collections -- parsed addresses, parsed
    networks, and a set of *string literals* for anything it could not parse -- plus an
    `always_trust` flag for `"*"`. A malformed entry lands in the literals and matches no
    client, which trusts nothing while looking configured. Asserting on the parsed address set
    and on both of the other collections being empty is what makes that state impossible to
    reach from here.

    Requires `config.load()` to have run, which imports `app.main` and therefore runs the
    application's own production guard.
    """
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = config.loaded_app
    if not isinstance(app, ProxyHeadersMiddleware):
        raise ProxyPolicyError(
            "The loaded application is not wrapped in ProxyHeadersMiddleware, so "
            "X-Forwarded-For would never be read and every client would appear to be Nginx."
        )

    trusted = app.trusted_hosts
    if getattr(trusted, "always_trust", False):
        raise ProxyPolicyError(
            "ProxyHeadersMiddleware was built to trust every peer. The leftmost, "
            "caller-supplied X-Forwarded-For value would become the rate-limit key."
        )
    import ipaddress

    # Literals and networks are checked BEFORE the address set, and the order is the whole
    # value of these two checks. A typo such as `127.0.0..1` leaves `trusted_hosts` empty and
    # the typo sitting in `trusted_literals`, so an address-set check would fire first and
    # report "trusts set(), expected 127.0.0.1" -- true, and useless for finding the cause.
    # Checking the literals first means the operator is told that something unparseable was
    # configured and what that does, which is the actual diagnosis.
    if trusted.trusted_literals:
        raise ProxyPolicyError(
            f"ProxyHeadersMiddleware holds unparsed trust literals {trusted.trusted_literals!r}. "
            "Uvicorn keeps anything it cannot parse as an address or network as a plain "
            "string, and a string matches no client address -- so this configuration trusts "
            "nothing at all while appearing set. Every request would resolve to Nginx and the "
            "whole user base would share one authentication rate-limit bucket."
        )
    if trusted.trusted_networks:
        raise ProxyPolicyError(
            f"ProxyHeadersMiddleware trusts networks {trusted.trusted_networks!r}; this "
            "topology has one proxy and must trust one address."
        )
    expected = {ipaddress.ip_address(TRUSTED_PROXY_IPS)}
    if set(trusted.trusted_hosts) != expected:
        raise ProxyPolicyError(
            f"ProxyHeadersMiddleware trusts {set(trusted.trusted_hosts)!r}, expected {expected!r}."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Start the server, or refuse to.

    Takes no arguments on purpose. Accepting even one would reopen the hole this module was
    written to close, because the first argument anybody would want is the one that changes
    what the server trusts.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        sys.stderr.write(
            f"{__name__} takes no arguments (got {args!r}).\n"
            "The host, port, worker count and trusted-proxy policy are fixed in this module "
            "because they are security settings, not deployment preferences. To change one, "
            "edit it here in a reviewed commit and re-run the proxy trust tests.\n"
        )
        return 2

    align_environment()
    config = build_config()
    config.load()
    verify_loaded_middleware(config)

    logger.info(
        "Starting API on %s:%s with %s worker, trusting proxy %s",
        BIND_HOST,
        BIND_PORT,
        WORKERS,
        TRUSTED_PROXY_IPS,
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
