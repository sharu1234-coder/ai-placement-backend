"""Shared harness for Phase 30's replay tests: a real database, and a certain interleaving.

Two things have to be true for a test here to be able to fail, and neither is the default.

**One connection per session.** conftest's client fixture uses an in-memory database with
StaticPool, which hands every session the SAME connection and therefore the same transaction. Two
"concurrent" requests on it each see the other's uncommitted INSERT, no constraint can fire, and
the test passes whatever the code does. Measured, not assumed: Phase 30's first draft used
StaticPool and reported a race that genuinely existed as safe. So these tests run against a real
file with a normal pool, which gives each request its own connection exactly as production does.

**A barrier at the write.** Releasing racing threads at the start of the request only makes the
race likely. These endpoints read some row, decide it has not been written yet, and then write --
so the window is between that read and that write, and a thread sitting at its first INSERT into
the table in question has necessarily already passed the read. Holding every thread there and
releasing them together makes the interleaving certain rather than lucky. It is the same technique
as tests/fixtures/career_path_helpers.py's pre-write barrier, hooked at the SQL rather than at an
AI call, because these endpoints call no model.

The gate is a no-op until a test arms it, so importing this module changes nothing for any other
test.
"""

import threading
from typing import Optional

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db
from app.core.config import settings
from app.main import app
from app.models.profile import StudentProfile

PASSWORD = "supersecret123"

#: Bounds a *failing* test, not a passing one: a thread that never gets its partner has to end as
#: a readable assertion rather than hanging the suite.
GATE_TIMEOUT = 30

_gate: Optional[tuple] = None

#: The threads the gate has held, by identity. A test that stages a race has to be able to say the
#: race was staged, and "how many statements did I hold" is not enough to say it: a gate that
#: holds one thread twice satisfies a two-party barrier all by itself, which lets the other request
#: run to completion first and the test then pass as two sequential requests. That is not a
#: hypothetical -- it is how apply_jd's race first reported itself as safe.
gated_threads: set = set()


def arm_gate(match: str, parties: int = 2) -> threading.Barrier:
    """Hold each thread once, at its first statement containing `match`, until `parties` are there.

    Once per *thread* rather than once overall, for two separate reasons. It is what makes the
    interleaving a fact instead of a hope (above). And SQLite allows one writer at a time, so a
    gate that can hold the same request twice deadlocks when the endpoint writes more than one row
    -- one thread holds the write lock while waiting at the barrier for a thread that is blocked on
    that very lock. (Measured: gated at the INSERT, apply_jd's two requests both came back 500
    after the barrier timed out.) Gating the *read* and letting the writes serialise however the
    database likes is both deadlock-free and all the race needs.
    """
    global _gate
    gated_threads.clear()
    barrier = threading.Barrier(parties, timeout=GATE_TIMEOUT)
    _gate = (barrier, match.upper())
    return barrier


def disarm_gate() -> None:
    global _gate
    _gate = None


def make_engine(db_path) -> object:
    eng = create_engine(
        f"sqlite:///{db_path}",
        # check_same_thread=False because the sync route runs in Starlette's threadpool, so a
        # session is used by a different thread than created it -- the same reason
        # app/database/session.py passes it. timeout keeps a slow machine's lock contention from
        # being mistaken for the failure under test.
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(eng, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(eng, "before_cursor_execute")
    def _hold_at_statement(_conn, _cursor, statement, _params, _context, _many):
        gate = _gate
        if gate is None:
            return
        barrier, match = gate
        ident = threading.get_ident()
        if ident in gated_threads:
            return
        if match in " ".join(statement.upper().split()):
            gated_threads.add(ident)
            barrier.wait(timeout=GATE_TIMEOUT)

    return eng


def make_client(engine, tmp_path, monkeypatch) -> tuple:
    """The real app on a real database, one session per request exactly as production has."""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    sessions = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # The limiter's counters live on the shared app object, so a previous test's requests would
    # otherwise count against this one's quota. Same call, same reason, as conftest.
    app.state.limiter.reset()
    return TestClient(app, raise_server_exceptions=False), sessions


def signup(client, email: str = "p30@example.com") -> dict:
    assert client.post("/auth/signup", json={"email": email, "password": PASSWORD}).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": PASSWORD}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # get_or_create_profile runs on the first *authenticated* request, not at signup, so a test
    # that looks for the profile straight after logging in finds nothing.
    assert client.get("/users/me", headers=headers).status_code == 200
    return headers


def profile_id(db) -> int:
    """The profile the app made on the student's first authenticated request."""
    return db.query(StudentProfile).one().id


def post_together(client, url: str, headers: dict, *, gate: str, body=None, bodies=None) -> list:
    """Send two overlapping requests, both released together at the statement `gate` names.

    `body` sends the identical request twice (a transport replay). `bodies` sends a different one
    to each, which is what proves a write guard: two requests that ask for the *same* thing leave
    the same row behind whether the guard is there or not, so only differing requests can show
    that the loser was refused rather than allowed to overwrite the winner.
    """
    arm_gate(gate)
    payloads = bodies if bodies is not None else (body, body)
    responses: list = [None, None]

    def send(slot: int):
        responses[slot] = client.post(url, headers=headers, json=payloads[slot])

    threads = [threading.Thread(target=send, args=(slot,)) for slot in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=GATE_TIMEOUT * 2)
    assert all(not thread.is_alive() for thread in threads), "a request never finished"
    assert len(gated_threads) == 2, (
        f"the gate held {len(gated_threads)} of the 2 requests, so they were not actually "
        f"interleaved and this test proves nothing. Check that {gate!r} still appears in the "
        f"statement the endpoint issues at the seam."
    )
    return responses
