from collections import Counter

from app.services.placement_alarm_service import REQUIRED_QUESTIONS, _assign_six, _placement_relevant


def test_placement_relevant_filters_out_hobbies_and_soft_skills():
    skills = ["Python", "SQL", "Machine Learning", "Cooking", "Photography", "Leadership"]
    result = _placement_relevant(skills)
    assert "Cooking" not in result
    assert "Photography" not in result
    assert "Leadership" not in result
    assert "Python" in result
    assert "SQL" in result
    assert "Machine Learning" in result


def test_placement_relevant_is_case_insensitive():
    assert _placement_relevant(["COOKING", "Python"]) == ["Python"]


def test_placement_relevant_keeps_everything_that_is_a_recognized_placement_topic():
    skills = ["Python", "SQL", "DSA"]
    assert _placement_relevant(skills) == skills


def test_placement_relevant_matches_free_text_variants_via_token_boundaries():
    skills = ["Data Structures & Algorithms (DSA)", "React.js", "AWS Cloud"]
    result = _placement_relevant(skills)
    assert result == skills


def test_placement_relevant_excludes_arbitrary_non_technical_resume_entries():
    # Real resume entries that are neither hobbies nor placement/interview-prep topics --
    # being on the resume is not sufficient, only recognized technical/aptitude topics qualify.
    skills = ["Public Speaking", "Event Management", "Volunteering"]
    assert _placement_relevant(skills) == []


def test_required_questions_is_fixed_at_six():
    # Product rule: never a user setting, never configurable.
    assert REQUIRED_QUESTIONS == 6


def test_assign_six_with_one_skill_uses_it_for_all_six():
    result = _assign_six(["SQL"])
    assert result == ["SQL"] * 6


def test_assign_six_with_three_skills_distributes_with_repeats_and_no_invented_skills():
    pool = ["Python", "SQL", "DSA"]
    result = _assign_six(pool)
    assert len(result) == 6
    assert set(result) <= set(pool)  # never a skill outside the actual pool
    # Every pool skill appears at least once -- a "distribute across" mix, not all-on-one.
    counts = Counter(result)
    assert set(counts) == set(pool)


def test_assign_six_with_six_or_more_skills_uses_distinct_skills_no_repeats():
    pool = ["Python", "SQL", "DSA", "Machine Learning", "Data Science", "Power BI", "Java"]
    result = _assign_six(pool)
    assert len(result) == 6
    assert len(set(result)) == 6  # no repeats needed when the pool already has >= 6
    assert set(result) <= set(pool)


def test_assign_six_always_returns_exactly_six_regardless_of_pool_size():
    for pool_size in range(1, 10):
        pool = [f"skill-{i}" for i in range(pool_size)]
        assert len(_assign_six(pool)) == REQUIRED_QUESTIONS
