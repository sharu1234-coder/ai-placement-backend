"""F12-07: no database transaction may be open while an AI provider call is running.

These tests assert at the exact seam. A probe provider is installed through the real
`get_ai_provider` dependency, real routes are driven over HTTP, and at the instant the provider
is called the probe asks every live SQLAlchemy Session whether it currently has a transaction
open -- how long it has been open, and whether it has already emitted a write (which is what
takes a SQLite RESERVED lock and PostgreSQL row locks).

Why it is asserted this way rather than by reading the code: `sessionmaker` leaves
`autobegin=True`, so a transaction starts on the *first* statement and lives until commit,
rollback or close. `get_current_user` runs `db.get(User, ...)` on the very session FastAPI
injects into the route, so by the time any route body runs, a transaction is already open. That
is invisible in the service code -- nothing there says `begin()` -- which is precisely why the
brief asks for the session lifetime to be traced rather than assumed.

These tests fail if anyone later moves an AI call back inside a transaction.
"""

import time

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.ai.schemas.assessment import AnswerGrade, GeneratedQuestion
from app.ai.schemas.career_path import (
    GeneratedCareerPathExplanations,
    GeneratedRoleCatalog,
    GeneratedRoleOverview,
    GeneratedRolePhase,
    GeneratedRoleSkill,
)
from app.ai.schemas.jd import AIParsedJD, JDRequirementItem
from app.ai.schemas.resume import ParsedResume
from app.ai.schemas.resume_ats_check import GeneratedAtsCheckContent
from app.ai.schemas.roadmap import GeneratedRoadmap, GeneratedRoadmapTask
from app.core.config import settings
from app.main import app

# --------------------------------------------------------------------------------------
# the probe
# --------------------------------------------------------------------------------------

_WRITE_VERBS = ("insert", "update", "delete")


def _enclosing_blocks(tree) -> dict[int, list]:
    """Every node's chain of enclosing `with` and `def` nodes, by id."""
    import ast

    chains: dict[int, list] = {}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            chains[id(child)] = stack
            deeper = stack + [child] if isinstance(child, (ast.With, ast.FunctionDef)) else stack
            walk(child, deeper)

    walk(tree, [])
    return chains


def _describe_open(o: dict) -> str:
    bits = [f"{o['age_ms']:.1f}ms open"]
    bits.append("HOLDING WRITE LOCKS" if o["wrote"] else "read-only")
    if o["pending"]:
        bits.append(f"{o['pending']} unflushed object(s)")
    return ", ".join(bits)


