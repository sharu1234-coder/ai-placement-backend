from app.services.placement_path_rules import (
    TopicView,
    build_topic_specs,
    compute_effective_status,
    compute_progress,
    compute_topic_priority,
    detect_primary_languages,
    match_domain_tracks,
    pick_next_best_action,
    topic_spec_for,
    track_rank,
)


def test_match_domain_tracks_matches_sql():
    assert "sql" in match_domain_tracks(["SQL"])


def test_match_domain_tracks_matches_free_text_variants():
    matched = match_domain_tracks(["Data Structures & Algorithms (DSA)", "React.js", "AWS Cloud"])
    # DSA itself is a baseline track, not a domain-track trigger -- but react/aws are.
    assert "web_backend" in matched
    assert "cloud_devops" in matched


def test_match_domain_tracks_matches_ml_abbreviation():
    assert "ml_data_science" in match_domain_tracks(["ML"])
    assert "ml_data_science" in match_domain_tracks(["Machine Learning"])


def test_match_domain_tracks_is_case_insensitive():
    assert "sql" in match_domain_tracks(["sql"])
    assert "sql" in match_domain_tracks(["SQL"])


def test_match_domain_tracks_combination():
    matched = match_domain_tracks(["Python", "SQL", "Docker", "Cooking"])
    assert set(matched.keys()) == {"sql", "cloud_devops"}


def test_match_domain_tracks_empty_when_nothing_matches():
    assert match_domain_tracks(["Cooking", "Photography", "Leadership"]) == {}


def test_detect_primary_languages_empty_with_no_evidence():
    assert detect_primary_languages(["Cooking", "SQL"]) == []


def test_detect_primary_languages_caps_at_two():
    langs = detect_primary_languages(["Python", "Java", "JavaScript", "Go"], cap=2)
    assert len(langs) == 2


def test_detect_primary_languages_deterministic_fixed_priority_order():
    # Python outranks Java regardless of resume list order.
    assert detect_primary_languages(["Java", "Python"]) == detect_primary_languages(["Python", "Java"])
    assert detect_primary_languages(["Java", "Python"])[0] == "python"


def test_detect_primary_languages_distinguishes_c_family():
    # "C", "C++", and "C#" must never collapse into the same detected language -- a resume
    # that only lists "C#" must detect "c#", not "c" or "c++" (regression: the tokenizer used
    # to strip "+"/"#" as separators, making all three indistinguishable).
    assert detect_primary_languages(["C#"], cap=1) == ["c#"]
    assert detect_primary_languages(["C++"], cap=1) == ["c++"]
    assert detect_primary_languages(["C"], cap=1) == ["c"]


def test_build_topic_specs_empty_when_no_domain_tracks_matched():
    assert build_topic_specs({}, []) == []


def test_build_topic_specs_includes_baseline_tracks_only_when_domain_matched():
    matched = match_domain_tracks(["SQL"])
    specs = build_topic_specs(matched, [])
    track_keys = {s.track_key for s in specs}
    assert "dsa" in track_keys
    assert "aptitude_reasoning" in track_keys
    assert "practice_assessments" in track_keys
    assert "interview_prep" in track_keys
    assert "final_readiness" in track_keys


def test_build_topic_specs_sql_track_matches_worked_example_order():
    matched = match_domain_tracks(["SQL"])
    specs = [s for s in build_topic_specs(matched, []) if s.track_key == "sql"]
    titles = [s.title for s in specs]
    assert titles == [
        "SQL Basics",
        "SQL Filtering",
        "SQL Aggregation",
        "SQL JOINs",
        "SQL Subqueries",
        "SQL Window Functions",
    ]


def test_build_topic_specs_only_matched_domain_tracks_appear():
    matched = match_domain_tracks(["SQL"])
    specs = build_topic_specs(matched, [])
    track_keys = {s.track_key for s in specs}
    assert "ml_data_science" not in track_keys
    assert "web_backend" not in track_keys


def test_build_topic_specs_is_deterministic():
    matched = match_domain_tracks(["Python", "SQL", "Machine Learning"])
    langs = detect_primary_languages(["Python", "SQL", "Machine Learning"])
    first = build_topic_specs(matched, langs)
    second = build_topic_specs(matched, langs)
    assert [s.topic_key for s in first] == [s.topic_key for s in second]


def test_build_topic_specs_programming_fundamentals_reflects_detected_language():
    matched = match_domain_tracks(["SQL"])
    specs = build_topic_specs(matched, ["python"])
    prog_specs = [s for s in specs if s.track_key.startswith("programming_fundamentals_")]
    assert prog_specs
    assert all("Python" in s.title for s in prog_specs)


def test_compute_effective_status_first_topic_never_locked():
    assert compute_effective_status("not_started", None) == "not_started"


def test_compute_effective_status_locked_when_prerequisite_incomplete():
    assert compute_effective_status("not_started", "in_progress") == "locked"
    assert compute_effective_status("not_started", "not_started") == "locked"


