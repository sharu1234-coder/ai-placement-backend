from app.services.skill_matching import skill_present


def test_exact_match():
    assert skill_present("SQL", ["sql", "python"])


def test_case_insensitive_match():
    assert skill_present("Sql", ["sql"])


def test_substring_match_either_direction():
    assert skill_present("React", ["react.js", "node"])
    assert skill_present("React.js", ["react"])


def test_no_match():
    assert not skill_present("Kubernetes", ["python", "sql"])


def test_empty_resume_skills():
    assert not skill_present("SQL", [])