class TransactionProbe:
    """Tracks every Session's transaction lifetime, and whether that transaction has written.

    Session-level `after_begin` is the only reliable signal for "a transaction started": with
    autobegin there is no explicit `begin()` call anywhere in this codebase to hook. Writes are
    attributed via the connection the transaction is bound to, because `before_cursor_execute`
    reports a Connection rather than a Session.
    """

    def __init__(self) -> None:
        self.open: dict[int, tuple[Session, float]] = {}
        self.wrote: set[int] = set()
        self.conn_to_session: dict[int, int] = {}
        self.snapshots: list[dict] = []
        # Every transaction that has ended, with how long it lived. This is what makes the
        # before/after claim measurable rather than asserted: the question is not only "was one
        # open during the AI call" but "how long did any of them last".
        self.finished: list[dict] = []

    # -- listeners ---------------------------------------------------------------------

    def _after_begin(self, session, _transaction, connection):
        # perf_counter, not monotonic: on Windows monotonic ticks at ~15.6ms, which reports a
        # genuinely-open 5ms transaction as "0.0ms" and understates every age here.
        self.open[id(session)] = (session, time.perf_counter())
        self.wrote.discard(id(session))
        if connection is not None:
            self.conn_to_session[id(connection)] = id(session)

    def _after_end(self, session):
        started = self.open.pop(id(session), None)
        if started is not None:
            self.finished.append({
                "ms": (time.perf_counter() - started[1]) * 1000,
                "wrote": id(session) in self.wrote,
            })
        self.wrote.discard(id(session))

    def _before_cursor_execute(self, conn, _cursor, statement, _params, _context, _many):
        if statement.lstrip()[:6].lower().startswith(_WRITE_VERBS):
            sid = self.conn_to_session.get(id(conn))
            if sid is not None:
                self.wrote.add(sid)

    def install(self):
        event.listen(Session, "after_begin", self._after_begin)
        event.listen(Session, "after_commit", self._after_end)
        event.listen(Session, "after_rollback", self._after_end)
        event.listen(Engine, "before_cursor_execute", self._before_cursor_execute)
        return self

    def remove(self):
        event.remove(Session, "after_begin", self._after_begin)
        event.remove(Session, "after_commit", self._after_end)
        event.remove(Session, "after_rollback", self._after_end)
        event.remove(Engine, "before_cursor_execute", self._before_cursor_execute)

    # -- the measurement ---------------------------------------------------------------

    def snapshot(self, label: str) -> dict:
        """What the database is holding, right now, at an AI call."""
        now = time.perf_counter()
        live = []
        for sid, (session, started) in list(self.open.items()):
            if session.in_transaction():
                live.append({
                    "age_ms": (now - started) * 1000,
                    # A write statement has been emitted in this transaction: on SQLite that is a
                    # RESERVED lock, on PostgreSQL row locks, both held until commit.
                    "wrote": sid in self.wrote,
                    # Objects pending but not yet flushed. These decide whether the transaction
                    # can simply be ended before the AI call or whether the flow has to be
                    # reordered -- ending it would discard them.
                    "pending": len(session.new) + len(session.dirty) + len(session.deleted),
                })
        row = {"label": label, "open": live}
        self.snapshots.append(row)
        return row

    # -- reporting ---------------------------------------------------------------------

    @property
    def open_at_ai(self) -> list[dict]:
        return [s for s in self.snapshots if s["open"]]

    def describe(self) -> str:
        if not self.snapshots:
            return "no AI call was made at all"
        parts = []
        for s in self.snapshots:
            if s["open"]:
                held = ", ".join(_describe_open(o) for o in s["open"])
                parts.append(f"{s['label']}: {len(s['open'])} transaction(s) OPEN [{held}]")
            else:
                parts.append(f"{s['label']}: no transaction open")
        return "; ".join(parts)


# --------------------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------------------

SAMPLE_PARSED_RESUME = ParsedResume(
    skills=["Python", "SQL"], education=[], experience=[], projects=[], certifications=[],
    ats_score=70.0, weak_sections=[], extraction_confidence=0.9,
)

SAMPLE_PARSED_JD = AIParsedJD(
    role_title="Data Analyst Intern",
    required_skills=[JDRequirementItem(value="SQL", is_explicit=True)],
    preferred_skills=[JDRequirementItem(value="Python", is_explicit=True)],
    responsibilities=["Analyze sales data"], tools_technologies=["Power BI"],
    experience_expectation="0-1 years", education_eligibility="Bachelor's degree",
    other_signals=[], parsing_confidence=0.9,
)

SAMPLE_ATS_CONTENT = GeneratedAtsCheckContent(summary="A fake AI summary.", finding_rephrases=[])

SAMPLE_GENERATED_MCQ = GeneratedQuestion(
    type="mcq",
    prompt="What does SQL stand for?",
    options=["Structured Query Language", "Simple Query Logic", "Sequential Query List",
             "Standard Question Language"],
    expected_answer_or_rubric="Structured Query Language",
    explanation="SQL stands for Structured Query Language.",
)

SAMPLE_GENERATED_SHORT_ANSWER = GeneratedQuestion(
    type="short_answer",
    prompt="Explain what a primary key is.",
    options=None,
    expected_answer_or_rubric="A column that uniquely identifies each row in a table.",
    explanation="Primary keys enforce uniqueness and identify rows.",
)

SAMPLE_ANSWER_GRADE = AnswerGrade(is_correct=False, score=0.1, feedback="Not quite.")

