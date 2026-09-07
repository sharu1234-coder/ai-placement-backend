"""Where each skill claim actually comes from.

Spec phase 5 requires every skill on a job to be presented in exactly one of four buckets --
On Your Resume / Verified Evidence / Missing / Unclear -- and phase 5 is explicit that a
resume mention must never be shown as verified evidence "where the Evidence Engine rejects
that interpretation". So this module never decides for itself what counts as proof: it asks
app/services/evidence_engine.py, which already owns that rule for Career Twin and the Skill
Passport, and maps its five states onto these four buckets. One definition of "verified"
across the whole product, not a second one that quietly disagrees.

The mapping is deliberately asymmetric. `verified` and `strong` are the only two evidence
states the engine will produce without a self-asserted-only source set, so they are the only
two that may say VERIFIED here. `claimed`, `supported` and `stale` all become ON_RESUME --
including `supported`, which is a single real source, because a single source is exactly what
a resume upload alone produces and the student would otherwise read one keyword match as
corroboration.

The fourth bucket is the one that is easy to skip and matters most. A skill the job asks for
is only MISSING when we know the student's skills and this is not among them. When we hold no
skills for the student at all, every job skill is UNCLEAR instead -- absence of data about a
student is not evidence about the student. That is the same "unknown is not zero" rule
matching.py applies to score components, applied here at the level of a single skill.
"""

import dataclasses
from typing import Dict, List, Optional

# The four buckets, in the order the UI lists them.
VERIFIED_EVIDENCE = "verified_evidence"
ON_RESUME = "on_resume"
MISSING = "missing"
UNCLEAR = "unclear"

SKILL_PROVENANCE_STATUSES = (VERIFIED_EVIDENCE, ON_RESUME, MISSING, UNCLEAR)

# Evidence-engine states that carry independent, non-self-asserted proof. Kept as an explicit
# set rather than a threshold on `confidence` so that a future change to the engine's
# confidence numbers cannot silently promote a claim into verified evidence here.
_PROVEN_EVIDENCE_STATES = frozenset({"verified", "strong"})


@dataclasses.dataclass(frozen=True)
class SkillProvenance:
    """One skill the advert asks for, and how well the student can actually back it."""

    skill: str
    status: str
    # The evidence_engine state this was derived from, carried through so the UI can explain
    # itself ("last confirmed over 90 days ago") and so a test can assert on the derivation
    # rather than only on the bucket. None for skills the student does not have.
    evidence_state: Optional[str] = None
    detail: Optional[str] = None


_DETAILS = {
    "verified": "Confirmed by your assessments, projects and interviews",
    "strong": "Backed by more than one thing you have actually done",
    "supported": "One source backs this so far",
    "claimed": "You have listed this, but nothing confirms it yet",
    "stale": "Last confirmed over three months ago",
}


def classify(
    matched_skills: List[str],
    missing_skills: List[str],
    evidence_states: Dict[str, str],
    *,
    student_has_skills: bool,
) -> List[SkillProvenance]:
    """Bucket every skill this advert asks for.

    `evidence_states` maps a skill name to an evidence_engine state; a skill absent from it is
    treated as having no evidence, which is the correct reading -- the engine returns
    "claimed" for a skill with nothing behind it, so a missing key and a "claimed" value mean
    the same thing and both land in ON_RESUME for a matched skill.
    """
    out: List[SkillProvenance] = []
    for skill in matched_skills:
        state = evidence_states.get(skill)
        proven = state in _PROVEN_EVIDENCE_STATES
        out.append(
            SkillProvenance(
                skill=skill,
                status=VERIFIED_EVIDENCE if proven else ON_RESUME,
                evidence_state=state,
                detail=_DETAILS.get(state or "claimed"),
            )
        )
    for skill in missing_skills:
        if student_has_skills:
            out.append(
                SkillProvenance(
                    skill=skill,
                    status=MISSING,
                    detail="This job asks for it and it isn't on your profile",
                )
            )
        else:
            out.append(
                SkillProvenance(
                    skill=skill,
                    status=UNCLEAR,
                    detail="We don't have your skills yet, so we can't tell",
                )
            )
    return out


def verified_count(provenance: List[SkillProvenance]) -> int:
    """How many of this advert's skills the student can actually prove.

    Used as a ranking signal (spec phase 6, "evidence strength"), deliberately *after* the
    match score rather than inside it: the score answers "does this job fit me", and folding
    proof strength into it would let a well-evidenced poor fit outrank a genuine one.
    """
    return sum(1 for item in provenance if item.status == VERIFIED_EVIDENCE)
