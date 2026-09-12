"""Deterministic evidence precedence -- what to believe when two sources disagree.

This module owns the *vocabulary* of evidence kinds and their ordering. Everything else in
career_strategy imports both from here, so there is exactly one place where "is a resume
claim weaker than a measured assessment result" is answered, and no way for two modules to
drift onto different answers.

The ordering encodes one question only: **how much does this signal tell us about what the
student can actually do?**

    verified (5)      several independent sources agree AND at least one is strongly scored.
                      This is evidence_engine's own `verified` state -- we do not re-derive it.
    observed (4)      the product measured or witnessed it: an assessment score, a graded
                      interview, a completed guided project.
    inferred (3)      a derived aggregate over real data (Career Twin readiness, an ATS
                      category). Weaker than the measurements it is computed from, because a
                      rollup loses the detail; stronger than an unbacked claim, because it is
                      at least grounded in something we observed.
    user_claim (1)    the student typed it into the product (a free-text Project row).
    resume_claim (1)  a keyword matched on their uploaded resume.
    missing (0)       we have nothing. Not "zero ability" -- *nothing*.

`user_claim` and `resume_claim` deliberately share a rank. evidence_engine treats both as
`self_asserted` and draws no distinction between them, and inventing one here -- deciding that
typing a project into a form is more credible than writing it on a resume, or the reverse --
would be exactly the kind of unbacked judgement this layer exists to avoid. They stay distinct
*kinds* (so a contradiction can say which one it saw) with equal *weight*.

Two rules matter more than the numbers:

**A claim never outranks evidence.** `resume_claim` and `user_claim` sit below `observed` and
`verified`, so no arrangement of claims can override a measurement. This is asserted directly
in the tests rather than left implicit in the table.

**Agreement does not upgrade.** `combine()` returns the strongest kind present -- never a sum,
never a bonus for corroboration. Two resume mentions of AWS are two resume mentions of AWS.
This mirrors the rule evidence_engine already enforces one level down (its `strong` state
requires a non-self-asserted source precisely because breadth alone was once satisfiable by
two claims and produced a "you're strong at this!" notification for a skill nothing had ever
verified).

`missing` is a real, first-class kind, not a null. A gap classifier needs to distinguish "we
looked and there is nothing" from "we did not look", and callers need to be able to say which
of the two they are holding.
"""

from typing import Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict

# Ordered weakest to strongest, purely for readability -- RANKS below is the authority.
EVIDENCE_KINDS: Tuple[str, ...] = (
    "missing",
    "resume_claim",
    "user_claim",
    "inferred",
    "observed",
    "verified",
)

RANKS = {
    "missing": 0,
    "resume_claim": 1,
    "user_claim": 1,
    "inferred": 3,
    "observed": 4,
    "verified": 5,
}

# The kinds that are, by themselves, only an assertion by the student. Named once here so gap
# classification and contradiction detection cannot disagree about what counts as a claim.
CLAIM_KINDS = frozenset({"resume_claim", "user_claim"})

# The kinds that represent something the product actually measured or witnessed.
EVIDENCED_KINDS = frozenset({"observed", "verified"})


class UnknownEvidenceKindError(ValueError):
    """Raised for a kind outside EVIDENCE_KINDS. Deliberately loud: a typo'd kind that
    silently ranked as 0 would quietly demote real evidence to "missing", which is the exact
    failure mode this layer is supposed to make impossible."""


def rank(kind: str) -> int:
    try:
        return RANKS[kind]
    except KeyError:
        raise UnknownEvidenceKindError(f"Unknown evidence kind {kind!r}; expected one of {sorted(RANKS)}")


def outranks(left: str, right: str) -> bool:
    """Strictly stronger. Equal-ranked kinds (user_claim vs resume_claim) outrank neither."""
    return rank(left) > rank(right)


def is_claim(kind: str) -> bool:
    rank(kind)  # validate
    return kind in CLAIM_KINDS


def is_evidenced(kind: str) -> bool:
    rank(kind)  # validate
    return kind in EVIDENCED_KINDS


def combine(kinds: Iterable[str]) -> str:
    """The strongest kind present -- never an upgrade for agreement.

    Three resume claims combine to `resume_claim`, not to `observed`. An assessment score
    alongside a resume mention combines to `observed`, because the measurement is what we
    learned; the resume mention adds nothing to what the student can do, only to what they say.
    Returns `missing` for an empty iterable, which is the honest answer: nothing was supplied.
    """
    strongest = "missing"
    for kind in kinds:
        if rank(kind) > rank(strongest):
            strongest = kind
    return strongest


class PrecedenceResolution(BaseModel):
    """The outcome of resolving disagreeing sources about one subject.

    `winner_keys` and `overridden_keys` are both populated on purpose. §44 asks for
    documented, deterministic precedence -- not for the losing source to disappear. A
    contradiction can only be explained ("your resume presents AWS strongly, but your
    supporting evidence is limited") if the overridden fact is still there to name.
    """

    model_config = ConfigDict(frozen=True)

    subject: str
    winning_kind: str
    winner_keys: List[str]
    overridden_keys: List[str]
    # True when at least one overridden source is a claim and the winner is evidenced, i.e.
    # this subject is a candidate for the resume-claim-exceeds-evidence contradiction and for
    # RESUME_GAP/EVIDENCE_GAP classification. Computed here so both consumers agree.
    claim_was_overridden: bool


def resolve(subject: str, kinds_by_key: Sequence[Tuple[str, str]]) -> Optional[PrecedenceResolution]:
    """Resolve `[(signal_key, evidence_kind), ...]` for one subject.

    Ties are kept, not broken: every key at the winning rank is a winner. Picking one
    arbitrarily would make the output depend on dict/query ordering, and §68 requires the same
    inputs to produce the same result every time. Key lists are sorted for the same reason.
    Returns None for an empty input -- there is nothing to resolve, which is different from
    resolving to "missing".
    """
    if not kinds_by_key:
        return None

    top = max(rank(kind) for _key, kind in kinds_by_key)
    winner_keys = sorted(key for key, kind in kinds_by_key if rank(kind) == top)
    overridden = [(key, kind) for key, kind in kinds_by_key if rank(kind) < top]
    # Among equally-ranked winners the kind is the same by construction except for the
    # user_claim/resume_claim pair, which share rank 1; report deterministically.
    winning_kind = sorted({kind for _key, kind in kinds_by_key if rank(kind) == top})[0]

    return PrecedenceResolution(
        subject=subject,
        winning_kind=winning_kind,
        winner_keys=winner_keys,
        overridden_keys=sorted(key for key, _kind in overridden),
        claim_was_overridden=any(is_claim(kind) for _key, kind in overridden) and is_evidenced(winning_kind),
    )