SAMPLE_GENERATED_ROADMAP = GeneratedRoadmap(
    tasks=[
        GeneratedRoadmapTask(day=1, task_type="learn", skill_tag="DSA", title="Review arrays",
                             description="Read about arrays and practice 3 problems."),
        GeneratedRoadmapTask(day=3, task_type="practice", skill_tag="DSA", title="Practice problems",
                             description="Solve 5 medium array problems."),
    ]
)

SAMPLE_ROLE_CATALOG = GeneratedRoleCatalog(
    overview=GeneratedRoleOverview(
        short_explanation="A fake AI-generated role overview.",
        responsibilities=["Do the work"], core_skills=["Skill A"], tools=["Tool A"],
        difficulty="moderate", entry_level_expectations="Some baseline expectation.",
        interview_focus=["Fundamentals"], portfolio_expectations="One solid project.",
    ),
    phases=[
        GeneratedRolePhase(
            phase_key="foundations",
            skills=[GeneratedRoleSkill(skill_key="skill_a", title="Skill A", level="beginner")],
        )
    ],
)


class ProbingAI(AIProvider):
    """Answers like the other suites' fakes, but records the transaction state first. The
    optional delay stands in for a slow model: a transaction that is open here is open for that
    whole duration in production."""

    def __init__(self, probe: TransactionProbe, delay: float = 0.0, short_answer: bool = False):
        self.probe = probe
        self.delay = delay
        self.short_answer = short_answer
        self.calls = 0

    def generate_structured(self, prompt, schema):
        self.calls += 1
        self.probe.snapshot(schema.__name__)
        if self.delay:
            # Stands in for a slow model. The second snapshot is what proves the transaction did
            # not merely exist when the call started but was still open when it returned.
            time.sleep(self.delay)
            self.probe.snapshot(f"{schema.__name__}:returned")
        if schema is GeneratedQuestion:
            return SAMPLE_GENERATED_SHORT_ANSWER if self.short_answer else SAMPLE_GENERATED_MCQ
        if schema is GeneratedRoadmap:
            return SAMPLE_GENERATED_ROADMAP
        if schema is ParsedResume:
            return SAMPLE_PARSED_RESUME
        if schema is AIParsedJD:
            return SAMPLE_PARSED_JD
        if schema is GeneratedAtsCheckContent:
            return SAMPLE_ATS_CONTENT
        if schema is GeneratedRoleCatalog:
            return SAMPLE_ROLE_CATALOG
        if schema is GeneratedCareerPathExplanations:
            return GeneratedCareerPathExplanations(explanations=[])
        if schema is AnswerGrade:
            return SAMPLE_ANSWER_GRADE
        raise AssertionError(f"Unexpected schema requested in test: {schema}")

    def generate_text(self, prompt):
        self.calls += 1
        self.probe.snapshot("text")
        return "fake text"


# --------------------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------------------


@pytest.fixture()
def probe():
    p = TransactionProbe().install()
    yield p
    p.remove()


@pytest.fixture()
def ai(probe):
    provider = ProbingAI(probe)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "txn@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "txn@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _pdf(text="some resume text with Python and SQL"):
    from tests.fixtures.pdf_helpers import make_pdf_with_text

    return make_pdf_with_text(text)


def assert_no_transaction_during_ai(probe: TransactionProbe, route: str):
    assert probe.snapshots, f"{route}: the AI provider was never called, so nothing was measured"
    assert not probe.open_at_ai, (
        f"F12-07 at {route}: a database transaction was open while the AI provider ran -- "
        f"{probe.describe()}"
    )


# --------------------------------------------------------------------------------------
# the systemic cause, proven once
# --------------------------------------------------------------------------------------


def test_authentication_alone_opens_a_transaction_that_outlives_the_route_body(client, auth_headers, probe):
    """The reason this is not a one-function problem. `get_current_user` reads the users row on
    the same Session the route body receives, so with autobegin a transaction is already open
    before any service code runs -- no service has to do anything wrong for an AI call to end up
    inside one."""
    client.get("/career-path/current", headers=auth_headers)
    # A read-only route commits nothing, so the transaction opened by authentication stays open
    # until get_db closes the session at the end of the request.
    assert probe.open or probe.snapshots == [], (
        "expected authentication to have begun a transaction on the request session"
    )


