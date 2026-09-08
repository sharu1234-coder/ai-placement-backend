"""The explainable match score.

Three properties this engine is built around, all from spec section 6.

*Deterministic.* No AI anywhere in this file. The same student and the same advert always
produce the same number, so a score can be defended to a student and reproduced in a test.

*Bounded and versioned.* Every component is in [0, 1] and so is the total. SCORE_VERSION
changes whenever weights or component semantics change, so a stored score can be told apart
from one today's engine would produce, and recomputed rather than shown as though it were
current.

*Unknown stays unknown.* This is the part that is easy to get wrong and matters most. Adverts
routinely omit salary, work mode and employment type; Careerjet omits seniority entirely.
Scoring an absent field as 0 would quietly punish a job for what the *provider* failed to
send, and the student would read that as "this job is a bad fit for me". So a component that
cannot be judged is excluded from both the numerator and the denominator -- the total is the
weighted mean over the components that could actually be evaluated -- and the field is named
in `unknown_factors`, which the UI shows verbatim. A job with no salary data is not a worse
match; it is a match we know less about, and the student is told exactly that.
"""

import dataclasses
import datetime
from typing import Optional, Sequence

from app.services.job_hunt.normalization import (
    role_family,
    seniority_from_title,
    skills_intersection,
)

# Bump on any change to weights or component semantics. Read by the service to decide whether
# a stored JobListingMatch is still current; an older version is recomputed rather than shown.
#
# v2: location_fit stopped treating "different city" as a flat zero for a student who has said
# they are open to relocating. Scoring their own stated flexibility as a total mismatch made
# every out-of-city job look like a rejection to exactly the students most willing to take one.
SCORE_VERSION = "v2"

# Server-authoritative weights (spec section 6: "Keep weights server-authoritative"). Never
# read from a request, never overridable per user -- a client that could send its own weights
# could manufacture a 99% match on any advert.
WEIGHTS: dict[str, float] = {
    "role_alignment": 0.30,
    "skill_overlap": 0.30,
    "experience_fit": 0.15,
    "location_fit": 0.15,
    "employment_type_fit": 0.05,
    "freshness": 0.05,
}

# How stale an advert can be before freshness scores zero. 30 days rather than something
# tighter because Indian campus-hiring adverts routinely stay open a month.
_FRESHNESS_HORIZON_DAYS = 30

# Ordered ladder used to compare a student's experience level with an advert's. Index distance
# is the penalty, so fresher-vs-senior is scored worse than fresher-vs-junior.
_EXPERIENCE_LADDER = ("fresher", "junior", "mid", "senior", "lead")


@dataclasses.dataclass(frozen=True)
class MatchInput:
    """Everything the engine is allowed to look at. A frozen dataclass rather than passing the
    ORM rows directly, so it is impossible for a scoring rule to reach sideways into the
    database mid-computation and make the result depend on query order."""

    student_skills: list[str]
    student_role_family: Optional[str]
    student_experience_level: Optional[str]
    preferred_location: Optional[str]
    preferred_work_mode: Optional[str]
    preferred_employment_type: Optional[str]

    job_title: str
    job_skills: list[str]
    job_location: Optional[str]
    job_work_mode: Optional[str]
    job_employment_type: Optional[str]
    job_seniority: Optional[str]
    job_posted_at: Optional[datetime.datetime]

    # Defaulted fields, so they sit last: a dataclass cannot put a defaulted field before a
    # required one. Both are optional inputs the engine works without.
    job_company: Optional[str] = None
    # Their own stated flexibility. None means they never answered, which is not the same as
    # "no" -- see _location_score for why the two are scored differently.
    open_to_relocation: Optional[bool] = None
    # Employers the student named. Never a score component: a weight here would be a number
    # invented for a preference, and a student who names three companies would see every other
    # job marked down. It surfaces as a reason and as a ranking tie-break instead.
    preferred_companies: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class MatchResult:
    total_score: float
    score_version: str
    component_scores: dict[str, float]
    matched_skills: list[str]
    missing_skills: list[str]
    unknown_factors: list[str]
    rationale: str
    reasons: list[str]
    apply_readiness: Optional[str]
    # True when the advert's employer is one the student named. Reported separately from the
    # score so a client can badge it without the number having silently absorbed it.
    preferred_company_match: bool = False


