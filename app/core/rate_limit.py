from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

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
    return f"ip:{get_remote_address(request)}"

# Storage is slowapi's in-memory default -- no Redis/Memcached, so no extra infrastructure to
# run this. The counters are PER PROCESS, which this fix does not change: behind multiple
# workers or instances each process enforces its own copy of every limit, so the real ceiling
# is the configured limit times the number of processes. That bounds abuse and cost; it is not
# an exact global quota and must not be described as one. A genuinely global limiter needs
# shared storage, which is a new infrastructure dependency and a deployment decision -- see
# docs/PRODUCTION_DEPLOYMENT.md.
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
# actual proxy address or CIDR, never "*". This module cannot enforce that from inside the
# process, which is why it is written down here and in the deployment guide rather than
# assumed.
#
# Switching authenticated routes to a user key (above) narrows the blast radius of getting this
# wrong -- a student's JD budget no longer depends on their network, and no forwarded header
# can move them into someone else's bucket -- but it does not remove the requirement, because
# login, signup and password reset still have no account to key on and are exactly the limits
# an attacker would want to escape.
limiter = Limiter(key_func=user_or_ip_key)
