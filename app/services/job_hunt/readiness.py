"""Application readiness: "am I ready to apply to *this* job?"

Distinct from the match score, and the difference is the whole point of the screen. The match
score answers *does this job suit me* -- a question about the advert. Readiness answers *can I
make a credible application to it today* -- a question about the student. A perfect-fit job you
cannot yet evidence is a high match and a low readiness, and collapsing the two into one number
would hide exactly the gap the student needs to see.

Every dimension is computed from something already recorded elsewhere in this product, and
every one of them can come back as unknown:

  skills      the advert's own required skills, weighted by whether the student can *prove*
              each one (Evidence Engine) or merely listed it
  experience  the match engine's experience_fit component, unchanged
  resume      the latest ATS check's overall score for this student's resume
  education   whether the advert states a degree requirement, and what the profile records
  interview   the student's own interview-evaluation history

The same rule the match engine uses applies here and for the same reason: a dimension that
cannot be judged is excluded from both the numerator and the denominator, never scored zero.
Scoring "we have no interview history for you" as 0% interview readiness would tell a student
they are unprepared for something they have simply not attempted yet, and the honest answer is
that we do not know. Anything excluded is named in `unknown_dimensions`, which the UI shows.

There is no AI in this file. Every number is arithmetic over rows a student can go and look at.
"""

import dataclasses
import datetime
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.interview import Interview
from app.models.interview_evaluation import InterviewEvaluation
from app.models.job_listing import JobListing
from app.models.profile import StudentProfile
from app.models.resume_ats_check import ResumeAtsCheck
from app.services.job_hunt import provenance

# Weights across the five dimensions. Skills dominates because it is the thing a recruiter
# screens on first and the thing the student can actually change before applying. Education is
# lightest: for the campus-hiring market this product serves it is usually a gate that is
# either already passed or cannot be moved before the deadline.
READINESS_WEIGHTS: Dict[str, float] = {
    "skills": 0.35,
    "experience": 0.20,
    "resume": 0.20,
    "education": 0.10,
    "interview": 0.15,
}

# A skill the student can prove counts fully; one they have only listed counts for this much.
# Not zero -- a resume claim is real information and recruiters do read resumes -- and not one,
# because the entire premise of the Evidence Engine is that a claim and a proof differ.
_UNPROVEN_SKILL_CREDIT = 0.6

# Interview evaluations older than this stop counting toward readiness. Interview skill decays,
# and a strong evaluation from a year ago is not evidence about today.
_INTERVIEW_RECENCY_DAYS = 180

# Degree words we can recognise in an advert. Deliberately small and literal: a fuzzy match
# here would invent an education requirement the employer never stated, and then tell the
# student they fail it.
_DEGREE_TERMS = (
    "bachelor",
    "b.tech",
    "btech",
    "b.e.",
    "be degree",
    "master",
    "m.tech",
    "mtech",
    "mba",
    "phd",
    "degree in",
    "graduate degree",
)


@dataclasses.dataclass(frozen=True)
class ReadinessDimension:
    key: str
    label: str
    # None when the dimension could not be judged. Never 0.0 as a stand-in for "unknown".
    score: Optional[float]
    # One sentence saying what the number is derived from, so a student can check our working.
    detail: str


@dataclasses.dataclass(frozen=True)
class ReadinessResult:
    # None when nothing at all could be judged, which the UI renders as "not enough
    # information" rather than as 0%.
    overall: Optional[float]
    band: Optional[str]
    dimensions: List[ReadinessDimension]
    unknown_dimensions: List[str]
    # The single most improvable gap, with a concrete before/after. None when there is nothing
    # actionable -- we never manufacture a task to fill the space.
    biggest_gap: Optional[Dict[str, object]]
    tips: List[str]


def _band(score: float) -> str:
    """Four bands, worded as advice rather than as a grade. "Not ready" is deliberately absent:
    a low readiness means there is preparation to do, not that the student should not apply --
    that is their call, and adverts routinely over-state requirements."""
    if score >= 0.8:
        return "strong_candidate"
    if score >= 0.6:
        return "worth_applying"
    if score >= 0.4:
        return "stretch"
    return "prepare_first"


