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


def test_short_skill_name_does_not_false_positive_on_substring():
    # Regression: a plain `"r" in "docker"` substring check used to return True here.
    assert not skill_present("R", ["docker", "python"])


def test_empty_skill_name_never_matches():
    assert not skill_present("", ["python", "sql"])


def test_empty_resume_skill_entry_never_matches():
    assert not skill_present("SQL", ["", "python"])
