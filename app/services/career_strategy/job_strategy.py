"""Job-specific strategy: apply now, prepare first, or say we cannot tell.

This is the one place the career-level strategy meets a single advert, and it is composed
entirely from services that already exist. The match score is job_hunt/matching's. The
per-skill buckets are job_hunt/provenance's. The five readiness dimensions are
job_hunt/readiness's. Nothing here re-scores a listing, and there is deliberately no second
match engine -- if this file disagreed with the Job Hunt screen about the same job, one of them
would be wrong and the student would have no way to tell which.

What this adds is the decision, and the decision is the hard part to get honest.

**"Insufficient information" is a real answer, and it is the default.** An advert that lists no
skills, or a student with nothing recorded, produces a readiness score of None -- and a None is
not a low score. The temptation is to fill the screen anyway, because a screen that says
"prepare first" always looks more useful than one that admits it cannot tell. It is also a lie
about the student, and the student is the one who has to act on it. So a decision is only
returned when enough dimensions could actually be judged; otherwise the answer names what is
missing and what would fix it.

**"Not ready" never means "do not apply".** Adverts routinely overstate requirements and the
decision to apply is the student's. The wording throughout is about preparation, and the
blockers are the specific, named things that would make an application stronger -- never a
verdict on whether they deserve the job.

There is no hiring probability here, and no field one could be written into.

Deterministic: same rows in, same decision out. No AI, no network.
"""

import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.saved_job import SavedJob
from app.services.career_strategy.bottleneck import StrategyDecision
from app.services.job_hunt import matching, provenance, readiness, service

JOB_DECISIONS: Tuple[str, ...] = (
    "APPLY_NOW",
    "FIX_BEFORE_APPLYING",
    "NOT_A_CURRENT_PRIORITY",
    "INSUFFICIENT_INFORMATION",
)

# Readiness is a weighted mean over the dimensions that could be judged. Below this many
# judged dimensions the mean is arithmetic dressed as a conclusion: two of five dimensions can
# put a student in any band at all depending on which two happened to be knowable.
MIN_JUDGED_DIMENSIONS = 3

# The readiness bands (job_hunt/readiness._band) that support applying today. Reused rather
# than re-thresholded so this screen and the Job Hunt readiness screen can never disagree about
# the same number.
_APPLY_BANDS = frozenset({"strong_candidate", "worth_applying"})

# A match component the engine scored exactly 0.0. This is a *measurement*, not an absence:
# job_hunt/matching omits a component it could not judge (those go to `unknown_factors`), so a
# zero that reached component_scores means the engine compared two known facts and found them
# incompatible -- "you want remote, this is not remote", "this is in a city you did not list
# and you said you would not relocate". Nothing here re-derives that; the threshold only reads
# what matching already decided.
_HARD_MISMATCH = 0.0

# The two components that describe the *terms of the job* rather than the student's capability.
# They matter to the decision for a reason no skill blocker shares: no amount of preparation
# changes them. A student cannot study their way into a different city.
# component -> (blocker subject, blocker title, headline clause)
_CONDITION_COMPONENTS: Dict[str, Tuple[str, str, str]] = {
    "location_fit": (
        "location",
        "Where this job is",
        "this one is not where you said you want to work",
    ),
    "employment_type_fit": (
        "employment_type",
        "The kind of contract this is",
        "this is not the kind of contract you said you want",
    ),
}


class JobConditions(BaseModel):
    """The advert's own terms, carried through from the listing row unchanged.

    Salary is kept as four separate fields rather than one pre-formatted string precisely so
    the period cannot be lost in transit. ₹500/hour and ₹500/year are different offers by a
    factor of about two thousand, and the only safe way to render one is to render its period
    beside it -- so `salary_period` travels with the amount, and stays None when the provider
    did not say. Nothing here converts between periods: an hourly figure is never annualised,
    because doing so would require assuming hours per week that no provider supplies.
    """

    model_config = ConfigDict(frozen=True)

    location: Optional[str] = None
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    # None means the provider did not state a period. The client shows the figure as
    # undisclosed rather than guessing, and never defaults to "year".
    salary_period: Optional[str] = None
    # Some providers (Adzuna) estimate a salary the employer never published. Shown as an
    # estimate or not at all -- presenting a predicted figure as the employer's own would be
    # fabricated data about a real company.
    salary_is_predicted: bool = False