def _skills_dimension(
    items: List[provenance.SkillProvenance],
) -> ReadinessDimension:
    """Weighted by proof, not by count.

    A student matching six of six skills where five are only resume claims is in a genuinely
    weaker position than one matching five of six with four proven, and a plain count would
    rank them the other way round.
    """
    considered = [
        item
        for item in items
        if item.status in (provenance.VERIFIED_EVIDENCE, provenance.ON_RESUME, provenance.MISSING)
    ]
    if not considered:
        # Either the advert named no skills we recognise, or we hold no skills for the student
        # (in which case every item is UNCLEAR). Both are absences of information about the
        # pairing, not evidence of a weak candidate.
        return ReadinessDimension(
            key="skills",
            label="Skills",
            score=None,
            detail="This listing doesn't name skills we can check you against",
        )

    proven = sum(1 for item in considered if item.status == provenance.VERIFIED_EVIDENCE)
    claimed = sum(1 for item in considered if item.status == provenance.ON_RESUME)
    credit = proven + (claimed * _UNPROVEN_SKILL_CREDIT)
    score = min(1.0, credit / len(considered))

    if proven and claimed:
        detail = f"{proven} of {len(considered)} skills proven, {claimed} listed on your resume"
    elif proven:
        detail = f"{proven} of {len(considered)} skills backed by verified evidence"
    elif claimed:
        detail = f"{claimed} of {len(considered)} skills on your resume, none proven yet"
    else:
        detail = f"None of the {len(considered)} skills this job asks for are on your profile"
    return ReadinessDimension(key="skills", label="Skills", score=score, detail=detail)


def _experience_dimension(component_scores: Dict[str, float]) -> ReadinessDimension:
    score = component_scores.get("experience_fit")
    if score is None:
        return ReadinessDimension(
            key="experience",
            label="Experience",
            score=None,
            detail="Either this listing states no seniority, or your experience level isn't set",
        )
    return ReadinessDimension(
        key="experience",
        label="Experience",
        score=score,
        detail="Based on the seniority this listing states against your experience level",
    )


def _resume_dimension(db: Session, profile_id: int) -> ReadinessDimension:
    """The student's most recent ATS check, reused rather than recomputed.

    Deliberately the existing score and not a new job-specific one: the ATS module already owns
    "how well does this resume read", it has been tuned and tested, and a second scorer here
    would give a student two different resume scores in one app with no way to reconcile them.
    """
    check = (
        db.query(ResumeAtsCheck)
        .filter(ResumeAtsCheck.profile_id == profile_id)
        .order_by(ResumeAtsCheck.id.desc())
        .first()
    )
    if check is None:
        return ReadinessDimension(
            key="resume",
            label="Resume",
            score=None,
            detail="Run an ATS check on your resume and this fills in",
        )
    return ReadinessDimension(
        key="resume",
        label="Resume",
        score=max(0.0, min(1.0, check.overall_score / 100.0)),
        detail=f"Your latest ATS check scored {check.overall_score}%",
    )


def _education_dimension(
    profile: Optional[StudentProfile], listing: JobListing
) -> ReadinessDimension:
    """Three genuinely different situations, kept apart.

    The advert names a degree and we know the student has one -> met. The advert names one and
    we hold no education for the student -> unknown, because not knowing their education is not
    the same as knowing they lack it. The advert names none and the student has a degree
    recorded -> met, and the detail says *why* it is met so a full bar never looks like a
    guess.
    """
    has_degree = bool(profile and (profile.degree or profile.graduation_year))
    text = " ".join(filter(None, [listing.title, listing.description or ""])).lower()
    advert_states_requirement = any(term in text for term in _DEGREE_TERMS)

    if not has_degree:
        return ReadinessDimension(
            key="education",
            label="Education",
            score=None,
            detail="Add your degree to your profile and this fills in",
        )
    if advert_states_requirement:
        return ReadinessDimension(
            key="education",
            label="Education",
            score=1.0,
            detail="This listing asks for a degree and your profile records one",
        )
    return ReadinessDimension(
        key="education",
        label="Education",
        score=1.0,
        detail="This listing states no specific degree requirement",
    )


def _interview_dimension(
    db: Session, profile_id: int, now: Optional[datetime.datetime] = None
) -> ReadinessDimension:
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    cutoff = now - datetime.timedelta(days=_INTERVIEW_RECENCY_DAYS)
    rows = (
        db.query(InterviewEvaluation)
        .join(Interview, Interview.id == InterviewEvaluation.interview_id)
        .filter(Interview.profile_id == profile_id)
        .filter(InterviewEvaluation.created_at >= cutoff)
        .all()
    )
    scored = [row.overall_score for row in rows if row.overall_score is not None]
    if not scored:
        return ReadinessDimension(
            key="interview",
            label="Interview readiness",
            score=None,
            detail="Complete a mock interview and this fills in",
        )
    # The most recent evaluations, averaged. Not the best one: a single strong showing months
    # of practice ago is not what a student will walk into an interview with.
    #
    # InterviewEvaluation.overall_score is already a 0.0-1.0 unit score -- interview_service
    # clamps every dimension to that range before persisting -- so it is used as-is. Dividing
    # by 100 here (as the ATS score legitimately needs) would have reported every student's
    # interview readiness as under one percent.
    average = sum(scored) / len(scored)
    return ReadinessDimension(
        key="interview",
        label="Interview readiness",
        score=max(0.0, min(1.0, average)),
        detail=f"Average of your last {len(scored)} mock interview evaluations",
    )