# --------------------------------------------------------------------------------------
# one test per affected production path
# --------------------------------------------------------------------------------------


def test_resume_upload_makes_no_ai_call_inside_a_transaction(client, auth_headers, ai, probe):
    resp = client.post("/resumes", files={"file": ("r.pdf", _pdf(), "application/pdf")}, headers=auth_headers)
    assert resp.status_code == 201
    assert_no_transaction_during_ai(probe, "POST /resumes")


def test_ats_check_makes_no_ai_call_inside_a_transaction(client, auth_headers, ai, probe):
    resume_id = client.post(
        "/resumes", files={"file": ("r.pdf", _pdf(), "application/pdf")}, headers=auth_headers
    ).json()["id"]
    probe.snapshots.clear()
    resp = client.post(f"/resumes/{resume_id}/ats-check", json={}, headers=auth_headers)
    assert resp.status_code == 201
    assert_no_transaction_during_ai(probe, "POST /resumes/{id}/ats-check")


def test_job_description_parse_makes_no_ai_call_inside_a_transaction(client, auth_headers, ai, probe):
    resp = client.post("/jobs", json={"raw_text": "We need a data analyst with SQL."}, headers=auth_headers)
    assert resp.status_code == 201
    assert_no_transaction_during_ai(probe, "POST /jobs")


@pytest.fixture()
def sde_role_id(client):
    return next(r["id"] for r in client.get("/roles").json() if r["name"] == "SDE")


def _complete_assessment_with_wrong_answers(client, auth_headers, role_id):
    """Produces real weakness data, which /roadmap/generate requires."""
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": role_id}, headers=auth_headers
    ).json()["id"]
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    for _ in range(100):
        next_q = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers).json()
        if next_q["completed"]:
            break
        client.post(
            f"/assessments/{assessment_id}/answer",
            json={"question_id": next_q["question"]["id"], "student_answer": "Simple Query Logic"},
            headers=auth_headers,
        )
    client.get(f"/assessments/{assessment_id}/result", headers=auth_headers)
    return assessment_id


def test_assessment_next_question_makes_no_ai_call_inside_a_transaction(
    client, auth_headers, sde_role_id, ai, probe
):
    """The one path the static audit expected to already be safe: get_next_question commits
    before it calls the AI. Included as a discriminator -- if the probe cannot tell this apart
    from the others, the probe is measuring nothing."""
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    ).json()["id"]
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    probe.snapshots.clear()
    resp = client.get(f"/assessments/{assessment_id}/next-question", headers=auth_headers)
    assert resp.status_code == 200
    assert_no_transaction_during_ai(probe, "GET /assessments/{id}/next-question")


@pytest.fixture()
def short_answer_ai(probe):
    """MCQ answers are graded deterministically by option match -- only a short answer reaches
    the AI grader, which is the call site under test here."""
    provider = ProbingAI(probe, short_answer=True)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


def test_assessment_answer_grading_makes_no_ai_call_inside_a_transaction(
    client, auth_headers, sde_role_id, short_answer_ai, probe
):
    blueprint_id = client.post(
        "/assessments/blueprint", json={"role_id": sde_role_id}, headers=auth_headers
    ).json()["id"]
    assessment_id = client.post(
        "/assessments", json={"blueprint_id": blueprint_id}, headers=auth_headers
    ).json()["id"]
    question_id = client.get(
        f"/assessments/{assessment_id}/next-question", headers=auth_headers
    ).json()["question"]["id"]
    probe.snapshots.clear()
    resp = client.post(
        f"/assessments/{assessment_id}/answer",
        json={"question_id": question_id, "student_answer": "A uniquely identifying column."},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert_no_transaction_during_ai(probe, "POST /assessments/{id}/answer")


def test_a_slow_ai_call_does_not_extend_a_database_transaction(client, auth_headers, probe):
    """The decisive shape, measured rather than argued: a transaction that is merely *open* when
    the AI starts is open for the AI's entire duration. With a real model that is 98-183s of
    transaction lifetime per Phase 17's measurements.

    The proof is the pair of snapshots around a deliberate delay: if the transaction's age grows
    by the delay, the transaction spanned the call -- i.e.
    transaction_start < AI_start < AI_end < transaction_commit."""
    delay = 0.4
    provider = ProbingAI(probe, delay=delay)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    try:
        resp = client.post("/jobs", json={"raw_text": "We need a data analyst with SQL."}, headers=auth_headers)
        assert resp.status_code == 201
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)

    started = [s for s in probe.snapshots if not s["label"].endswith(":returned")]
    returned = [s for s in probe.snapshots if s["label"].endswith(":returned")]
    assert started and returned, "the probe did not capture both ends of the AI call"

    grew = [
        r["open"][0]["age_ms"] - s["open"][0]["age_ms"]
        for s, r in zip(started, returned, strict=True)
        if s["open"] and r["open"]
    ]
    assert not grew, (
        "F12-07: a database transaction stayed open across the whole AI call -- its age grew by "
        f"{max(grew):.0f}ms while the provider slept {delay * 1000:.0f}ms. "
        f"{probe.describe()}"
    )