class JobApplicationState(BaseModel):
    """What this student has already done about this advert.

    `applied_at` is the authority on whether they applied, never `status`. A rejected
    application is still an application -- the student did the thing -- and reading a status
    string to decide otherwise would erase it. `status` is carried through verbatim and
    deliberately uninterpreted: the schema does not document what values like "closed" mean, and
    inventing a meaning for them here would be exactly the fabrication this layer exists to
    prevent.
    """

    model_config = ConfigDict(frozen=True)

    is_saved: bool = False
    applied_at: Optional[datetime.datetime] = None
    # Uninterpreted. See above.
    status: Optional[str] = None


class JobBlocker(BaseModel):
    """One specific, named thing standing between this student and a strong application."""

    model_config = ConfigDict(frozen=True)

    kind: str  # condition | skill | dimension
    subject: str
    title: str
    detail: str
    # Where in the app this gets fixed, so the card can carry a working button rather than
    # advice with nowhere to go.
    destination: str


class JobStrategy(BaseModel):
    model_config = ConfigDict(frozen=True)

    listing_id: int
    job_title: str
    company: Optional[str] = None

    decision: str
    headline: str
    reasons: List[str] = []

    # Straight from the existing match engine, with its version, so a stored score is never
    # shown as though today's weights produced it.
    match_score: Optional[float] = None
    score_version: Optional[str] = None
    # The per-factor breakdown behind match_score, and the match engine's own one-sentence
    # explanation of it. Both read from the stored JobListingMatch row rather than recomputed:
    # this screen and the Job Hunt card are then physically incapable of telling the student
    # two different stories about the same advert.
    component_scores: Dict[str, float] = {}
    match_rationale: Optional[str] = None
    # What the provider gave the match engine nothing to judge on. Carried through verbatim so
    # a mid-range score reads as "we could not tell" rather than "you scored badly".
    unknown_factors: List[str] = []

    # The advert's terms. Present even when the decision is INSUFFICIENT_INFORMATION -- not
    # knowing whether a student is ready has no bearing on what the job pays or where it is.
    conditions: JobConditions = JobConditions()

    # Which match components actually decided this, in plain words. Built from the stored
    # component scores rather than from a fresh comparison, so a factor named here is one the
    # match engine genuinely weighed. This is what makes a component explainable and not merely
    # numeric: a weak location fit should be readable as a sentence, not inferred from 0.0.
    deciding_factors: List[str] = []

    # What the student has already done about this advert.
    application: JobApplicationState = JobApplicationState()

    # Straight from job_hunt/readiness. None when too little could be judged -- never a zero.
    readiness_percent: Optional[int] = None
    readiness_band: Optional[str] = None
    judged_dimensions: int = 0
    unknown_dimensions: List[str] = []

    verified_skill_count: int = 0
    missing_skills: List[str] = []
    unproven_skills: List[str] = []
    # Skills we could not judge at all because no skill list exists for this student. Kept
    # apart from missing_skills on purpose: "we don't know" is not "you don't have it", and
    # collapsing the two is what would let an unknown read as a qualification.
    unclear_skills: List[str] = []

    blockers: List[JobBlocker] = []
    # What the student would have to do first, drawn from their career-level queue where that
    # queue already covers a skill this advert asks for. Composition, not a second planner.
    prep_from_career_strategy: List[str] = []
    # job_hunt/readiness's own "most improvable gap", with the concrete before/after it already
    # computed. Passed through untouched -- this file does not get to decide what the biggest
    # improvement is when readiness.compute has already answered that from the same dimensions
    # that produced the score.
    biggest_gap: Optional[Dict[str, Any]] = None


def _destination_for_dimension(key: str) -> str:
    return {
        "skills": "roadmap",
        "experience": "job_hunt",
        "resume": "resume_ats",
        "education": "profile",
        "interview": "interview",
    }.get(key, "career_strategy")


