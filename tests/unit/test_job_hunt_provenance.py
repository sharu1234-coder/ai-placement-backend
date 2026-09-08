"""Where a skill claim comes from, and what we are allowed to call proof.

Spec phase 5 is explicit that a resume mention must never be presented as verified evidence
"where the Evidence Engine rejects that interpretation". These tests exist to make that
non-negotiable rather than a comment: every one of them fails loudly if the mapping is ever
widened to promote a claim.
"""

from app.services.job_hunt import provenance


def _skills(items):
    return {item.skill: item.status for item in items}


class TestVerifiedRequiresRealProof:
    def test_verified_evidence_state_is_shown_as_verified(self):
        result = provenance.classify(
            ["python"], [], {"python": "verified"}, student_has_skills=True
        )
        assert _skills(result) == {"python": "verified_evidence"}

    def test_strong_evidence_state_is_shown_as_verified(self):
        # `strong` already requires a source the student did not simply assert about
        # themselves -- see evidence_engine._compute_state_and_confidence.
        result = provenance.classify(["sql"], [], {"sql": "strong"}, student_has_skills=True)
        assert _skills(result) == {"sql": "verified_evidence"}

    def test_a_single_supported_source_is_a_claim_not_proof(self):
        # This is the case the rule exists for: uploading a resume produces exactly one
        # source, and one source must not read to the student as corroboration.
        result = provenance.classify(["java"], [], {"java": "supported"}, student_has_skills=True)
        assert _skills(result) == {"java": "on_resume"}

    def test_a_bare_claim_is_a_claim(self):
        result = provenance.classify(["go"], [], {"go": "claimed"}, student_has_skills=True)
        assert _skills(result) == {"go": "on_resume"}

    def test_stale_evidence_is_demoted_to_a_claim_and_says_why(self):
        result = provenance.classify(["rust"], [], {"rust": "stale"}, student_has_skills=True)
        assert result[0].status == "on_resume"
        assert result[0].evidence_state == "stale"
        assert "three months" in result[0].detail

    def test_a_skill_with_no_evidence_entry_is_treated_as_unproven(self):
        # A missing key and an explicit "claimed" must mean the same thing; anything else
        # would make provenance depend on whether the engine happened to return a row.
        result = provenance.classify(["scala"], [], {}, student_has_skills=True)
        assert _skills(result) == {"scala": "on_resume"}


class TestMissingVersusUnclear:
    def test_a_skill_the_student_lacks_is_missing_when_we_know_their_skills(self):
        result = provenance.classify(
            ["python"], ["kubernetes"], {"python": "verified"}, student_has_skills=True
        )
        assert _skills(result)["kubernetes"] == "missing"

    def test_every_skill_is_unclear_when_we_hold_no_skills_for_the_student(self):
        # Absence of data about a student is not evidence about the student. Calling these
        # "missing" would tell someone who has not filled in their profile that they lack
        # skills we never asked them about.
        result = provenance.classify(
            [], ["python", "sql"], {}, student_has_skills=False
        )
        assert set(_skills(result).values()) == {"unclear"}
        assert all(item.evidence_state is None for item in result)


class TestVerifiedCount:
    def test_counts_only_proven_skills(self):
        items = provenance.classify(
            ["python", "sql", "java"],
            ["kubernetes"],
            {"python": "verified", "sql": "strong", "java": "supported"},
            student_has_skills=True,
        )
        assert provenance.verified_count(items) == 2

    def test_is_zero_when_nothing_is_proven(self):
        items = provenance.classify(["python"], [], {"python": "claimed"}, student_has_skills=True)
        assert provenance.verified_count(items) == 0


class TestOrderingAndShape:
    def test_matched_skills_come_before_gaps(self):
        items = provenance.classify(
            ["python"], ["kubernetes"], {"python": "verified"}, student_has_skills=True
        )
        assert [item.skill for item in items] == ["python", "kubernetes"]

    def test_every_status_is_one_of_the_four_declared_buckets(self):
        items = provenance.classify(
            ["a", "b"], ["c"], {"a": "verified", "b": "claimed"}, student_has_skills=True
        )
        assert all(item.status in provenance.SKILL_PROVENANCE_STATUSES for item in items)


class TestMatchStrength:
    """The badge shown beside each skill. Derived from the bucket so the two cannot disagree."""

    def _one(self, status, state=None):
        return provenance.SkillProvenance(skill="python", status=status, evidence_state=state)

    def test_proven_evidence_reads_as_a_strong_match(self):
        assert provenance.match_strength(self._one(provenance.VERIFIED_EVIDENCE)) == "strong_match"

    def test_a_resume_claim_still_reads_as_a_match(self):
        # Credible on a CV screen even though it is only a claim to us. Calling it "weak"
        # would understate what a recruiter actually sees.
        assert provenance.match_strength(self._one(provenance.ON_RESUME)) == "match"

    def test_a_skill_the_student_lacks_reads_as_a_gap(self):
        assert provenance.match_strength(self._one(provenance.MISSING)) == "skill_gap"

    def test_an_unknown_skill_reads_as_unclear_not_as_a_gap(self):
        assert provenance.match_strength(self._one(provenance.UNCLEAR)) == "unclear"

    def test_every_bucket_has_a_badge(self):
        # A new bucket must be a KeyError here, not a silent fall-through to a badge that
        # flatters the student.
        for status in provenance.SKILL_PROVENANCE_STATUSES:
            assert provenance.match_strength(self._one(status)) in provenance.MATCH_STRENGTHS
