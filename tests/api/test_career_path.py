import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.career_path import (
    GeneratedCareerPathExplanations,
    GeneratedRoleCatalog,
    GeneratedRoleOverview,
    GeneratedRolePhase,
    GeneratedRoleSkill,
)
from app.main import app


class FakeAIProvider(AIProvider):
    """Branches on schema, mirroring test_placement_path.py's precedent: skill explanations
    are requested for every generation, and a role-definition call (career_path_role_v2) is
    additionally triggered whenever the selected role isn't in the curated catalog."""

    def __init__(self, fail: bool = False):
        self.fail = fail

    def generate_structured(self, prompt, schema):
        if self.fail:
            raise AIGenerationError("simulated AI failure")
        if schema is GeneratedCareerPathExplanations:
            return GeneratedCareerPathExplanations(explanations=[])
        if schema is GeneratedRoleCatalog:
            return GeneratedRoleCatalog(
                overview=GeneratedRoleOverview(
                    short_explanation="A fake AI-generated role overview.",
                    responsibilities=["Do the work"],
                    core_skills=["Skill A", "Skill B"],
                    tools=["Tool A"],
                    difficulty="moderate",
                    entry_level_expectations="Some baseline expectation.",
                    interview_focus=["Fundamentals"],
                    portfolio_expectations="One solid project.",
                ),
                phases=[
                    GeneratedRolePhase(
                        phase_key="foundations",
                        skills=[GeneratedRoleSkill(skill_key="skill_a", title="Skill A", level="beginner")],
                    )
                ],
            )
        raise AssertionError(f"Unexpected schema requested: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "career@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "career@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def other_auth_headers(client):
    client.post("/auth/signup", json={"email": "career2@example.com", "password": "supersecret123"})
    token = client.post("/auth/login", json={"email": "career2@example.com", "password": "supersecret123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def use_fake_ai():
    provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


@pytest.fixture()
def use_failing_ai():
    provider = FakeAIProvider(fail=True)
    app.dependency_overrides[get_ai_provider] = lambda: provider
    yield provider
    app.dependency_overrides.pop(get_ai_provider, None)


def _data_analyst_role_id(client, auth_headers):
    roles = client.get("/roles", headers=auth_headers).json()
    role = next(r for r in roles if r["name"] == "Data Analyst")
    return role["id"]


def test_generate_by_custom_role_never_says_not_supported(client, auth_headers, use_failing_ai):
    """Even when the AI call fails entirely, a genuinely novel custom role must still 201
    with real, non-empty content -- never a "role not supported" rejection."""
    resp = client.post("/career-path/generate", headers=auth_headers, json={"custom_role": "Blockchain Developer"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["generated_via"] == "fallback"
    assert sum(len(p["skill_items"]) for p in body["phases"]) > 0


def test_generate_by_role_id_returns_curated_content(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    resp = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id})
    assert resp.status_code == 201
    body = resp.json()
    assert body["role_family_key"] == "data_analyst"
    assert body["generated_via"] == "catalog"
    assert body["is_primary"] is True
    assert len(body["phases"]) == 5
    assert body["job_readiness"]["overall_percent"] == 0
    assert body["resume_connection"] is None


def test_generate_rejects_both_role_id_and_custom_role(client, auth_headers, use_fake_ai):
    resp = client.post(
        "/career-path/generate",
        headers=auth_headers,
        json={"role_id": 1, "custom_role": "Something"},
    )
    assert resp.status_code == 422


def test_generate_rejects_neither_role_id_nor_custom_role(client, auth_headers, use_fake_ai):
    resp = client.post("/career-path/generate", headers=auth_headers, json={})
    assert resp.status_code == 422


def test_generate_rejects_whitespace_only_custom_role(client, auth_headers, use_fake_ai):
    """A whitespace-only custom_role must fail cleanly here, not strip down to an empty
    role name and waste an AI call on a nonsensical prompt at generation time."""
    resp = client.post("/career-path/generate", headers=auth_headers, json={"custom_role": "   "})
    assert resp.status_code == 422


def test_generate_unknown_role_id_returns_404(client, auth_headers, use_fake_ai):
    resp = client.post("/career-path/generate", headers=auth_headers, json={"role_id": 999999})
    assert resp.status_code == 404


def test_generate_requires_auth(client):
    resp = client.post("/career-path/generate", json={"custom_role": "Data Analyst"})
    assert resp.status_code == 401


def test_selecting_second_role_keeps_first_saved_not_deleted(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    first = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()
    client.post("/career-path/generate", headers=auth_headers, json={"custom_role": "Petroleum Engineer"})

    all_paths = client.get("/career-path/all", headers=auth_headers).json()
    assert len(all_paths) == 2

    first_again = client.get(f"/career-path/{first['id']}", headers=auth_headers).json()
    assert first_again["is_primary"] is False


def test_set_current_switches_primary(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    first = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()
    client.post("/career-path/generate", headers=auth_headers, json={"custom_role": "Petroleum Engineer"})

    resp = client.put(f"/career-path/{first['id']}/set-current", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["is_primary"] is True

    current = client.get("/career-path/current", headers=auth_headers).json()
    assert current["id"] == first["id"]


def test_get_current_is_null_before_any_selection(client, auth_headers):
    resp = client.get("/career-path/current", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() is None


def test_mark_skill_item_and_project_complete(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    path = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()

    item_id = path["phases"][0]["skill_items"][0]["id"]
    resp = client.patch(f"/career-path/skill-items/{item_id}", headers=auth_headers, json={"status": "completed"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"
    assert resp.json()["completed_via"] == "manual"

    project_id = next(p["projects"][0]["id"] for p in path["phases"] if p["projects"])
    resp = client.patch(f"/career-path/projects/{project_id}", headers=auth_headers, json={"status": "completed"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_practice_handoff_returns_reusable_blueprint(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    path = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()
    item_id = path["phases"][0]["skill_items"][0]["id"]

    resp = client.post(f"/career-path/skill-items/{item_id}/practice", headers=auth_headers)
    assert resp.status_code == 200
    blueprint_id = resp.json()["blueprint_id"]

    # Hands off entirely to the existing, unmodified assessment endpoint.
    resp2 = client.post("/assessments", headers=auth_headers, json={"blueprint_id": blueprint_id})
    assert resp2.status_code in (200, 201)


def test_cross_user_isolation_get_and_mutate(client, auth_headers, other_auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    path = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()

    resp = client.get(f"/career-path/{path['id']}", headers=other_auth_headers)
    assert resp.status_code == 404

    item_id = path["phases"][0]["skill_items"][0]["id"]
    resp = client.patch(f"/career-path/skill-items/{item_id}", headers=other_auth_headers, json={"status": "completed"})
    assert resp.status_code == 404

    resp = client.put(f"/career-path/{path['id']}/set-current", headers=other_auth_headers)
    assert resp.status_code == 404


def test_role_catalog_and_categories_are_public_reads(client, use_fake_ai):
    resp = client.get("/career-path/role-catalog")
    assert resp.status_code == 200
    families = resp.json()
    assert any(f["family_key"] == "data_analyst" for f in families)

    filtered = client.get("/career-path/role-catalog", params={"category": "Design"}).json()
    assert all(f["category"] == "Design" for f in filtered)

    categories = client.get("/career-path/role-categories").json()
    assert "Technology" in categories


def test_generate_rate_limited_after_ten_per_hour(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    for _ in range(10):
        resp = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id})
        assert resp.status_code == 201
    resp = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id})
    assert resp.status_code == 429


def test_apply_jd_adds_missing_jd_skills_without_duplicating(client, auth_headers, use_fake_ai):
    from app.api.deps import get_db
    from app.models.job_description import JobDescription
    from app.models.requirement import Requirement

    role_id = _data_analyst_role_id(client, auth_headers)
    path = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()
    existing_titles = {i["title"] for p in path["phases"] for i in p["skill_items"]}
    already_present_skill = next(iter(existing_titles))

    db = next(app.dependency_overrides[get_db]())
    jd = JobDescription(profile_id=None, raw_text="A JD.", source="paste", role_title="Data Analyst")
    db.add(jd)
    db.commit()
    db.refresh(jd)
    # profile_id is nullable on JobDescription -- set it to this test's actual profile so the
    # ownership check in apply_jd passes, matching jd_service's own ownership convention.
    from app.models.profile import StudentProfile
    from app.models.user import User

    user = db.query(User).filter(User.email == "career@example.com").first()
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    jd.profile_id = profile.id
    db.add(Requirement(jd_id=jd.id, category="skill", value="Brand New JD Skill", is_explicit=True, importance="required"))
    db.add(Requirement(jd_id=jd.id, category="skill", value=already_present_skill, is_explicit=True, importance="required"))
    db.commit()
    jd_id = jd.id
    db.close()

    resp = client.post(f"/career-path/{path['id']}/apply-jd/{jd_id}", headers=auth_headers)
    assert resp.status_code == 200
    updated = resp.json()
    new_titles = {i["title"] for p in updated["phases"] for i in p["skill_items"]}
    assert "Brand New JD Skill" in new_titles
    # Already-represented skill must never be duplicated.
    assert sum(1 for p in updated["phases"] for i in p["skill_items"] if i["title"] == already_present_skill) == 1


def test_apply_jd_unknown_jd_returns_404(client, auth_headers, use_fake_ai):
    role_id = _data_analyst_role_id(client, auth_headers)
    path = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()
    resp = client.post(f"/career-path/{path['id']}/apply-jd/999999", headers=auth_headers)
    assert resp.status_code == 404


def test_skill_gap_analysis_never_fabricates_when_resume_exists(client, auth_headers, use_fake_ai):
    from app.api.deps import get_db
    from app.models.profile import StudentProfile
    from app.models.resume import Resume
    from app.models.resume_analysis import ResumeAnalysis
    from app.models.user import User

    role_id = _data_analyst_role_id(client, auth_headers)

    db = next(app.dependency_overrides[get_db]())
    user = db.query(User).filter(User.email == "career@example.com").first()
    profile = db.query(StudentProfile).filter(StudentProfile.user_id == user.id).first()
    if profile is None:
        profile = StudentProfile(user_id=user.id)
        db.add(profile)
        db.commit()
        db.refresh(profile)
    resume = Resume(profile_id=profile.id, file_path="x.pdf", raw_text="text", parse_status="completed")
    db.add(resume)
    db.commit()
    db.refresh(resume)
    db.add(
        ResumeAnalysis(
            resume_id=resume.id,
            extracted_skills=["sql", "excel"],
            extracted_education=[],
            extracted_experience=[],
            extracted_projects=[],
            extracted_certifications=[],
            ats_score=70.0,
            weak_sections=[],
            extraction_confidence=0.9,
        )
    )
    db.commit()
    db.close()

    path = client.post("/career-path/generate", headers=auth_headers, json={"role_id": role_id}).json()
    strong_skills = {g["skill"].lower() for g in path["skill_gaps"] if g["status"] == "strong"}
    assert "sql" in strong_skills
    # A skill never mentioned in the resume must never be reported as strong.
    assert "power bi or tableau" not in strong_skills