def _condition_blockers(
    listing: JobListing,
    component_scores: Dict[str, float],
) -> List[JobBlocker]:
    """Terms of the job the student's own stated preferences rule out.

    These come first in the blocker list, ahead of every skill, and the ordering is the whole
    point. If a student said they will not relocate and this job is in another city, then
    "learn Kafka" is not the thing standing between them and this advert -- Kafka is simply the
    most *studyable* thing we found. Leading with it would send someone to a Kafka course for a
    job they had already ruled out on the first line of their preferences.

    Only an exact 0.0 qualifies. job_hunt/matching scores a different city as 0.5 when the
    student said they are open to relocating, and leaves the component out entirely when it
    could not tell -- so the cases that reach here are the ones where two known facts genuinely
    conflict. Nothing is inferred, and a missing component is never read as a mismatch.

    Never phrased as "do not apply". The constraint is reported because it is decisive
    information the student should weigh; whether to apply anyway remains theirs.
    """
    blockers: List[JobBlocker] = []
    for component, (subject, title, _clause) in _CONDITION_COMPONENTS.items():
        score = component_scores.get(component)
        if score is None or score > _HARD_MISMATCH:
            continue
        if component == "location_fit":
            where = listing.location or "the stated location"
            detail = (
                f"This role is in {where}, which does not match the location preferences on your "
                "profile. Preparation will not change that, so weigh it before spending time on "
                "the rest of this listing."
            )
        else:
            kind = listing.employment_type or "the stated contract type"
            detail = (
                f"This is {kind}, which is not what your profile says you are looking for. "
                "Worth deciding on before preparing for it."
            )
        blockers.append(
            JobBlocker(kind="condition", subject=subject, title=title, detail=detail, destination="job_hunt")
        )
    return blockers


def _blockers(
    result: readiness.ReadinessResult,
    items: List[provenance.SkillProvenance],
    conditions: List[JobBlocker],
) -> List[JobBlocker]:
    """Conditions first, then named skills, then judged dimensions that came back weak.

    A missing skill is actionable this afternoon; "your experience is short" is not, which is
    why the ordering matters more than the scores do. Conditions outrank both because they are
    not actionable at all -- see _condition_blockers. Dimensions that could not be judged are
    never blockers: not knowing something is not evidence against the student.
    """
    blockers: List[JobBlocker] = list(conditions)

    for item in items:
        if item.status == provenance.MISSING:
            blockers.append(
                JobBlocker(
                    kind="skill",
                    subject=item.skill,
                    title=f"{item.skill} is not on your profile",
                    detail=f"This listing asks for {item.skill} and nothing on your profile mentions it yet.",
                    destination="roadmap",
                )
            )

    for item in items:
        if item.status == provenance.ON_RESUME:
            blockers.append(
                JobBlocker(
                    kind="skill",
                    subject=item.skill,
                    title=f"{item.skill} is claimed but not proven",
                    detail=(
                        f"You have listed {item.skill}, but nothing independent backs it yet. "
                        "A recruiter has only your word for it."
                    ),
                    destination="practice",
                )
            )

    for dimension in result.dimensions:
        if dimension.score is None or dimension.score >= 0.6:
            continue
        blockers.append(
            JobBlocker(
                kind="dimension",
                subject=dimension.key,
                title=f"{dimension.label} is weak for this listing",
                detail=dimension.detail or f"{dimension.label} scored low against this listing.",
                destination=_destination_for_dimension(dimension.key),
            )
        )

    return blockers


def _prep_from_queue(decision: Optional[StrategyDecision], subjects: List[str]) -> List[str]:
    """Career-level queue items that happen to be about a skill this advert asks for.

    Filtered by subject rather than re-ranked. The career strategy already decided what order
    this student's work goes in; showing a different order on a job card would be a second
    opinion from the same system.
    """
    if decision is None:
        return []
    wanted = {subject.strip().lower() for subject in subjects if subject}
    return [item.title for item in decision.queue if item.subject.strip().lower() in wanted]


