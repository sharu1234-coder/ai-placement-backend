import ipaddress

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

#: IPv6 clients are keyed on this prefix length, not on the full address.
#:
#: An IPv4 client has one address, so one address is one identity. An IPv6 client is normally
#: delegated a whole /64 by its ISP and may use any address inside it, choosing a fresh one per
#: connection -- privacy extensions do exactly that by design. Keying on the full address would
#: therefore give a single household an effectively unlimited supply of distinct rate-limit
#: keys, which defeats the login and password-reset limits completely while looking like
#: correct per-client accounting.
#:
#: /64 is the smallest block an end site is ever assigned, so it is the narrowest prefix that
#: still cannot be subdivided to escape a limit. Going wider (a /56 or /48) would start putting
#: unrelated subscribers in one bucket, which is the NAT problem this limiter was changed to
#: avoid for authenticated routes.
IPV6_KEY_PREFIX = 64

# Set on request.state by app.api.deps.get_current_user, and ONLY after that dependency has
# fully validated the bearer token -- signature, `access` type, user exists, is_active, and a
# token_version still matching the user's current one. Nothing else writes it, so its presence
# means "this request is authenticated", and reading it here cannot weaken or bypass any of
# those checks: an unauthenticated or rejected request simply never sets it.
RATE_LIMIT_SUBJECT_ATTR = "rate_limit_subject"


def user_or_ip_key(request: Request) -> str:
    """Rate-limit identity: the authenticated user when there is one, else the client address.

    Keying every limit by IP (the previous behaviour) failed in both directions at once. A
    college, hostel or campus network behind one NAT put every student in a single bucket, so
    twenty JD analyses an hour was the ceiling for the whole institution and login capped at
    five a minute for everyone on it -- the exact audience this product is for. In the other
    direction, one account moving between IPs faced no limit at all, so nothing capped a single
    student's AI spend, which is the cost this limiter exists to bound.

    Keying by user fixes both, because the thing worth limiting on an authenticated route is
    the account, not the network it happens to be on.

    IP remains the key for genuinely unauthenticated routes -- login, signup, password reset --
    where no account exists yet and the address is the only identity available. That is also
    the one place IP-keying is the right tool, since the threat there is guessing someone
    else's credentials rather than overspending your own budget.

    The two forms are prefixed so they share no namespace: a user id of 42 and an IP of "42"
    can never collide into one bucket.

    NOTE the ordering this relies on, which is verified by
    tests/unit/test_rate_limit_identity.py rather than assumed: slowapi evaluates a route's
    decorated limits inside the decorator's own wrapper (Limiter._check_request_limit only
    collects route limits when in_middleware is False), which runs after FastAPI has resolved
    the endpoint's dependencies. SlowAPIMiddleware evaluates only application/default limits,
    of which this app configures none. So get_current_user has always run by the time this is
    called on an authenticated route.
    """
    subject = getattr(request.state, RATE_LIMIT_SUBJECT_ATTR, None)
    if subject is not None:
        return f"user:{subject}"
    return f"ip:{normalise_client_address(get_remote_address(request))}"


