from app.services.matching_service import _skill_present


def test_exact_match():
    assert _skill_present("SQL", ["sql", "python"])


def test_case_insensitive_match():
    assert _skill_present("Sql", ["sql"])


def test_substring_match_either_direction():
    assert _skill_present("React", ["react.js", "node"])
    assert _skill_present("React.js", ["react"])


def test_no_match():
    assert not _skill_present("Kubernetes", ["python", "sql"])


def test_empty_resume_skills():
    assert not _skill_present("SQL", [])