def _reasons(
    result: readiness.ReadinessResult,
    items: List[provenance.SkillProvenance],
    verified: int,
    missing: List[str],
    unclear: List[str],
) -> List[str]:
    """Built from the same numbers that drove the decision, never written afterwards to justify
    it. Each line names something the student can go and look at."""
    reasons: List[str] = []
    if verified:
        reasons.append(f"You can prove {verified} of the skills this listing asks for.")
    if missing:
        listed = ", ".join(missing[:3])
        reasons.append(f"Not on your profile yet: {listed}.")
    if unclear:
        listed = ", ".join(unclear[:3])
        reasons.append(
            f"We hold no skill list for you, so we cannot tell whether you have: {listed}."
        )
    for dimension in result.dimensions:
        if dimension.score is not None and dimension.score >= 0.8:
            reasons.append(f"{dimension.label} is strong for this listing.")
            break
    if result.unknown_dimensions:
        reasons.append(
            "We could not judge: " + ", ".join(result.unknown_dimensions) + "."
        )
    if not items:
        reasons.append("This listing does not state the skills it needs, so there is little to match against.")
    return reasons


def _is_off_critical_path(
    career_decision: Optional[StrategyDecision], missing: List[str], unproven: List[str]
) -> bool:
    """Whether fixing this advert's gaps would do nothing about what is actually blocking them.

    True only when there is a career-level bottleneck to compare against and it is about a
    different subject entirely. Absent a career decision this returns False: "not a priority" is
    a claim about the student's whole strategy, and without one there is no basis to make it.
    """
    if career_decision is None or career_decision.primary is None:
        return False
    subject = career_decision.primary.subject.strip().lower()
    here = {value.strip().lower() for value in list(missing) + list(unproven)}
    return subject not in here


def _condition_clause(component_scores: Dict[str, float]) -> Optional[str]:
    """The headline clause for a decisive term of the job, or None when there is not one.

    Deliberately checked before anything about skills. A missing skill is the most *studyable*
    thing this file can find, which is exactly why it must not be allowed to become the
    headline by default: telling a student to learn Kafka for a job in a city they have said
    they will not move to sends them to a course to solve a problem a course cannot solve.
    """
    for component, (_subject, _title, clause) in _CONDITION_COMPONENTS.items():
        score = component_scores.get(component)
        if score is not None and score <= _HARD_MISMATCH:
            return clause
    return None


# Component -> (label, strong sentence, weak sentence). Only components the match engine
# actually scored appear; one it could not judge is absent from component_scores entirely and
# is reported through `unknown_factors` instead of being guessed at here.
_FACTOR_WORDS: Dict[str, Tuple[str, str, str]] = {
    "skill_overlap": (
        "Skills",
        "your skills line up well with what this advert asks for",
        "your recorded skills cover little of what this advert asks for",
    ),
    "experience_fit": (
        "Experience",
        "the seniority of this role matches where you are",
        "this role is pitched at a different level of experience than yours",
    ),
    "location_fit": (
        "Location",
        "the location works for you",
        "this is not where you said you want to work",
    ),
    "employment_type_fit": (
        "Contract",
        "the contract type is what you are looking for",
        "this is not the kind of contract you said you want",
    ),
    "role_alignment": (
        "Role",
        "this role is aligned with your target",
        "this role sits away from the target you have chosen",
    ),
}

# The band above which a component reads as a point in the student's favour, and below which it
# reads as a point against. Deliberately the same 0.6 the blocker list already uses for
# readiness dimensions, so one advert cannot be described as strong here and weak there.
_FACTOR_STRONG = 0.6


def _deciding_factors(
    component_scores: Dict[str, float],
    listing: JobListing,
    preferred_companies: Sequence[str],
) -> List[str]:
    """The match, in sentences.

    Every line restates a number job_hunt/matching already produced. Nothing is recomputed and
    no component is invented: if the engine could not judge location, there is no location line,
    because "we could not tell" is `unknown_factors`' job and saying anything else would turn an
    absence into a verdict.

    The preferred-employer line is a fact about the student's stated intent, not a score. It
    reuses matching's own public comparison so this screen and the Job Hunt ranking agree on
    what counts as a match, and it stays out of every number on this page -- naming a company
    you like does not make you more qualified for it.
    """
    factors: List[str] = []
    for component, (label, strong, weak) in _FACTOR_WORDS.items():
        score = component_scores.get(component)
        if score is None:
            continue
        factors.append(f"{label}: {strong if score >= _FACTOR_STRONG else weak}.")
    if matching.matches_preferred_company(listing.company, preferred_companies):
        factors.append(
            f"Employer: {listing.company} is one of the companies you said you are targeting. "
            "That does not change how well you match the role, but it is worth weighing."
        )
    return factors


