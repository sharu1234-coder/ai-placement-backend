from slowapi import Limiter
from slowapi.util import get_remote_address

# In-memory storage (slowapi's default) -- no Redis/Memcached, so no paid/extra
# infrastructure to run this. Per-process counters: fine for a single-instance deployment;
# behind multiple app processes/workers each process enforces its own limit independently
# (not a shared global limit) -- acceptable for the brute-force/abuse protection this exists
# for, not a hard billing-accuracy guarantee. keyed by client IP.
limiter = Limiter(key_func=get_remote_address)
