from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory storage (slowapi's default) -- no Redis/Memcached, so no paid/extra
# infrastructure to run this. Per-process counters: fine for a single-instance deployment;
# behind multiple app processes/workers each process enforces its own limit independently
# (not a shared global limit) -- acceptable for the brute-force/abuse protection this exists
# for, not a hard billing-accuracy guarantee.
#
# DEPLOYMENT-SENSITIVE, and it fails in opposite directions. get_remote_address returns
# `request.client.host`, which is the *socket peer*, so behind a reverse proxy the value it
# sees depends entirely on how Uvicorn is started:
#
#   * No --proxy-headers: every request appears to come from the proxy's own IP, so all users
#     share a single bucket. Login then caps at 5/minute across the entire user base -- an
#     availability problem (one attacker locks everyone out), and the per-attacker
#     brute-force protection this decorator exists for does not actually apply to anyone.
#
#   * --proxy-headers with --forwarded-allow-ips="*": Uvicorn rewrites the client address
#     from X-Forwarded-For sent by *anyone*, so the rate-limit key becomes attacker-
#     controlled. Rotating one header value per request defeats every limit in this app,
#     including the login and password-reset limits. README's HTTPS section suggests "*" as
#     an option, because it was written about X-Forwarded-Proto and HSTS; that advice is
#     safe for the scheme and NOT safe for this key function.
#
# The correct configuration is --proxy-headers together with --forwarded-allow-ips set to the
# actual proxy address(es), never "*" where the platform exposes a fixed IP or CIDR. That is
# an operational setting this module cannot enforce from inside the process, which is exactly
# why it is written down here rather than assumed.
limiter = Limiter(key_func=get_remote_address)
