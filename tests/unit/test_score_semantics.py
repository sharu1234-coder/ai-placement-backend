"""Phase 2F -- every percentage says what it measures, and absence never becomes zero.

The product had one word, "readiness", naming four different formulas: a weighted mean of
evidence confidence (Career Twin, Career Decision), a checklist completion ratio (Career Path
summary), a five-signal average (Career Path job readiness) and a mean of the last test and
interview score (Progress). Only the third is readiness by any defensible definition.

Worse, three of them turned "we cannot compute this" into a confident 0.

These tests pin both halves: the NAME matches the formula, and the NO-DATA state is null rather
than zero. They are deliberately organised around the distinction that matters most --

    measured zero   the calculation ran and the answer is 0
    unmeasurable    the calculation could not run at all

-- because collapsing those two is how a student ends up being told they are 0% ready for a job
nobody has described to the app.
"""

import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main  # noqa: F401  -- registers every model with Base.metadata
from app.database.base import Base
from app.models.assessment import Assessment
from app.models.assessment_blueprint import AssessmentBlueprint
from app.models.assessment_result import AssessmentResult
from app.models.profile import StudentProfile
from app.models.readiness_snapshot import ReadinessSnapshot
from app.models.role import Role
from app.models.role_skill_weight import RoleSkillWeight
from app.services import progress_service
from app.services.career_twin_service import get_current

