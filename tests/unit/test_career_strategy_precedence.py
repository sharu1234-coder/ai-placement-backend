"""Evidence precedence: what wins when sources disagree, and what must never win.

These are the rules the rest of the package leans on, so they are asserted directly rather than
inferred from downstream behaviour -- a precedence bug that only shows up as a wrong gap
category three modules later is much harder to diagnose than a failing rank assertion here.
"""

import pytest
from pydantic import ValidationError

from app.services.career_strategy import precedence


class TestAClaimNeverOutranksEvidence:
    """The single most important property in this module. Everything else is bookkeeping."""

    @pytest.mark.parametrize("claim", ["resume_claim", "user_claim"])
    @pytest.mark.parametrize("evidence", ["observed", "verified"])
    def test_no_claim_outranks_any_evidence(self, claim, evidence):
        assert precedence.outranks(evidence, claim) is True
        assert precedence.outranks(claim, evidence) is False

    def test_a_resume_claim_does_not_outrank_verified_evidence(self):
        assert precedence.rank("resume_claim") < precedence.rank("verified")

    def test_a_user_entered_claim_does_not_outrank_a_measurement(self):
        assert precedence.rank("user_claim") < precedence.rank("observed")

    def test_an_inferred_rollup_does_not_outrank_the_measurement_it_came_from(self):
        # A derived aggregate loses detail; the underlying measurement stays authoritative.
        assert precedence.rank("inferred") < precedence.rank("observed")

    def test_missing_is_the_floor_and_is_not_a_claim(self):
        assert precedence.rank("missing") == 0
        assert precedence.is_claim("missing") is False
        assert precedence.is_evidenced("missing") is False


class TestAgreementDoesNotUpgrade:
    """§ "Do not upgrade evidence because another source says the same thing." This is the rule
    evidence_engine already enforces one level down, restated where combination happens."""

    def test_two_claims_combine_to_a_claim(self):
        assert precedence.combine(["resume_claim", "user_claim"]) in {"resume_claim", "user_claim"}
        assert precedence.is_claim(precedence.combine(["resume_claim", "user_claim"])) is True

    def test_many_identical_claims_never_become_evidence(self):
        assert precedence.combine(["resume_claim"] * 10) == "resume_claim"

    def test_a_measurement_alongside_claims_combines_to_the_measurement(self):
        assert precedence.combine(["resume_claim", "observed", "user_claim"]) == "observed"

    def test_combine_returns_the_strongest_never_a_sum(self):
        assert precedence.combine(["inferred", "inferred", "inferred"]) == "inferred"

    def test_combining_nothing_is_missing_not_an_error(self):
        assert precedence.combine([]) == "missing"


class TestVocabularyIsClosed:
    def test_every_declared_kind_has_a_rank(self):
        assert set(precedence.EVIDENCE_KINDS) == set(precedence.RANKS)

    def test_an_unknown_kind_raises_rather_than_ranking_as_zero(self):
        # Silently ranking a typo at 0 would demote real evidence to "missing" -- the exact
        # failure this layer exists to make impossible.
        with pytest.raises(precedence.UnknownEvidenceKindError):
            precedence.rank("verifed")

    def test_claim_and_evidence_sets_do_not_overlap(self):
        assert precedence.CLAIM_KINDS.isdisjoint(precedence.EVIDENCED_KINDS)

    def test_the_two_claim_kinds_are_deliberately_equal_in_weight(self):
        # evidence_engine draws no distinction between them, so neither does this.
        assert precedence.rank("user_claim") == precedence.rank("resume_claim")


class TestResolutionPreservesTheLosingFact:
    def test_the_overridden_source_is_kept_not_discarded(self):
        resolution = precedence.resolve(
            "AWS", [("resume:AWS", "resume_claim"), ("assessment:AWS", "observed")]
        )
        assert resolution.winning_kind == "observed"
        assert resolution.winner_keys == ["assessment:AWS"]
        # Without this the "your resume says more than your evidence shows" explanation has
        # nothing to cite.
        assert resolution.overridden_keys == ["resume:AWS"]

    def test_a_claim_overridden_by_evidence_is_flagged(self):
        resolution = precedence.resolve(
            "AWS", [("resume:AWS", "resume_claim"), ("assessment:AWS", "observed")]
        )
        assert resolution.claim_was_overridden is True

    def test_a_claim_overridden_by_another_claim_is_not_flagged(self):
        resolution = precedence.resolve(
            "AWS", [("resume:AWS", "resume_claim"), ("project:AWS", "user_claim")]
        )
        # Equal rank: neither overrides the other, so there is nothing to explain.
        assert resolution.overridden_keys == []
        assert resolution.claim_was_overridden is False

    def test_ties_keep_every_winner_rather_than_picking_one_arbitrarily(self):
        resolution = precedence.resolve(
            "SQL", [("assessment:SQL", "observed"), ("practice:SQL", "observed")]
        )
        assert resolution.winner_keys == ["assessment:SQL", "practice:SQL"]

    def test_resolving_nothing_returns_none_not_missing(self):
        # "There was nothing to resolve" and "we resolved it to nothing" are different answers.
        assert precedence.resolve("SQL", []) is None

    def test_resolution_is_deterministic_regardless_of_input_order(self):
        pairs = [("a:SQL", "observed"), ("b:SQL", "resume_claim"), ("c:SQL", "observed")]
        first = precedence.resolve("SQL", pairs)
        second = precedence.resolve("SQL", list(reversed(pairs)))
        assert first.model_dump() == second.model_dump()

    def test_a_resolution_cannot_be_mutated_after_the_fact(self):
        resolution = precedence.resolve("SQL", [("assessment:SQL", "observed")])
        with pytest.raises(ValidationError):
            resolution.winning_kind = "resume_claim"