def _decide(
    result: readiness.ReadinessResult,
    judged: int,
    items: List[provenance.SkillProvenance],
    missing: List[str],
    unclear: List[str],
    unproven: List[str],
    career_decision: Optional[StrategyDecision],
    component_scores: Dict[str, float],
) -> Tuple[str, str]:
    """The four-way decision and its headline.

    Order matters, and both insufficient-information checks come first so a profile we cannot
    read never falls through into a confident-sounding verdict in either direction. The
    condition check sits immediately after them for the reason given in _condition_clause.
    """
    if result.overall is None or judged < MIN_JUDGED_DIMENSIONS:
        return (
            "INSUFFICIENT_INFORMATION",
            "There is not enough here to judge this application yet.",
        )

    if unclear:
        # We hold no skill list for this student, so job_hunt/provenance could not tell whether
        # they have what this advert asks for -- it returned UNCLEAR, not MISSING.
        #
        # That distinction is the whole reason this branch exists. Without it, `missing` is
        # empty for exactly the same reason it is empty for a perfectly-qualified candidate,
        # and the next check would read "we know nothing about your skills" as "you have every
        # skill" and tell them to apply. Skills carry the heaviest readiness weight and are the
        # first thing a recruiter screens on; a recommendation to apply that silently skipped
        # them is the most damaging fabrication this file could produce.
        return (
            "INSUFFICIENT_INFORMATION",
            "Add your skills and we can tell you whether this one is worth applying to.",
        )

    clause = _condition_clause(component_scores)

    if result.band in _APPLY_BANDS and not missing:
        if clause is not None:
            # Still APPLY_NOW: they are ready, and nothing about a location or a contract type
            # makes them less ready. Downgrading this to "fix something first" would invent
            # preparation work that does not exist. The constraint is stated instead, because
            # it is the only open question left.
            return (
                "APPLY_NOW",
                f"On preparation you could apply to this today — but {clause}.",
            )
        return ("APPLY_NOW", "You can make a credible application to this today.")

    if result.band in _APPLY_BANDS and missing:
        if clause is not None:
            return (
                "FIX_BEFORE_APPLYING",
                f"Worth settling one thing first: {clause}. Preparation will not change that, "
                "so decide on it before putting time into the rest.",
            )
        if len(missing) == 1:
            detail = f"{missing[0]} is the one thing this listing asks for that you cannot show yet."
        else:
            detail = (
                f"{missing[0]} is the first of {len(missing)} things this listing asks for that "
                "you cannot show yet."
            )
        return ("FIX_BEFORE_APPLYING", f"You are close. {detail}")

    if _is_off_critical_path(career_decision, missing, unproven):
        # This advert is a long way off *and* closing its gaps would not touch the thing
        # actually holding this student back. Saying so is the useful answer: the alternative
        # is a plausible-looking to-do list that quietly competes with their real bottleneck.
        # Never phrased as "don't apply" -- that remains their call, always.
        return (
            "NOT_A_CURRENT_PRIORITY",
            f"This one needs a lot, and none of it is what is currently holding you back "
            f"({career_decision.primary.title}). Worth revisiting once that is moving.",
        )

    if clause is not None:
        return (
            "FIX_BEFORE_APPLYING",
            f"There is preparation that would strengthen this application, but {clause} — "
            "worth deciding that before starting on the rest.",
        )
    return (
        "FIX_BEFORE_APPLYING",
        "Worth applying to eventually. There is preparation that would make this application much stronger.",
    )


