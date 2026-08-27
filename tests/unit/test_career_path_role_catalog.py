from app.services.career_path.role_catalog import (
    CATEGORY_LIST,
    PHASE_ORDER,
    ROLE_FAMILIES,
    get_role_family,
    resolve_role_family,
)


def test_every_family_has_five_phases_and_at_least_one_project():
    for family in ROLE_FAMILIES.values():
        assert set(family.phase_skill_templates.keys()).issubset(set(PHASE_ORDER))
        assert len(family.project_templates) >= 3
        assert len(family.core_skills) >= 5


def test_resolve_exact_display_names():
    for family in ROLE_FAMILIES.values():
        assert resolve_role_family(family.display_name) == family.family_key


def test_resolve_common_aliases():
    assert resolve_role_family("ML Engineer") == "ml_engineer"
    assert resolve_role_family("Machine Learning Engineer") == "ml_engineer"
    assert resolve_role_family("Data Scientist") == "data_scientist"
    assert resolve_role_family("UI/UX Designer") == "ui_ux_designer"
    assert resolve_role_family("Frontend Developer") == "sde"
    assert resolve_role_family("Backend Engineer") == "sde"
    assert resolve_role_family("DevOps Engineer") == "devops_engineer"
    assert resolve_role_family("Cloud Engineer") == "devops_engineer"
    assert resolve_role_family("HR Recruiter") == "hr_recruiter"


def test_resolve_strips_qualifier_words():
    assert resolve_role_family("Senior Software Engineer") == "sde"
    assert resolve_role_family("Junior Data Analyst") == "data_analyst"
    assert resolve_role_family("Data Analyst Intern") == "data_analyst"


def test_business_analyst_does_not_false_positive_match_data_analyst():
    """Regression test: an earlier bidirectional-subset matching algorithm incorrectly
    resolved "Business Analyst" (tokens {business, analyst}) to Data Analyst, because that
    token set is a subset of Data Analyst's "business intelligence analyst" alias (tokens
    {business, intelligence, analyst}). Exact-set matching after qualifier-stripping fixes
    this whole class of false positive."""
    assert resolve_role_family("Business Analyst") == "business_analyst"
    assert resolve_role_family("Business Intelligence Analyst") == "data_analyst"
    assert resolve_role_family("Business Systems Analyst") == "business_analyst"


def test_resolve_unrecognized_role_returns_none():
    assert resolve_role_family("Blockchain Developer") is None
    assert resolve_role_family("Mechanical Engineer") is None
    assert resolve_role_family("") is None
    assert resolve_role_family("   ") is None


def test_get_role_family_returns_none_for_unknown_key():
    assert get_role_family("nonexistent") is None
    assert get_role_family("sde") is not None


def test_category_list_is_the_fixed_thirteen():
    assert len(CATEGORY_LIST) == 13
    assert "Technology" in CATEGORY_LIST
    assert "Other" in CATEGORY_LIST


def test_every_family_category_is_in_category_list():
    for family in ROLE_FAMILIES.values():
        assert family.category in CATEGORY_LIST
