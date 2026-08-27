from app.services.ats_scoring.config import CATEGORY_WEIGHTS
from app.services.ats_scoring.engine import score_resume
from tests.fixtures.pdf_helpers import (
    make_multi_column_pdf,
    make_pdf_with_text,
    make_single_column_pdf_with_right_aligned_dates,
)

STRONG_RESUME_TEXT = """G. Sharath Kumar
sharathkumar@example.com | +91 9876543210
linkedin.com/in/sharathkumar | github.com/sharathkumar

SUMMARY
Software developer skilled in Python and SQL, focused on backend performance and reliability.

SKILLS
Python, SQL, Java, Git, Docker, REST API

EXPERIENCE
Software Development Intern
- Developed REST APIs using Python and FastAPI, serving 10,000+ daily requests
- Reduced average response time by 30% through query optimization

PROJECTS
Placement Prep App
- Built a resume analyzer processing 500+ resumes using Python and PyMuPDF

EDUCATION
XYZ University

CERTIFICATIONS
AWS Certified Cloud Practitioner
"""

MINIMAL_RESUME_TEXT = "Just a name and nothing else."


def _score(text=STRONG_RESUME_TEXT, pdf_bytes=None, skills=None, experience=None, projects=None):
    return score_resume(
        raw_text=text,
        pdf_bytes=pdf_bytes,
        extracted_skills=skills if skills is not None else ["Python", "SQL", "Java", "Git", "Docker"],
        extracted_education=[{"institution": "XYZ University"}],
        extracted_experience=experience
        if experience is not None
        else [{"company": "ABC", "title": "Intern", "description": "Built REST APIs serving 10,000+ requests"}],
        extracted_projects=projects
        if projects is not None
        else [{"title": "Placement Prep App", "description": "Analyzed 500+ resumes", "tech_stack": "Python"}],
        extracted_certifications=[{"name": "AWS Certified Cloud Practitioner"}],
    )


def test_identical_input_produces_identical_score_every_time():
    result1 = _score(pdf_bytes=make_pdf_with_text(STRONG_RESUME_TEXT))
    result2 = _score(pdf_bytes=make_pdf_with_text(STRONG_RESUME_TEXT))
    assert result1 == result2


def test_strong_resume_scores_well():
    result = _score(pdf_bytes=make_pdf_with_text(STRONG_RESUME_TEXT))
    assert result.overall_score >= 70


def test_overall_score_matches_weighted_sum_of_category_scores():
    result = _score()
    expected = round(sum(result.category_scores[cat] * weight for cat, weight in CATEGORY_WEIGHTS.items()))
    assert result.overall_score == expected


def test_all_category_scores_are_in_valid_range():
    result = _score()
    for score in result.category_scores.values():
        assert 0 <= score <= 100


def test_missing_sections_lowers_structure_score_and_records_issues():
    result = _score(text="Nothing but a name here.", skills=[], experience=[], projects=[])
    assert result.category_scores["structure"] < 100
    structure_issue_categories = [i["category"] for i in result.issues]
    assert "structure" in structure_issue_categories


def test_zero_experience_does_not_produce_harsh_penalty():
    result = _score(experience=[])
    assert result.category_scores["experience"] >= 70
    # No issue should exist for a student simply not having experience yet -- only a
    # gentle score adjustment, per the product's explicit "never make the user feel bad."
    assert not any(i["category"] == "experience" and i["severity"] == "high" for i in result.issues)


def test_missing_contact_info_is_detected():
    result = _score(text="EDUCATION\nXYZ University\n\nSKILLS\nPython")
    assert result.category_scores["contact"] < 50
    assert any(i["category"] == "contact" for i in result.issues)


def test_multi_column_pdf_is_detected_as_a_parsing_issue():
    result = _score(text=STRONG_RESUME_TEXT, pdf_bytes=make_multi_column_pdf())
    assert any("column" in i["title"].lower() for i in result.issues if i["category"] == "parsing")


def test_single_column_with_right_aligned_dates_is_not_flagged_multi_column():
    # Regression guard: a right-aligned date sharing a row with body text produces a
    # second x0 cluster too, but must not be confused with a genuine second column.
    result = _score(text=STRONG_RESUME_TEXT, pdf_bytes=make_single_column_pdf_with_right_aligned_dates())
    assert not any("column" in i["title"].lower() for i in result.issues if i["category"] == "parsing")


def test_missing_pdf_bytes_degrades_gracefully_instead_of_crashing():
    result = _score(pdf_bytes=None)
    assert 0 <= result.overall_score <= 100
    assert result.category_scores["parsing"] > 0


def test_recommendations_are_ranked_high_severity_first():
    result = _score(text=MINIMAL_RESUME_TEXT, skills=[], experience=[], projects=[])
    severities = [r["priority"] for r in result.recommendations]
    rank_order = {"high": 0, "medium": 1, "low": 2}
    assert severities == sorted(severities, key=lambda s: rank_order.get(s, 3))


def test_potential_score_is_never_lower_than_overall_score():
    result = _score(text=MINIMAL_RESUME_TEXT, skills=[], experience=[], projects=[])
    if result.potential_score is not None:
        assert result.potential_score >= result.overall_score


def test_excellent_resume_has_no_recoverable_potential_score():
    # A resume with no detectable high/medium issues shouldn't show a padded/redundant
    # "potential score" equal to its own current score.
    result = _score(pdf_bytes=make_pdf_with_text(STRONG_RESUME_TEXT))
    if result.overall_score >= 95:
        assert result.potential_score is None


def test_section_analysis_includes_all_nine_sections():
    result = _score()
    sections = {s["section"] for s in result.section_analysis}
    assert sections == {
        "skills", "education", "summary", "experience", "projects",
        "certifications", "achievements", "languages", "contact",
    }


def test_target_role_is_not_an_accepted_parameter():
    # Structural enforcement, not just convention: score_resume's signature must not be
    # able to accept target_role at all, so it mechanically cannot influence the score.
    import inspect

    from app.services.ats_scoring.engine import score_resume as fn

    assert "target_role" not in inspect.signature(fn).parameters