def _biggest_gap(
    dimensions: List[ReadinessDimension],
    items: List[provenance.SkillProvenance],
) -> Optional[Dict[str, object]]:
    """The one thing most worth doing before applying.

    Prefers a named missing skill over a low dimension score, because "learn Kubernetes" is
    something a student can start this afternoon and "improve your experience" is not. Returns
    None rather than inventing a task when everything we can see is already strong.
    """
    missing = [item for item in items if item.status == provenance.MISSING]
    if missing:
        skill = missing[0].skill
        return {
            "kind": "skill",
            "skill": skill,
            "title": f"Close your {skill} gap",
            "detail": f"{skill} is named in this listing and isn't on your profile yet.",
            "action": "roadmap",
        }

    unproven = [item for item in items if item.status == provenance.ON_RESUME]
    if unproven:
        skill = unproven[0].skill
        return {
            "kind": "evidence",
            "skill": skill,
            "title": f"Prove your {skill}",
            "detail": (
                f"You've listed {skill} but nothing confirms it yet. An assessment or a "
                "project would turn it into verified evidence."
            ),
            "action": "skill_passport",
        }

    weakest = min(
        (dim for dim in dimensions if dim.score is not None),
        key=lambda dim: dim.score,
        default=None,
    )
    if weakest is None or weakest.score >= 0.8:
        return None
    return {
        "kind": "dimension",
        "skill": None,
        "title": f"Improve your {weakest.label.lower()}",
        "detail": weakest.detail,
        "action": "roadmap" if weakest.key in ("skills", "experience") else weakest.key,
    }


def _tips(listing: JobListing, items: List[provenance.SkillProvenance]) -> List[str]:
    """Concrete, job-specific, and only ever derived from this advert.

    No generic interview advice: a tip that would be equally true of every job on the platform
    is noise, and a student learns quickly to skip a section that always says the same thing.
    """
    tips: List[str] = []
    missing = [item.skill for item in items if item.status == provenance.MISSING]
    unproven = [item.skill for item in items if item.status == provenance.ON_RESUME]

    if missing:
        tips.append(f"Tailor your resume for {', '.join(missing[:2])}")
    if unproven:
        tips.append(f"Be ready to give a concrete example of {unproven[0]}")
    if listing.company:
        tips.append(f"Research {listing.company} before the interview")
    return tips


def compute(
    db: Session,
    profile: Optional[StudentProfile],
    listing: JobListing,
    component_scores: Dict[str, float],
    skill_provenance: List[provenance.SkillProvenance],
    now: Optional[datetime.datetime] = None,
) -> ReadinessResult:
    """Assemble the five dimensions and the weighted mean over the ones we could judge."""
    profile_id = profile.id if profile is not None else None

    dimensions = [
        _skills_dimension(skill_provenance),
        _experience_dimension(component_scores),
        _resume_dimension(db, profile_id) if profile_id else ReadinessDimension(
            "resume", "Resume", None, "Add a resume and this fills in"
        ),
        _education_dimension(profile, listing),
        _interview_dimension(db, profile_id, now) if profile_id else ReadinessDimension(
            "interview", "Interview readiness", None, "Complete a mock interview and this fills in"
        ),
    ]

    judged = {dim.key: dim.score for dim in dimensions if dim.score is not None}
    unknown = [dim.label for dim in dimensions if dim.score is None]

    if judged:
        used_weight = sum(READINESS_WEIGHTS[key] for key in judged)
        overall = sum(score * READINESS_WEIGHTS[key] for key, score in judged.items()) / used_weight
        overall = round(overall, 4)
        band = _band(overall)
    else:
        overall = None
        band = None

    return ReadinessResult(
        overall=overall,
        band=band,
        dimensions=dimensions,
        unknown_dimensions=unknown,
        biggest_gap=_biggest_gap(dimensions, skill_provenance),
        tips=_tips(listing, skill_provenance),
    )