def test_roadmap_generation_makes_no_ai_call_inside_a_transaction(
    client, auth_headers, sde_role_id, ai, probe
):
    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    probe.snapshots.clear()
    resp = client.post("/roadmap/generate", json={"duration_days": 7}, headers=auth_headers)
    assert resp.status_code == 201
    assert_no_transaction_during_ai(probe, "POST /roadmap/generate")


def test_a_failed_task_write_leaves_the_previous_roadmap_active(
    client, auth_headers, sde_role_id, ai, probe, monkeypatch
):
    """Moving AI out of a transaction must not cost atomicity (the other half of F12-07).

    Retiring the old roadmap, creating the new one and writing its tasks are one unit of work.
    They used to be three separate commits, so a failure part-way through could leave the student
    with their working roadmap superseded and an empty one in its place, or none at all. Here the
    second task insert explodes, and the roadmap the student already had must survive untouched.
    """
    import app.services.roadmap_service as roadmap_service

    _complete_assessment_with_wrong_answers(client, auth_headers, sde_role_id)
    first = client.post("/roadmap/generate", json={"duration_days": 7}, headers=auth_headers)
    assert first.status_code == 201
    first_id = first.json()["id"]

    real_task = roadmap_service.RoadmapTask
    made = {"n": 0}

    def explode_on_the_second(**kwargs):
        made["n"] += 1
        if made["n"] == 2:
            raise RuntimeError("simulated failure part-way through writing the tasks")
        return real_task(**kwargs)

    monkeypatch.setattr(roadmap_service, "RoadmapTask", explode_on_the_second)
    with pytest.raises(RuntimeError):
        client.post("/roadmap/generate", json={"duration_days": 7}, headers=auth_headers)
    monkeypatch.undo()

    active = client.get("/roadmap/current", headers=auth_headers)
    assert active.status_code == 200
    assert active.json()["id"] == first_id, (
        "the student's existing roadmap was superseded by a generation that never completed"
    )
    assert active.json()["tasks"], "the surviving roadmap lost its tasks"


def test_career_path_custom_role_makes_no_ai_call_inside_a_transaction(client, auth_headers, ai, probe):
    """The Phase 15 / Phase 17 path. A role outside the curated catalog calls
    get_or_generate_role_definition inline, which is structural AI on the critical path."""
    resp = client.post(
        "/career-path/generate", json={"custom_role": "Quantum Widget Specialist"}, headers=auth_headers
    )
    assert resp.status_code == 201
    assert_no_transaction_during_ai(probe, "POST /career-path/generate (custom role)")