def score_match(data: MatchInput, now: Optional[datetime.datetime] = None) -> MatchResult:
    """Score one advert for one student. Pure: no I/O, no clock read unless `now` is omitted."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

    components: dict[str, float] = {}
    unknown: list[str] = []
    reasons: list[str] = []

    # -- Role alignment ---------------------------------------------------------------------
    job_family = role_family(data.job_title)
    if data.student_role_family is None:
        # The student has not told us what they are targeting. That is a gap in *our* input,
        # not evidence about the job, so it is unknown rather than a zero.
        unknown.append("Your target role isn't set, so role fit couldn't be judged")
    elif job_family is None:
        unknown.append(f"We don't recognise the job title \"{data.job_title}\" as a known role family")
    elif job_family == data.student_role_family:
        components["role_alignment"] = 1.0
        reasons.append(f"This is a {_humanize(job_family)} role, which is what you're targeting")
    else:
        components["role_alignment"] = 0.0
        reasons.append(
            f"This is a {_humanize(job_family)} role, not the {_humanize(data.student_role_family)} "
            "role you're targeting"
        )

    # -- Skill overlap ----------------------------------------------------------------------
    matched, missing = skills_intersection(data.student_skills, data.job_skills)
    if not data.job_skills:
        # The advert named no skill we recognise. Common with short adverts, and scoring it as
        # zero overlap would be a statement about the student that the advert never made.
        unknown.append("This listing doesn't name specific skills we can match against")
    elif not data.student_skills:
        unknown.append("We don't have your skills yet, so skill overlap couldn't be judged")
    else:
        components["skill_overlap"] = len(matched) / len(data.job_skills)
        if matched:
            reasons.append(
                f"You match {len(matched)} of {len(data.job_skills)} skills this job asks for: "
                + ", ".join(matched[:4])
            )
        if missing:
            reasons.append("Missing for this role: " + ", ".join(missing[:4]))

    # -- Experience fit ---------------------------------------------------------------------
    # Falls back to reading the advert's title when the provider sent no seniority field --
    # a derivation from provider-supplied text, not an invention, and it yields None (not a
    # guess) when the title says nothing.
    job_level = data.job_seniority or seniority_from_title(data.job_title)
    if job_level == "intern":
        job_level = "fresher"
    if data.student_experience_level is None:
        unknown.append("Your experience level isn't set, so seniority fit couldn't be judged")
    elif job_level is None:
        unknown.append("This listing doesn't state a seniority level")
    else:
        components["experience_fit"] = _experience_score(data.student_experience_level, job_level)
        if components["experience_fit"] >= 0.99:
            reasons.append(f"The seniority ({job_level}) matches your experience level")
        elif components["experience_fit"] <= 0.34:
            reasons.append(f"This is a {job_level} role, which is a stretch from your stated level")

    # -- Location / work mode ---------------------------------------------------------------
    location_score, location_reason, location_unknown = _location_score(data)
    if location_unknown is not None:
        unknown.append(location_unknown)
    if location_score is not None:
        components["location_fit"] = location_score
        if location_reason:
            reasons.append(location_reason)

    # -- Employment type --------------------------------------------------------------------
    if data.preferred_employment_type is None:
        # No preference stated is not an unknown -- it genuinely means "anything is fine", so
        # this component is simply not part of the score rather than being reported as a gap.
        pass
    elif data.job_employment_type is None:
        unknown.append("This listing doesn't state its employment type")
    else:
        components["employment_type_fit"] = (
            1.0 if data.job_employment_type == data.preferred_employment_type else 0.0
        )

    # -- Freshness --------------------------------------------------------------------------
    if data.job_posted_at is None:
        unknown.append("This listing doesn't include a posted date")
    else:
        age_days = max((now - data.job_posted_at).days, 0)
        components["freshness"] = max(0.0, 1.0 - (age_days / _FRESHNESS_HORIZON_DAYS))
        if age_days <= 3:
            reasons.append("Posted in the last few days")

    # -- Preferred employer -----------------------------------------------------------------
    # A named company is a fact about the student's intent, not about how well they fit the
    # role, so it never enters the score. It is worth saying out loud, and worth using to
    # order two jobs the engine otherwise rates identically.
    company_match = _matches_preferred_company(data)
    if company_match:
        reasons.append(f"{data.job_company} is one of the companies you're targeting")

    total = _weighted_mean(components)

    return MatchResult(
        total_score=round(total, 4),
        score_version=SCORE_VERSION,
        component_scores={k: round(v, 4) for k, v in components.items()},
        matched_skills=matched,
        missing_skills=missing,
        unknown_factors=unknown,
        rationale=_rationale(components, matched, data),
        reasons=reasons,
        apply_readiness=_apply_readiness(data, matched, missing),
        preferred_company_match=company_match,
    )


def _weighted_mean(components: dict[str, float]) -> float:
    """Weighted mean over only the components that could be evaluated.

    Renormalizing by the weights actually used -- rather than by the full weight total -- is
    what implements "unknown is not zero". With no evaluable component at all the result is
    0.0, which the caller pairs with a full `unknown_factors` list so the UI can say "not
    enough information" instead of showing a 0% match.
    """
    used_weight = sum(WEIGHTS[name] for name in components)
    if used_weight <= 0:
        return 0.0
    return sum(score * WEIGHTS[name] for name, score in components.items()) / used_weight


def _experience_score(student_level: str, job_level: str) -> float:
    """1.0 for an exact match, decaying by ladder distance, floored at 0."""
    try:
        student_index = _EXPERIENCE_LADDER.index(student_level)
        job_index = _EXPERIENCE_LADDER.index(job_level)
    except ValueError:
        return 0.0
    return max(0.0, 1.0 - (abs(student_index - job_index) / 3.0))


def _location_score(data: MatchInput) -> tuple[Optional[float], Optional[str], Optional[str]]:
    """Returns (score, reason, unknown_note). Work mode dominates: a remote job satisfies a
    location preference regardless of where the employer is registered, which is why remote is
    checked before any string comparison of place names."""
    if data.preferred_work_mode == "remote":
        if data.job_work_mode == "remote":
            return 1.0, "This role is remote, which is what you're looking for", None
        if data.job_work_mode is None:
            return None, None, "This listing doesn't state whether it's remote"
        return 0.0, "This role isn't remote", None

    if data.preferred_location is None:
        return None, None, None
    if data.job_location is None:
        return None, None, "This listing doesn't state a location"
    if data.job_work_mode == "remote":
        return 1.0, "This role is remote, so your location preference is satisfied", None

    preferred = data.preferred_location.strip().lower()
    job_location = data.job_location.strip().lower()
    # Substring either way: "Bengaluru" should match "Bengaluru, Karnataka", and a preference
    # of "Bengaluru, India" should match a job listed simply as "Bengaluru".
    if preferred and (preferred in job_location or job_location in preferred):
        return 1.0, f"Located in {data.job_location}, matching your preference", None
    # A different city is a real, knowable mismatch -- not an unknown. How much it counts
    # against the job depends on something only the student can tell us.
    if data.open_to_relocation:
        # They said they would move. Scoring that as a total mismatch would mark down every
        # out-of-city job for exactly the students most willing to take one, and would be the
        # engine overruling a preference they stated explicitly. Not 1.0 either: relocating is
        # a real cost, and a job in their own city is genuinely a better fit.
        return (
            0.5,
            f"Located in {data.job_location} rather than {data.preferred_location}, "
            "but you're open to relocating",
            None,
        )
    # False, or never answered. These are deliberately scored the same: an unanswered
    # relocation question is not a reason to assume flexibility on the student's behalf, and
    # assuming it would quietly fill their results with cities they cannot move to.
    return 0.0, f"Located in {data.job_location}, not your preferred {data.preferred_location}", None


def _rationale(components: dict[str, float], matched: list[str], data: MatchInput) -> str:
    """One plain sentence for the job card (spec section 12).

    Built from the same component scores that produced the total, so the card and the detailed
    breakdown can never tell the student two different stories.
    """
    if not components:
        return "Not enough information about this listing to explain a match."
    strongest = max(components.items(), key=lambda item: item[1] * WEIGHTS[item[0]])
    name, value = strongest
    if value <= 0.0:
        return "This listing doesn't line up with your target role or skills."
    if name == "skill_overlap" and matched:
        return f"You already have {len(matched)} of the skills this role asks for."
    if name == "role_alignment":
        return "This role matches the job title you're targeting."
    if name == "experience_fit":
        return "The seniority of this role fits your stated experience level."
    if name == "location_fit":
        return "The location and work mode fit your preferences."
    if name == "freshness":
        return "This role was posted recently."
    return "This role partially fits your profile."


def _apply_readiness(data: MatchInput, matched: list[str], missing: list[str]) -> Optional[str]:
    """"ready" / "stretch" / "gap", or None when there is no evidence to judge it on.

    None is returned whenever the advert named no recognisable skills or the student has no
    skills recorded -- spec section 5 requires this be based only on real evidence, and a
    readiness verdict computed from nothing would be exactly the fabricated confidence signal
    the spec forbids.
    """
    if not data.job_skills or not data.student_skills:
        return None
    coverage = len(matched) / len(data.job_skills)
    if coverage >= 0.75:
        return "ready"
    if coverage >= 0.4:
        return "stretch"
    return "gap"


def matches_preferred_company(
    job_company: Optional[str], preferred_companies: Sequence[str]
) -> bool:
    """Case-insensitive containment either way, so "Acme" matches "Acme Technologies Pvt Ltd"
    and a student who typed the full legal name still matches an advert listing "Acme".

    Deliberately not fuzzy beyond that. A near-match on a company name is the kind of thing
    that reads as a mistake rather than as helpfulness -- telling a student that "Amazon Web
    Services" satisfies their interest in "Amazon Prime Video" would be a claim we cannot
    support.

    Public because the ranking step in service.py needs the same answer, and re-running the
    whole scoring pass per listing just to read one boolean off it would be the expensive way
    to duplicate a rule that has to stay singular anyway.
    """
    if not job_company or not preferred_companies:
        return False
    job = job_company.strip().lower()
    if not job:
        return False
    for preferred in preferred_companies:
        wanted = (preferred or "").strip().lower()
        if wanted and (wanted in job or job in wanted):
            return True
    return False


def _matches_preferred_company(data: MatchInput) -> bool:
    return matches_preferred_company(data.job_company, data.preferred_companies)


def _humanize(family_key: str) -> str:
    return family_key.replace("_", " ")


# -- Ranking labels -------------------------------------------------------------------------
# Spec phase 6 names four honest labels and forbids implying guaranteed employment, so every
# one of these describes the *fit we measured*, never an outcome: "Best Match" means highest
# ranked in this result set, not most likely to hire you.
RANKING_LABELS = ("best_match", "strong_fit", "stretch_opportunity", "skill_build_opportunity")

# A match at or above this is a genuine fit rather than a partial one. Shared by best_match
# and strong_fit so the top card can never be labelled "Best Match" on a score the second card
# would not even be called a strong fit for.
STRONG_FIT_THRESHOLD = 0.6
# Private alias kept so the rest of this module reads unchanged; the public name exists
# because service.py's "best matches" count has to use the same bar the labels use.
_STRONG_FIT_THRESHOLD = STRONG_FIT_THRESHOLD
# Below this, the honest framing is "something to build towards", not "a stretch".
_STRETCH_FLOOR = 0.35


def ranking_label(
    *,
    rank: Optional[int],
    total_score: float,
    component_count: int,
    apply_readiness: Optional[str],
) -> Optional[str]:
    """The label shown on a job card, or None when we have no basis for one.

    None whenever no component could be evaluated: the total is then 0.0 by construction (see
    _weighted_mean) and calling that "Skill-Build Opportunity" would dress up an absence of
    information as a verdict about the student.

    `rank` is the job's zero-based position in the ranked result set, or None when the job is
    being viewed on its own -- opening one saved job cannot make it the best match of a set
    that was never computed, so only a ranked call can produce best_match.
    """
    if component_count == 0:
        return None
    if total_score >= _STRONG_FIT_THRESHOLD:
        return "best_match" if rank == 0 else "strong_fit"
    if apply_readiness == "gap":
        return "skill_build_opportunity"
    if total_score >= _STRETCH_FLOOR:
        return "stretch_opportunity"
    return "skill_build_opportunity"
