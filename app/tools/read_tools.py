"""VA-4's read-only capabilities, over the existing domain services.

Every tool here was derived by reading the authenticated route that already serves the same
data, and each calls the same service that route calls. Nothing composes SQL except one
scoped query, explained where it appears.

Two rules shape all of it:

**Ownership is structural, not checked.** No tool takes an owner identifier, so there is no
comparison to get wrong. The profile comes from `context.require_profile()`, which comes from
the authenticated user. The only identifiers a tool accepts are *resource* ids, and each is
passed to a service that already filters by profile -- so a resource belonging to someone else
comes back empty rather than being filtered afterwards by this layer.

**Outputs are built field by field.** The API schemas are richer than a spoken assistant
needs: `StudentProfileRead` carries an email address and a mobile number, `ResumeSummaryRead` a
filename, `SavedJobRead` a nested listing with a URL. Reusing them would expose every field a
future UI feature adds. The cost of listing fields by hand is the point.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from pydantic import Field

from app.tools.contracts import (
    Idempotency,
    ToolContext,
    ToolInput,
    ToolKind,
    ToolOutput,
    ToolSpec,
    Verification,
)

#: Default ceiling for list results. Chosen for a *spoken* answer: a voice agent reading
#: twenty saved jobs aloud is already useless, so the limit is about the consumer rather than
#: the database.
DEFAULT_MAX_ITEMS = 20


class NoArguments(ToolInput):
    """For a tool whose only input is who is asking.

    Still a model rather than `None`: `extra="forbid"` is what rejects an invented
    `profile_id`, and a tool with no schema would have nothing to reject it with.
    """


def _iso(value: Optional[datetime.datetime | datetime.date]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _clip(text: Optional[str], limit: int) -> Optional[str]:
    """Bound a free-text field.

    These strings are AI-generated or user-supplied and have no inherent length. Bounding them
    here keeps a result's size predictable without the registry's size ceiling having to be
    the thing that catches it.
    """
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _bounded(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    return items[:limit], len(items) > limit


# --------------------------------------------------------------------------- profile


class ProfileSummary(ToolOutput):
    full_name: Optional[str] = None
    college: Optional[str] = None
    degree: Optional[str] = None
    branch: Optional[str] = None
    graduation_year: Optional[int] = None
    experience_level: Optional[str] = None
    target_role: Optional[str] = None
    onboarding_completed: bool = False


def _profile_summary(context: ToolContext, _args: NoArguments) -> ProfileSummary:
    profile = context.require_profile()
    role_name: Optional[str] = None
    if profile.custom_target_role:
        role_name = profile.custom_target_role
    elif profile.target_role_id is not None:
        from app.models.role import Role

        role = context.db.get(Role, profile.target_role_id)
        role_name = role.name if role else None
    return ProfileSummary(
        full_name=profile.full_name,
        college=profile.college,
        degree=profile.degree,
        branch=profile.branch,
        graduation_year=profile.graduation_year,
        experience_level=profile.experience_level,
        target_role=role_name,
        onboarding_completed=bool(profile.onboarding_completed),
        # Deliberately absent: email, mobile_number. The agent never needs to speak either,
        # and a contact detail read aloud in a shared room is a disclosure the feature does
        # not require.
    )


# --------------------------------------------------------------------------- skills / evidence


class SkillItem(ToolOutput):
    skill: str
    level: Optional[str] = None
    confidence_percent: Optional[int] = None
    confidence_band: Optional[str] = None
    state: Optional[str] = None
    last_verified_at: Optional[str] = None


class SkillPassport(ToolOutput):
    skills: list[SkillItem] = Field(default_factory=list)
    truncated: bool = False


def _skill_passport(context: ToolContext, _args: NoArguments) -> SkillPassport:
    from app.services import skill_passport_service

    profile = context.require_profile()
    rows = skill_passport_service.list_skills(context.db, profile)
    kept, truncated = _bounded(list(rows), DEFAULT_MAX_ITEMS)
    return SkillPassport(
        skills=[
            SkillItem(
                skill=row.skill,
                level=row.level,
                confidence_percent=row.confidence_percent,
                confidence_band=row.confidence_band,
                state=row.state,
                last_verified_at=_iso(row.last_verified_at),
            )
            # `evidence` and `next_action_hint` are dropped: the evidence breakdown is a
            # per-skill nested structure meant for a screen, and including it would make one
            # call's output larger than everything else in the registry combined.
            for row in kept
        ],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- resume / ATS


class ResumeSummary(ToolOutput):
    has_resume: bool
    parse_status: Optional[str] = None
    uploaded_at: Optional[str] = None


def _resume_summary(context: ToolContext, _args: NoArguments) -> ResumeSummary:
    from app.services.resume_service import get_latest_resume

    latest = get_latest_resume(context.db, context.require_profile().id)
    if latest is None:
        # A distinct, truthful "nothing yet" rather than an error: having no resume is a
        # normal state the agent should be able to talk about.
        return ResumeSummary(has_resume=False)
    # `filename` and `file_size_bytes` are dropped: a filename is user-chosen and routinely
    # carries a real name, and neither tells the agent anything it can act on.
    return ResumeSummary(has_resume=True, parse_status=latest.parse_status, uploaded_at=_iso(latest.created_at))


class AtsCheckItem(ToolOutput):
    id: int
    overall_score: Optional[int] = None
    score_category: Optional[str] = None
    checked_at: Optional[str] = None


class AtsCheckList(ToolOutput):
    checks: list[AtsCheckItem] = Field(default_factory=list)
    truncated: bool = False


def _ats_checks(context: ToolContext, _args: NoArguments) -> AtsCheckList:
    from app.services.ats_check_service import list_ats_checks

    rows = list_ats_checks(context.db, context.require_profile().id)
    kept, truncated = _bounded(list(rows), 10)
    return AtsCheckList(
        checks=[
            AtsCheckItem(
                id=row.id,
                overall_score=row.overall_score,
                score_category=row.score_category,
                checked_at=_iso(row.created_at),
            )
            for row in kept
        ],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- career goals


class CareerGoalItem(ToolOutput):
    id: int
    title: str
    company: Optional[str] = None
    status: Optional[str] = None
    is_analyzed: bool = False
    application_status: Optional[str] = None


class CareerGoalList(ToolOutput):
    goals: list[CareerGoalItem] = Field(default_factory=list)
    truncated: bool = False


def _career_goals(context: ToolContext, _args: NoArguments) -> CareerGoalList:
    from app.services import career_goal_service

    rows = career_goal_service.list_goals(context.db, context.require_profile().id)
    kept, truncated = _bounded(list(rows), DEFAULT_MAX_ITEMS)
    return CareerGoalList(
        goals=[
            CareerGoalItem(
                id=row.id,
                title=row.title,
                company=row.company,
                status=row.status,
                is_analyzed=bool(row.is_analyzed),
                application_status=row.application_status,
            )
            for row in kept
        ],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- career twin


class CareerTwinReadiness(ToolOutput):
    has_role: bool
    display_role_name: Optional[str] = None
    evidence_coverage_percent: Optional[int] = None
    is_estimate: bool = False
    biggest_gap_skill: Optional[str] = None
    next_action_hint: Optional[str] = None
    applications_total: Optional[int] = None
    applications_at_interview_stage: Optional[int] = None


def _career_twin(context: ToolContext, _args: NoArguments) -> CareerTwinReadiness:
    from app.services import career_twin_service

    profile = context.require_profile()
    twin = career_twin_service.get_current(context.db, profile)
    # `reasons` is the field the HTTP route gates behind Premium (see
    # app/api/career_twin.py::_gate_reasons). It is not in this output at all -- a free
    # account must not reach it through a second door, and the agent can give the headline
    # number and the gap without the full evidence breakdown.
    return CareerTwinReadiness(
        has_role=bool(twin.has_role),
        display_role_name=twin.display_role_name,
        evidence_coverage_percent=twin.evidence_coverage_percent,
        is_estimate=bool(twin.is_estimate),
        biggest_gap_skill=twin.biggest_gap_skill,
        next_action_hint=_clip(twin.next_action_hint, 240),
        applications_total=twin.applications_total,
        applications_at_interview_stage=twin.applications_at_interview_stage,
    )


# --------------------------------------------------------------------------- career path


class CareerPathSummary(ToolOutput):
    has_path: bool
    id: Optional[int] = None
    display_role_name: Optional[str] = None
    is_primary: bool = False
    generated_via: Optional[str] = None
    phase_count: int = 0
    skill_gap_count: int = 0


def _career_path(context: ToolContext, _args: NoArguments) -> CareerPathSummary:
    from app.services.career_path import service as career_path_service

    path = career_path_service.get_current(context.db, context.require_profile().id)
    if path is None:
        return CareerPathSummary(has_path=False)
    # Counts rather than contents: `phases` and `skill_gaps` are the large nested structures
    # the path screen renders. A tool returning them would blow the size ceiling, and an agent
    # that needs one phase should ask for that phase through a tool that does not exist yet.
    return CareerPathSummary(
        has_path=True,
        id=path.id,
        display_role_name=path.display_role_name,
        is_primary=bool(path.is_primary),
        generated_via=path.generated_via,
        phase_count=len(path.phases or []),
        skill_gap_count=len(path.skill_gaps or []),
    )


# --------------------------------------------------------------------------- saved jobs


class SavedJobItem(ToolOutput):
    id: int
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    status: Optional[str] = None
    applied_at: Optional[str] = None


class SavedJobList(ToolOutput):
    jobs: list[SavedJobItem] = Field(default_factory=list)
    truncated: bool = False


class SavedJobQuery(ToolInput):
    status: Optional[str] = Field(default=None, max_length=30)


def _saved_jobs(context: ToolContext, args: SavedJobQuery) -> SavedJobList:
    from app.services.job_hunt import service as job_hunt_service

    rows = job_hunt_service.list_saved_jobs(
        context.db,
        context.require_profile().id,
        statuses=[args.status] if args.status else None,
    )
    kept, truncated = _bounded(list(rows), DEFAULT_MAX_ITEMS)
    return SavedJobList(
        jobs=[
            SavedJobItem(
                id=saved.id,
                title=listing.title,
                company=listing.company,
                location=listing.location,
                status=saved.status,
                applied_at=_iso(saved.applied_at),
            )
            # `url` and `notes` are dropped: the URL is useless spoken aloud, and notes are
            # the student's own free text, which the agent has no task here that needs.
            for saved, listing in kept
        ],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- interviews


class InterviewItem(ToolOutput):
    id: int
    type: Optional[str] = None
    mode: Optional[str] = None
    status: Optional[str] = None
    overall_score: Optional[float] = None
    created_at: Optional[str] = None


class InterviewHistory(ToolOutput):
    interviews: list[InterviewItem] = Field(default_factory=list)
    truncated: bool = False


def _interview_history(context: ToolContext, _args: NoArguments) -> InterviewHistory:
    from app.services import interview_service

    rows = interview_service.list_interviews(context.db, context.require_profile().id)
    kept, truncated = _bounded(list(rows), DEFAULT_MAX_ITEMS)
    return InterviewHistory(
        interviews=[
            InterviewItem(
                id=row.id,
                type=getattr(row, "type", None),
                mode=getattr(row, "mode", None),
                status=row.status,
                overall_score=row.overall_score,
                created_at=_iso(row.created_at),
            )
            for row in kept
        ],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- weaknesses


class WeaknessItem(ToolOutput):
    skill_key: str
    status: Optional[str] = None
    occurrences: Optional[int] = None
    last_score: Optional[float] = None
    last_seen_at: Optional[str] = None


class WeaknessList(ToolOutput):
    weaknesses: list[WeaknessItem] = Field(default_factory=list)
    truncated: bool = False


def _weaknesses(context: ToolContext, _args: NoArguments) -> WeaknessList:
    from app.services import weakness_record_service

    rows = weakness_record_service.list_for_profile(context.db, context.require_profile().id)
    kept, truncated = _bounded(list(rows), DEFAULT_MAX_ITEMS)
    return WeaknessList(
        weaknesses=[
            WeaknessItem(
                skill_key=row.skill_key,
                status=row.status,
                occurrences=row.occurrences,
                last_score=row.last_score,
                last_seen_at=_iso(row.last_seen_at),
            )
            for row in kept
        ],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- preparation plan


class PreparationPlanQuery(ToolInput):
    #: A job-description id the student already owns. `gt=0` because an id of 0 or a negative
    #: one cannot exist, and refusing it here is cheaper than a database round trip --
    #: ownership itself is enforced by the service, which filters on profile_id.
    job_description_id: int = Field(gt=0)


class PreparationPlan(ToolOutput):
    found: bool
    job_title: Optional[str] = None
    company_name: Optional[str] = None
    gap_count: int = 0
    top_gaps: list[str] = Field(default_factory=list)
    truncated: bool = False


def _preparation_plan(context: ToolContext, args: PreparationPlanQuery) -> PreparationPlan:
    from app.services.jd_service import get_preparation_plan

    plan = get_preparation_plan(context.db, args.job_description_id, context.require_profile().id)
    if plan is None:
        # The same answer for "no such plan" and "someone else's plan". The service filters by
        # profile, so this branch covers both -- and that is the right behaviour: a different
        # response for the two would let a caller enumerate which ids exist.
        return PreparationPlan(found=False)
    gaps = [str(gap) for gap in (plan.preparation_gaps or [])]
    kept, truncated = _bounded(gaps, 5)
    return PreparationPlan(
        found=True,
        job_title=plan.job_title,
        company_name=plan.company_name,
        gap_count=len(gaps),
        top_gaps=[_clip(gap, 160) or "" for gap in kept],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- daily mission


class TodayMission(ToolOutput):
    has_mission: bool
    id: Optional[int] = None
    title: Optional[str] = None
    action_type: Optional[str] = None
    status: Optional[str] = None
    estimated_minutes: Optional[int] = None
    skill: Optional[str] = None


def _today_mission(context: ToolContext, _args: NoArguments) -> TodayMission:
    """Today's mission if one exists -- without creating it.

    The only tool here that queries the ORM directly, and the reason is the point of the
    read-only phase. `mission_service.get_or_create_today` is the function the HTTP route
    calls, and it **inserts and commits** a Mission when today has none. A READ tool that did
    that would be a write tool with a misleading label, and
    `test_tool_registry_security.py` asserts no READ tool mutates anything.

    So this is a scoped read: filtered by the authenticated profile and today's date, through
    the ORM, with no raw SQL and no path by which a caller could influence the filter.
    """
    from app.models.mission import Mission

    profile = context.require_profile()
    mission = (
        context.db.query(Mission)
        .filter(Mission.profile_id == profile.id, Mission.mission_date == datetime.date.today())
        .first()
    )
    if mission is None:
        return TodayMission(has_mission=False)
    return TodayMission(
        has_mission=True,
        id=mission.id,
        title=_clip(mission.title, 200),
        action_type=mission.action_type,
        status=mission.status,
        estimated_minutes=mission.estimated_minutes,
        skill=mission.skill,
    )


# --------------------------------------------------------------------------- progress


class ProgressSummary(ToolOutput):
    performance_score: Optional[float] = None
    completed_assessments: Optional[int] = None
    completed_interviews: Optional[int] = None
    roadmap_tasks_done: Optional[int] = None
    roadmap_tasks_total: Optional[int] = None
    top_weaknesses: list[str] = Field(default_factory=list)


def _progress(context: ToolContext, _args: NoArguments) -> ProgressSummary:
    from app.services import progress_service

    dashboard = progress_service.get_dashboard(context.db, context.require_profile().id)
    weaknesses = [str(item) for item in (dashboard.top_weaknesses or [])][:5]
    return ProgressSummary(
        performance_score=dashboard.performance_score,
        completed_assessments=dashboard.completed_assessments,
        completed_interviews=dashboard.completed_interviews,
        roadmap_tasks_done=dashboard.roadmap_tasks_done,
        roadmap_tasks_total=dashboard.roadmap_tasks_total,
        top_weaknesses=[_clip(item, 120) or "" for item in weaknesses],
    )


# --------------------------------------------------------------------------- the catalogue


def read_only_specs() -> list[ToolSpec]:
    """VA-4's inventory.

    Names are `domain.verb_noun` and are **stable**: an orchestrator's prompt, a transcript
    and a log line will all refer to them, so renaming one later is a breaking change. None is
    premium-gated, because the gated *fields* were left out of the outputs instead -- gating a
    whole capability would make the agent unable to answer a question the app answers for
    free, which is a worse outcome than a slightly thinner answer.
    """
    read = dict(kind=ToolKind.READ, idempotency=Idempotency.NATURAL, verification=Verification.NONE)
    return [
        ToolSpec(
            name="profile.get_summary",
            summary="The student's own profile basics: name, college, degree, graduation year, target role.",
            input_model=NoArguments,
            output_model=ProfileSummary,
            handler=_profile_summary,
            max_items=1,
            **read,
        ),
        ToolSpec(
            name="skills.get_passport",
            summary="Evidence-derived skill levels and confidence for the student's current role.",
            input_model=NoArguments,
            output_model=SkillPassport,
            handler=_skill_passport,
            max_items=DEFAULT_MAX_ITEMS,
            **read,
        ),
        ToolSpec(
            name="resume.get_latest_summary",
            summary="Whether a resume has been uploaded, and whether it parsed.",
            input_model=NoArguments,
            output_model=ResumeSummary,
            handler=_resume_summary,
            max_items=1,
            **read,
        ),
        ToolSpec(
            name="resume.list_ats_checks",
            summary="Recent ATS check scores for the student's resumes.",
            input_model=NoArguments,
            output_model=AtsCheckList,
            handler=_ats_checks,
            max_items=10,
            **read,
        ),
        ToolSpec(
            name="career_goals.list",
            summary="The student's career goals with analysis and application status.",
            input_model=NoArguments,
            output_model=CareerGoalList,
            handler=_career_goals,
            max_items=DEFAULT_MAX_ITEMS,
            **read,
        ),
        ToolSpec(
            name="career_twin.get_readiness",
            summary="Readiness headline: evidence coverage, biggest gap, application counts.",
            input_model=NoArguments,
            output_model=CareerTwinReadiness,
            handler=_career_twin,
            max_items=1,
            **read,
        ),
        ToolSpec(
            name="career_path.get_current",
            summary="The student's current career path: role, how it was generated, phase and gap counts.",
            input_model=NoArguments,
            output_model=CareerPathSummary,
            handler=_career_path,
            max_items=1,
            **read,
        ),
        ToolSpec(
            name="jobs.list_saved",
            summary="Jobs the student has saved or applied to, optionally filtered by status.",
            input_model=SavedJobQuery,
            output_model=SavedJobList,
            handler=_saved_jobs,
            max_items=DEFAULT_MAX_ITEMS,
            **read,
        ),
        ToolSpec(
            name="interviews.list_history",
            summary="Past mock interviews with type, status and overall score.",
            input_model=NoArguments,
            output_model=InterviewHistory,
            handler=_interview_history,
            max_items=DEFAULT_MAX_ITEMS,
            **read,
        ),
        ToolSpec(
            name="weaknesses.list",
            summary="Skills the student has been repeatedly flagged weak on, with occurrence counts.",
            input_model=NoArguments,
            output_model=WeaknessList,
            handler=_weaknesses,
            max_items=DEFAULT_MAX_ITEMS,
            **read,
        ),
        ToolSpec(
            name="jobs.get_preparation_plan",
            summary="The preparation plan for one of the student's own job descriptions.",
            input_model=PreparationPlanQuery,
            output_model=PreparationPlan,
            handler=_preparation_plan,
            max_items=5,
            **read,
        ),
        ToolSpec(
            name="missions.get_today",
            summary="Today's daily mission, if one has been generated.",
            input_model=NoArguments,
            output_model=TodayMission,
            handler=_today_mission,
            max_items=1,
            **read,
        ),
        ToolSpec(
            name="progress.get_summary",
            summary="Performance score, completed assessments and interviews, roadmap progress.",
            input_model=NoArguments,
            output_model=ProgressSummary,
            handler=_progress,
            max_items=1,
            **read,
        ),
    ]
