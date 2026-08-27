from app.services.career_path.role_catalog import PHASE_ORDER, get_role_family
from app.services.career_path.rules import build_phase_specs, compute_phase_status, compute_progress


def test_build_phase_specs_is_deterministic():
    family = get_role_family("sde")
    specs1 = build_phase_specs(family)
    specs2 = build_phase_specs(family)
    assert [s.phase_key for s in specs1] == [s.phase_key for s in specs2]
    for s1, s2 in zip(specs1, specs2):
        assert [i.skill_key for i in s1.skill_items] == [i.skill_key for i in s2.skill_items]
        assert [p.project_key for p in s1.projects] == [p.project_key for p in s2.projects]


def test_build_phase_specs_covers_all_five_phases_in_order():
    family = get_role_family("data_analyst")
    specs = build_phase_specs(family)
    assert [s.phase_key for s in specs] == PHASE_ORDER
    assert [s.order_index for s in specs] == list(range(5))


def test_only_projects_phase_has_projects():
    family = get_role_family("ui_ux_designer")
    specs = build_phase_specs(family)
    for spec in specs:
        if spec.phase_key == "projects":
            assert len(spec.projects) >= 3
        else:
            assert spec.projects == []


def test_compute_phase_status_all_combinations():
    assert compute_phase_status([], []) == "not_started"
    assert compute_phase_status(["not_started", "not_started"], []) == "not_started"
    assert compute_phase_status(["completed", "completed"], []) == "completed"
    assert compute_phase_status(["completed", "not_started"], []) == "in_progress"
    assert compute_phase_status(["in_progress"], []) == "in_progress"
    assert compute_phase_status(["completed"], ["not_started"]) == "in_progress"
    assert compute_phase_status(["completed"], ["completed"]) == "completed"


def test_compute_progress_counts_completed_across_items_and_projects():
    assert compute_progress([], []) == (0, 0)
    assert compute_progress(["completed", "not_started"], ["completed"]) == (2, 3)
    assert compute_progress(["not_started"], ["not_started"]) == (0, 2)