_UTC = datetime.timezone.utc


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _profile(db, target_role_id=None) -> StudentProfile:
    profile = StudentProfile(user_id=1, target_role_id=target_role_id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _role(db, name="SDE", weights=None) -> Role:
    role = Role(name=name, category="Engineering")
    db.add(role)
    db.commit()
    db.refresh(role)
    for skill, weight in (weights or {}).items():
        db.add(RoleSkillWeight(role_id=role.id, skill=skill, weight=weight, assessment_type="technical"))
    db.commit()
    return role


# ==========================================================================================
# Career Twin -- EVIDENCE COVERAGE
# ==========================================================================================


def test_a_role_with_no_defined_skills_is_unmeasurable_not_zero(db):
    """The `else 0` this phase removed.

    A role with no RoleSkillWeight rows gives nothing to weigh. The old code published 0, which
    asserts a measurement. It also meant the Android screen's own "Not enough data yet" branch
    could never fire, because the backend always sent a confident-looking number.
    """
    role = _role(db, weights={})
    profile = _profile(db, target_role_id=role.id)

    twin = get_current(db, profile)

    assert twin.evidence_coverage_percent is None, "no skills to weigh means no number, not zero"
    assert twin.has_role is True, "the role exists -- it is the measurement that does not"
    assert twin.reasons, "and the student is told why"


def test_an_unmeasurable_twin_writes_no_snapshot(db):
    """A snapshot asserts that a measurement was taken.

    readiness_snapshots.evidence_coverage_percent is NOT NULL, so storing the unmeasurable case
    either raise or persist a 0 that a later trend reads back as a real past score -- and then
    reports the student's first genuine measurement as a huge improvement they never made.
    """
    role = _role(db, weights={})
    profile = _profile(db, target_role_id=role.id)

    get_current(db, profile)

    assert db.query(ReadinessSnapshot).count() == 0


def test_an_unmeasurable_twin_has_no_trend(db):
    role = _role(db, weights={})
    profile = _profile(db, target_role_id=role.id)

    twin = get_current(db, profile)

    assert twin.evidence_coverage_trend_percent is None, "there is nothing to compare against nothing"


def test_a_measurable_role_with_no_evidence_is_a_measured_zero(db):
    """The other side of the distinction, and the reason this is not simply "null everywhere".

    The role defines skills, so the weighted mean genuinely evaluates -- to 0, because the
    student has no evidence yet. That is a real measurement and must stay a number.
    """
    role = _role(db, weights={"DSA": 1.0, "OOP": 0.7})
    profile = _profile(db, target_role_id=role.id)

    twin = get_current(db, profile)

    assert twin.evidence_coverage_percent == 0
    assert twin.evidence_coverage_percent is not None


def test_a_measured_zero_still_records_a_snapshot(db):
    """Because it IS a measurement -- the trend line should be able to show movement away
    from it later."""
    role = _role(db, weights={"DSA": 1.0})
    profile = _profile(db, target_role_id=role.id)

    get_current(db, profile)

    assert db.query(ReadinessSnapshot).count() == 1


def test_the_evidence_coverage_formula_is_unchanged(db):
    """Phase 2F is a semantics phase. If the number moved, something was redesigned that should
    not have been.

    One skill at weight 1.0 with no evidence: the weighted mean is 0 by construction, and the
    denominator is the total weight rather than a count -- the property that makes this a
    weighted mean at all.
    """
    role = _role(db, weights={"DSA": 1.0, "OOP": 0.5})
    profile = _profile(db, target_role_id=role.id)

    twin = get_current(db, profile)

    expected = round(100 * (1.0 * 0.0 + 0.5 * 0.0) / 1.5)
    assert twin.evidence_coverage_percent == expected


# ==========================================================================================
# Progress -- PERFORMANCE
# ==========================================================================================


def _completed_assessment(db, profile_id: int, score: float) -> None:
    blueprint = AssessmentBlueprint(
        role_id=None, jd_id=None, profile_id=profile_id, source="default_taxonomy", sections=[]
    )
    db.add(blueprint)
    db.commit()
    db.refresh(blueprint)
    assessment = Assessment(profile_id=profile_id, blueprint_id=blueprint.id, status="completed")
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    db.add(AssessmentResult(assessment_id=assessment.id, overall_score=score, skill_scores={}, priority_weaknesses=[]))
    db.commit()


def test_no_assessment_and_no_interview_is_not_zero_performance(db):
    """"Has not been measured" is not "performed badly". The field was already Optional here --
    this test exists so a later refactor cannot quietly default it to 0."""
    profile = _profile(db)

    dashboard = progress_service.get_dashboard(db, profile.id)

    assert dashboard.performance_score is None


def test_a_completed_assessment_scoring_zero_is_a_measured_zero(db):
    """A real 0 must survive. If "no data" and "scored 0" both became None, the honesty fix
    would have destroyed a genuine measurement in the other direction."""
    profile = _profile(db)
    _completed_assessment(db, profile.id, 0.0)

    dashboard = progress_service.get_dashboard(db, profile.id)

    assert dashboard.performance_score == 0.0
    assert dashboard.performance_score is not None


def test_performance_is_the_mean_of_what_was_actually_measured(db):
    """Formula unchanged: the mean of the signals that exist, never divided by a constant that
    counts signals which do not."""
    profile = _profile(db)
    _completed_assessment(db, profile.id, 0.5)

    dashboard = progress_service.get_dashboard(db, profile.id)

    assert dashboard.performance_score == pytest.approx(0.5)


# ==========================================================================================
# The cross-cutting rule
# ==========================================================================================


def test_no_percentage_field_in_the_product_is_named_readiness_unless_it_measures_readiness():
    """A guard against the regression Phase 2F existed to prevent, now complete.

    Only two things may carry the word: JobReadinessRead, which averages five grounded signals
    and is genuine readiness, and the career_strategy/job_hunt "apply readiness" family, which
    is its own documented concept.

    Phase 2F listed Career Twin here as a deliberate exception, because renaming its field
    reached the snapshot table and three career_strategy schemas. Phase 2G closed it. The
    exception is gone, and the assertions below are what stop it coming back.
    """
    from app.schemas.career_decision import CareerDecisionRoleRead
    from app.schemas.career_path import CareerPathSummaryRead
    from app.schemas.career_strategy import CareerStrategyRead, TargetStrategyRead
    from app.schemas.career_twin import CareerTwinRead, CareerTwinReadinessRead
    from app.schemas.progress import ProgressDashboardRead

    assert "readiness_percent" not in CareerPathSummaryRead.model_fields
    assert "preparation_percent" in CareerPathSummaryRead.model_fields

    assert "readiness_percent" not in CareerDecisionRoleRead.model_fields
    assert "evidence_coverage_percent" in CareerDecisionRoleRead.model_fields

    assert "readiness_score" not in ProgressDashboardRead.model_fields
    assert "performance_score" in ProgressDashboardRead.model_fields

    # Phase 2G: the last of them.
    for model in (CareerTwinRead, CareerTwinReadinessRead):
        assert "readiness_percent" not in model.model_fields
        assert "readiness_trend_percent" not in model.model_fields
        assert "evidence_coverage_percent" in model.model_fields
        assert "evidence_coverage_trend_percent" in model.model_fields

    for field in ("readiness_percent", "readiness_is_estimate", "readiness_reasons"):
        assert field not in CareerStrategyRead.model_fields
    assert "evidence_coverage_percent" in CareerStrategyRead.model_fields
    assert "readiness_percent" not in TargetStrategyRead.model_fields
    assert "evidence_coverage_percent" in TargetStrategyRead.model_fields


def test_every_renamed_percentage_is_optional():
    """Each of these can be genuinely unmeasurable, so none of them may be a bare int -- a
    non-optional field forces the service to invent a value for the no-data case, which is
    exactly how the 0s got there in the first place."""
    from app.schemas.career_decision import CareerDecisionRoleRead
    from app.schemas.career_path import CareerPathSummaryRead
    from app.schemas.progress import ProgressDashboardRead

    for model, field in (
        (CareerPathSummaryRead, "preparation_percent"),
        (CareerDecisionRoleRead, "evidence_coverage_percent"),
        (CareerDecisionRoleRead, "classification"),
        (CareerDecisionRoleRead, "estimated_hours_to_strengthen"),
        (ProgressDashboardRead, "performance_score"),
    ):
        annotation = str(model.model_fields[field].annotation)
        assert "Optional" in annotation or "None" in annotation, (
            f"{model.__name__}.{field} is not optional, so the service must invent a no-data value"
        )


# ==========================================================================================
# The same rule, applied to prose (F12-10)
# ==========================================================================================


def _prose_of(relative_path: str):
    """Every comment and docstring in one module, as (line number, text)."""
    import ast
    import io
    import pathlib
    import tokenize

    path = pathlib.Path(__file__).resolve().parents[2] / relative_path
    source = path.read_text(encoding="utf-8")
    out = []
    # Contiguous # lines are joined before being judged. A sentence that wraps across two lines
    # is one claim, and half of it read alone ("None when readiness found nothing") loses the
    # very qualifier that makes it correct ("job_hunt/readiness's own ... object" above it).
    block_start, block = None, []
    previous_line = None
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            if previous_line is not None and token.start[0] == previous_line + 1:
                block.append(token.string)
            else:
                if block:
                    out.append((block_start, " ".join(block)))
                block_start, block = token.start[0], [token.string]
            previous_line = token.start[0]
    if block:
        out.append((block_start, " ".join(block)))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                for offset, line in enumerate(doc.splitlines()):
                    out.append((getattr(node, "lineno", 1) + offset, line))
    return out


# The modules whose percentage is evidence coverage. Listed explicitly rather than discovered,
# because the rule below is only true of these: everywhere else in the product "readiness" may
# be exactly the right word.
_EVIDENCE_COVERAGE_MODULES = (
    "app/services/career_twin_service.py",
    "app/services/career_decision_service.py",
    "app/services/career_strategy/service.py",
    "app/services/career_strategy/targets.py",
    "app/schemas/career_twin.py",
    "app/schemas/career_decision.py",
    "app/schemas/career_strategy.py",
    "app/api/career_twin.py",
    "app/models/readiness_snapshot.py",
    "tests/unit/test_career_twin_service.py",
    "tests/unit/test_career_decision_service.py",
    "tests/unit/test_career_strategy_evidence_cache.py",
    "tests/unit/test_evidence_source_availability.py",
    # These four are not evidence-coverage modules, but their prose describes Career Twin's
    # number, which is why F12-10 reached them. Included so every line this phase corrected is
    # covered by the rule, not just the ones in the modules that own the metric.
    "app/services/interview_service.py",
    "app/services/progress_service.py",
    "app/services/account_service.py",
    "tests/unit/test_career_strategy_service.py",
)

# What the word is still allowed to mean inside those modules. Each entry is a real, current
# thing -- a type, a route, a stored value, a different product concept, or a sentence whose
# whole job is to say the two are not the same. This list is the difference between a semantic
# guard and a banned word: none of these are stale, and none of them may be "cleaned up".
_LEGITIMATE = (
    # names of things that keep the word because renaming them would break a contract
    "readinesssnapshot", "readiness_snapshots", "uq_readiness_snapshot",
    "careertwinreadinessread", "jobreadinessread", "readinessbiggestgapread",
    "/career-twin/readiness", "readiness_milestone", "check_readiness_milestone",
    # genuinely different measurements that are correctly called readiness
    "job_hunt/readiness", "job readiness", "application readiness", "apply readiness",
    "application-readiness", "readiness.compute", "readiness.py", "career path readiness",
    # sentences whose purpose is to distinguish the two
    "not readiness", "renamed from readiness", "readiness in this product",
    "off `readiness`", "is not\nreadiness", "readiness and never was",
    "one word must not name", "readiness_percent", "not called readiness",
    # a line that names the five-signal average is talking about the other metric, by definition
    "five-signal",
    # two more senses of the word, neither of them a metric: whether the *project* is ready to
    # ship, and the dimensions job_hunt weighs when judging one advert
    "production-readiness", "readiness dimension",
)


def test_no_comment_in_the_evidence_coverage_modules_calls_that_number_readiness():
    """F12-10, as a rule rather than a fix: the prose next to the code must mean what the code
    means.

    Phase 2G renamed the field and left sentences behind describing it as readiness -- a test
    comment asserting evidence coverage while calling it "a 70% readiness", a service docstring
    promising "readiness by career_twin_service". Renaming a field does not reach prose, and
    nothing was checking the prose.

    This is deliberately not a ban on the word. Four different measurements in this product are
    correctly called readiness, and three of them appear in these very files -- as type names, a
    route, and job_hunt's own apply-readiness. What is forbidden is naming *this* number, the
    weighted mean of evidence confidence, with the word that belongs to a five-signal average.
    """
    offenders = []
    for module in _EVIDENCE_COVERAGE_MODULES:
        for line_number, text in _prose_of(module):
            lowered = text.lower()
            if "readiness" not in lowered and "job-ready" not in lowered:
                continue
            if any(allowed in lowered for allowed in _LEGITIMATE):
                continue
            offenders.append(f"{module}:{line_number}: {text.strip()}")

    assert not offenders, (
        "These lines describe evidence coverage as readiness. Either the wording is stale, or "
        "the line names something that legitimately keeps the word and belongs in _LEGITIMATE:\n  "
        + "\n  ".join(offenders)
    )


def test_the_guard_would_actually_catch_the_defect_it_was_written_for():
    """A guard that cannot fail protects nothing. This is the exact sentence F12-10 was raised
    for, checked against the same predicate the test above applies."""
    stale = "# state (confidence 0.7) -- at weight 1.0 that's exactly a 70% readiness, crossing the"
    lowered = stale.lower()

    assert "readiness" in lowered
    assert not any(allowed in lowered for allowed in _LEGITIMATE), (
        "the allowlist has grown broad enough to permit the original defect"
    )


def test_the_guard_still_permits_the_words_that_are_correct():
    """The other half: these are real lines from the same modules, and every one of them must
    survive. A guard that flagged these would push someone into making the code *less* accurate
    to satisfy a test."""
    legitimate_lines = [
        "# The table name stays `readiness_snapshots`: it identifies a table, not a measurement",
        "the GET /career-twin/readiness endpoint whose payload it is, not the metric it carries",
        "# EVIDENCE COVERAGE, not readiness. Renamed from readiness_percent in Phase 2G",
        '# job_hunt/readiness\'s own "most improvable gap", with the concrete before/after',
        "# The `kind` string stays \"readiness_milestone\": it is a persisted value already",
    ]
    for line in legitimate_lines:
        assert any(allowed in line.lower() for allowed in _LEGITIMATE), (
            f"the guard would wrongly flag a correct line: {line}"
        )