def compose(
    db: Session,
    profile: StudentProfile,
    listing_id: int,
    *,
    career_decision: Optional[StrategyDecision] = None,
    now: Optional[datetime.datetime] = None,
) -> JobStrategy:
    """Assemble one job's strategy. Raises service.ListingNotFoundError for a listing id that
    does not exist.

    Note what is and is not scoped here, because the distinction matters to anyone reviewing
    this for IDOR. Listings are shared rows and are deliberately not profile-owned -- there is
    nothing private in a public job advert, and job_hunt/service.get_listing_for_profile says so
    explicitly. What *is* scoped, and is the only thing about this student in the response, is
    the match: that lookup is filtered by profile_id and recomputed per student. So a student
    can read any listing id, and can only ever see their own assessment of it.
    """
    listing, match = service.get_listing_for_profile(db, profile.id, listing_id)

    search_profile = service.get_or_create_search_profile(db, profile.id)
    skills = list(match.matched_skills or []) + list(match.missing_skills or [])
    evidence_states = service.evidence_states_for_skills(db, profile.id, skills)
    items = service.build_skill_provenance(search_profile, match, evidence_states)

    component_scores = {
        key: float(value) for key, value in dict(match.component_scores or {}).items() if value is not None
    }
    result = readiness.compute(db, profile, listing, component_scores, items, now)

    judged = len([dim for dim in result.dimensions if dim.score is not None])
    missing = [item.skill for item in items if item.status == provenance.MISSING]
    unproven = [item.skill for item in items if item.status == provenance.ON_RESUME]
    unclear = [item.skill for item in items if item.status == provenance.UNCLEAR]
    verified = provenance.verified_count(items)

    decision, headline = _decide(
        result, judged, items, missing, unclear, unproven, career_decision, component_scores
    )
    conditions = _condition_blockers(listing, component_scores)

    # One bounded query, not one per anything. Read before the headline is finalised because
    # telling a student to "apply today" to a job they applied to three weeks ago is not a
    # smaller mistake than getting the skills wrong -- it says plainly that the app is not
    # reading what they already did.
    saved = (
        db.query(SavedJob)
        .filter(SavedJob.profile_id == profile.id, SavedJob.listing_id == listing.id)
        .one_or_none()
    )
    application = JobApplicationState(
        is_saved=saved is not None,
        applied_at=None if saved is None else saved.applied_at,
        status=None if saved is None else saved.status,
    )
    if application.applied_at is not None:
        # The decision itself stands: it describes how well prepared they are for this role,
        # which is still true and still worth knowing. Only the call to action changes, because
        # the action has already been taken.
        headline = (
            f"You applied to this on {application.applied_at.date().isoformat()}. "
            f"{headline}"
        )

    return JobStrategy(
        listing_id=listing.id,
        job_title=listing.title,
        company=listing.company,
        decision=decision,
        headline=headline,
        reasons=_reasons(result, items, verified, missing, unclear),
        match_score=match.total_score,
        score_version=match.score_version,
        component_scores=component_scores,
        match_rationale=match.rationale or None,
        unknown_factors=list(match.unknown_factors or []),
        application=application,
        deciding_factors=_deciding_factors(
            component_scores, listing, list(search_profile.preferred_companies or [])
        ),
        conditions=JobConditions(
            location=listing.location,
            work_mode=listing.work_mode,
            employment_type=listing.employment_type,
            salary_min=listing.salary_min,
            salary_max=listing.salary_max,
            salary_currency=listing.salary_currency,
            salary_period=listing.salary_period,
            salary_is_predicted=listing.salary_is_predicted,
        ),
        # Percent is a presentation of readiness.overall, which is a 0-1 mean over judged
        # dimensions. Left as None when the decision is INSUFFICIENT_INFORMATION so no client
        # can render a number the decision itself refused to stand behind.
        readiness_percent=(
            None
            if decision == "INSUFFICIENT_INFORMATION" or result.overall is None
            else round(result.overall * 100)
        ),
        readiness_band=None if decision == "INSUFFICIENT_INFORMATION" else result.band,
        judged_dimensions=judged,
        unknown_dimensions=list(result.unknown_dimensions),
        verified_skill_count=verified,
        missing_skills=missing,
        unproven_skills=unproven,
        unclear_skills=unclear,
        blockers=_blockers(result, items, conditions),
        prep_from_career_strategy=_prep_from_queue(career_decision, missing + unproven),
        biggest_gap=result.biggest_gap,
    )