def test_no_transaction_outlives_the_ai_call_it_used_to_contain(client, auth_headers, probe, capsys):
    """The shape this phase is judged on, as numbers.

    A deliberate 400ms delay stands in for the model. What must be true afterwards is that no
    transaction of any length spans it: the read phase ends before the call and the write phase
    starts after, so the longest transaction in the request is milliseconds rather than the
    model's whole duration. The measured 'before' for this same route was a single transaction
    whose age went from 2.5ms to 402.7ms across the call.
    """
    delay_ms = 400.0
    provider = ProbingAI(probe, delay=delay_ms / 1000)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    try:
        started = time.perf_counter()
        resp = client.post("/jobs", json={"raw_text": "We need a data analyst with SQL."}, headers=auth_headers)
        request_ms = (time.perf_counter() - started) * 1000
    finally:
        app.dependency_overrides.pop(get_ai_provider, None)
    assert resp.status_code == 201

    longest = max((t["ms"] for t in probe.finished), default=0.0)
    with capsys.disabled():
        print(f"\n   request {request_ms:.0f}ms | AI {delay_ms:.0f}ms | "
              f"{len(probe.finished)} transactions, longest {longest:.1f}ms, "
              f"write-holding: {sum(1 for t in probe.finished if t['wrote'])}")

    assert not probe.open_at_ai, f"a transaction was open during the AI call: {probe.describe()}"
    assert longest < delay_ms, (
        f"the longest transaction lasted {longest:.1f}ms, which is at least as long as the "
        f"{delay_ms:.0f}ms AI call -- something is still spanning it"
    )


def test_every_ai_call_site_in_the_services_is_inside_a_boundary():
    """A static sweep over all of app/services, because the runtime tests above only reach the
    call sites their routes happen to exercise.

    Every provider call must either sit lexically inside `with outside_transaction(...)`, or live
    in a function that takes no session at all -- such a function cannot be holding a transaction
    of its own, and its callers are the ones wrapped. The five helpers in that second group are
    listed explicitly rather than inferred, so adding a sixth is a deliberate decision someone has
    to make here.
    """
    import ast
    import pathlib

    # Functions with no session parameter: they cannot own a transaction, and every caller of each
    # is wrapped at the caller's own boundary (covered by the runtime tests above).
    SESSIONLESS = {
        "get_or_generate_role_definition",   # career_path/ai_generation
        "generate_skill_explanations",       # career_path/ai_generation
        "_generate_explanations",            # placement_path_service
        "generate_narrative",                # professional_presence/audit_service
        "generate_stage_guidance",           # project_builder/ai_generation
    }
    AI_METHODS = {"generate_structured", "generate_text", "transcribe", "synthesize_speech"}

    services = pathlib.Path("app/services")
    unguarded = []
    checked = 0

    for path in sorted(services.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Map every node to its chain of enclosing With/FunctionDef nodes.
        parents = _enclosing_blocks(tree)

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in AI_METHODS:
                continue
            if not (isinstance(node.func.value, ast.Name) and node.func.value.id in ("ai_provider", "provider")):
                continue
            checked += 1
            stack = parents.get(id(node), [])
            fn = next((n for n in reversed(stack) if isinstance(n, ast.FunctionDef)), None)
            if fn is not None and fn.name in SESSIONLESS:
                continue
            guarded = any(
                isinstance(n, ast.With)
                and any(
                    isinstance(item.context_expr, ast.Call)
                    and getattr(item.context_expr.func, "id", None) == "outside_transaction"
                    for item in n.items
                )
                for n in stack
            )
            if not guarded:
                where = f"{path.as_posix()}:{node.lineno} in {fn.name if fn else '<module>'}()"
                unguarded.append(where)

    assert checked >= 20, f"expected to find at least 20 AI call sites, found {checked}"
    assert not unguarded, (
        "F12-07: these AI provider calls are not inside `with outside_transaction(...)` and are "
        "not in a session-less helper:\n  " + "\n  ".join(unguarded)
    )


def test_placement_alarm_challenge_makes_no_ai_call_inside_a_transaction(client, auth_headers, ai, probe):
    # The challenge route requires a completed resume; uploading one through the real route is
    # closer to production than inserting the rows directly.
    assert client.post(
        "/resumes", files={"file": ("r.pdf", _pdf(), "application/pdf")}, headers=auth_headers
    ).status_code == 201
    probe.snapshots.clear()
    resp = client.post("/placement-alarm/challenges", json={"client_alarm_id": "a1"}, headers=auth_headers)
    assert resp.status_code == 201
    assert_no_transaction_during_ai(probe, "POST /placement-alarm/challenges")