def normalise_client_address(address: str) -> str:
    """One client, one key -- for both address families.

    `get_remote_address` returns `request.client.host`, which behind the deployed Nginx is the
    real client address (see `app/server.py` for how that is guaranteed, and
    `tests/unit/test_proxy_trust.py` for what happens when it is not). This turns that address
    into a stable identity.

    Three cases, and the middle one is the one worth reading:

    * **IPv4** -- returned as parsed. One address, one identity, unchanged behaviour.
    * **IPv4-mapped IPv6** (`::ffff:198.51.100.7`) -- unwrapped to the IPv4 address it carries,
      and this is checked *before* any prefix is applied. It has to be: the mapped range is
      `::ffff:0:0/96`, so a /64 of any mapped address is `::/64`, which contains every
      IPv4-mapped address there is. Prefixing first would put the entire IPv4 internet into a
      single rate-limit bucket -- login would cap at 5/minute globally. This ordering is the
      difference between a correct limiter and a self-inflicted outage.
    * **IPv6** -- truncated to its /64 network, for the reason given at `IPV6_KEY_PREFIX`.

    Anything unparseable is returned unchanged rather than rejected. `request.client.host` is
    not always an IP: Starlette's `TestClient` reports `"testclient"`, and a Unix-socket peer
    has no address at all. Those are still usable as keys -- they are stable strings and they
    group exactly one caller -- so there is nothing to fail closed about. A caller cannot reach
    this with a value of their choosing anyway: the address comes from the socket or from
    Nginx, never from a header the caller wrote.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address

    if parsed.version == 4:
        return str(parsed)

    mapped = parsed.ipv4_mapped
    if mapped is not None:
        return str(mapped)

    return str(ipaddress.ip_network(f"{parsed}/{IPV6_KEY_PREFIX}", strict=False))

# Storage is slowapi's in-memory default -- no Redis/Memcached, so no extra infrastructure to
# run this. The counters are PER PROCESS, which this fix does not change: behind multiple
# workers or instances each process enforces its own copy of every limit, so the real ceiling
# is the configured limit times the number of processes. That bounds abuse and cost; it is not
# an exact global quota and must not be described as one. A genuinely global limiter needs
# shared storage, which is a new infrastructure dependency and a deployment decision -- see
# docs/PRODUCTION_DEPLOYMENT.md.
#
# The deployed topology therefore runs exactly ONE worker and ONE instance, and that is a
# security setting rather than a capacity choice: with one process there is one copy of every
# counter, so the configured limit is the real limit. app/server.py fixes workers=1 for this
# reason. Adding a second worker, a second Droplet or a load balancer multiplies every limit
# here by the process count, silently, with no error and no log line -- see the scaling gate in
# docs/PRODUCTION_DEPLOYMENT.md. Counters also live only in memory, so a restart or deploy
# resets them; that is accepted for now and recorded there too.
#
# DEPLOYMENT-SENSITIVE, for the IP half of the key only. get_remote_address returns
# `request.client.host`, the socket peer, so behind a reverse proxy what it sees depends
# entirely on how Uvicorn is started:
#
#   * No --proxy-headers: every request appears to come from the proxy's own address, so all
#     unauthenticated traffic shares one bucket. Login then caps at 5/minute across the entire
#     user base -- an availability problem (one attacker locks everyone out), and the
#     per-attacker brute-force protection those limits exist for applies to nobody.
#
#   * --proxy-headers with --forwarded-allow-ips="*": Uvicorn rewrites the client address from
#     an X-Forwarded-For sent by *anyone*, so the key becomes attacker-controlled. Rotating one
#     header value per request defeats every IP-keyed limit, including login and password
#     reset. README's HTTPS section suggests "*" because it was written about X-Forwarded-Proto
#     and HSTS; that is safe for the scheme and NOT safe for this key function.
#
# The correct configuration is --proxy-headers together with --forwarded-allow-ips set to the
# actual proxy address, never "*".
#
# This is now enforced rather than written down, which is what Phase 2B changed. The claim that
# used to stand here -- that the process cannot check its own proxy trust -- was wrong in a
# useful way: Uvicorn resolves `forwarded_allow_ips` through the FORWARDED_ALLOW_IPS
# environment variable when it is not passed explicitly, so `validate_for_production` in
# app/core/config.py can read exactly what the server will see, and refuses to boot on an
# unset, empty, wildcard, over-broad or unparseable policy.
#
# What remains true is narrower: a command line can still contradict the environment, and
# ProxyHeadersMiddleware wraps the ASGI app from outside, so nothing in here can observe the
# flag that was actually used. That hole is closed by having one canonical start --
# app/server.py, which accepts no arguments and verifies the middleware object it built --
# rather than by a check in this module.
#
# Switching authenticated routes to a user key (above) narrows the blast radius of getting this
# wrong -- a student's JD budget no longer depends on their network, and no forwarded header
# can move them into someone else's bucket -- but it does not remove the requirement, because
# login, signup and password reset still have no account to key on and are exactly the limits
# an attacker would want to escape.
limiter = Limiter(key_func=user_or_ip_key)