def test_compute_effective_status_unlocked_once_prerequisite_completed():
    assert compute_effective_status("not_started", "completed") == "not_started"
    assert compute_effective_status("in_progress", "completed") == "in_progress"


def _view(topic_key, track_key, order_in_track, status="not_started", prereq_status=None, skill_tag=None):
    return TopicView(
        topic_key=topic_key,
        track_key=track_key,
        stage="core_technical",
        order_in_track=order_in_track,
        title=topic_key,
        skill_tag=skill_tag or topic_key,
        status=status,
        prerequisite_status=prereq_status,
        global_order=order_in_track,
    )


def test_compute_topic_priority_high_when_skill_is_weak():
    view = _view("t1", "sql", 3, skill_tag="SQL JOINs")
    priority = compute_topic_priority(
        view, is_first_incomplete_in_track=False, track_size=6, weak_skill_tags=frozenset({"SQL JOINs"})
    )
    assert priority == "high"


def test_compute_topic_priority_high_for_unevidenced_dsa_worked_example():
    view = _view("dsa_complexity_basics", "dsa", 1)
    priority = compute_topic_priority(view, is_first_incomplete_in_track=True, track_size=8, weak_skill_tags=frozenset())
    assert priority == "high"


def test_compute_topic_priority_high_for_current_focus_in_any_track():
    view = _view("sql_basics", "sql", 1)
    priority = compute_topic_priority(view, is_first_incomplete_in_track=True, track_size=6, weak_skill_tags=frozenset())
    assert priority == "high"


def test_compute_topic_priority_low_for_late_chain_topic():
    view = _view("sql_window_functions", "sql", 6)
    priority = compute_topic_priority(view, is_first_incomplete_in_track=False, track_size=6, weak_skill_tags=frozenset())
    assert priority == "low"


def test_compute_topic_priority_medium_default():
    view = _view("sql_aggregation", "sql", 3)
    priority = compute_topic_priority(view, is_first_incomplete_in_track=False, track_size=6, weak_skill_tags=frozenset())
    assert priority == "medium"


def test_compute_topic_priority_low_when_completed():
    view = _view("sql_basics", "sql", 1, status="completed")
    priority = compute_topic_priority(view, is_first_incomplete_in_track=False, track_size=6, weak_skill_tags=frozenset())
    assert priority == "low"


def test_compute_progress_counts_correctly():
    views = [_view("a", "sql", 1, status="completed"), _view("b", "sql", 2, status="not_started")]
    assert compute_progress(views) == (1, 2)


def test_compute_progress_empty():
    assert compute_progress([]) == (0, 0)


def test_pick_next_best_action_never_returns_locked_topic():
    views = [
        _view("a", "sql", 1, status="not_started", prereq_status=None),
        _view("b", "sql", 2, status="not_started", prereq_status="not_started"),  # locked
    ]
    priorities = {"a": "medium", "b": "high"}
    result = pick_next_best_action(views, priorities)
    assert result is not None
    assert result.topic_key == "a"


def test_pick_next_best_action_none_when_everything_completed():
    views = [_view("a", "sql", 1, status="completed")]
    result = pick_next_best_action(views, {"a": "low"})
    assert result is None


def test_pick_next_best_action_prefers_high_priority():
    views = [
        _view("a", "sql", 1, status="not_started"),
        _view("b", "dsa", 1, status="not_started"),
    ]
    priorities = {"a": "medium", "b": "high"}
    result = pick_next_best_action(views, priorities)
    assert result.topic_key == "b"


def test_track_rank_matches_build_order():
    matched = match_domain_tracks(["Python", "SQL", "Machine Learning", "Java"])
    langs = detect_primary_languages(["Python", "SQL", "Machine Learning", "Java"])
    specs = build_topic_specs(matched, langs)
    seen = []
    for s in specs:
        if s.track_key not in seen:
            seen.append(s.track_key)
    assert seen == sorted(seen, key=track_rank)


def test_topic_spec_for_known_topic():
    spec = topic_spec_for("sql", "sql_joins")
    assert spec is not None
    assert spec.title == "SQL JOINs"
    assert spec.assessment_type == "sql"


def test_topic_spec_for_unknown_topic_returns_none():
    assert topic_spec_for("sql", "not_a_real_topic") is None
    assert topic_spec_for("not_a_real_track", "anything") is None


def test_topic_spec_for_programming_fundamentals():
    spec = topic_spec_for("programming_fundamentals_python", "prog_syntax_control_flow_python")
    assert spec is not None
    assert spec.title == "Python Syntax & Control Flow"


def test_manual_only_stages_have_no_assessment_type():
    matched = match_domain_tracks(["SQL"])
    specs = build_topic_specs(matched, [])
    manual_tracks = {"practice_assessments", "interview_prep", "final_readiness"}
    for spec in specs:
        if spec.track_key in manual_tracks:
            assert spec.assessment_type is None
