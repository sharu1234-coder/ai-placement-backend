import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.provider import AIGenerationError, AIProvider
from app.database.base import Base
from app.models.career_path import CareerPath
from app.models.profile import StudentProfile
from app.services import github_verification_service
from app.services.project_builder import service as svc
from app.services.project_builder.stages import FALLBACK_GUIDANCE, STAGE_ORDER


class FailingAIProvider(AIProvider):
    def generate_structured(self, prompt, schema):
        raise AIGenerationError("no AI in this test")

    def generate_text(self, prompt):
        return "fake"


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def ai():
    return FailingAIProvider()


_next_user_id = [1]


def _profile(db) -> StudentProfile:
    profile = StudentProfile(user_id=_next_user_id[0])
    _next_user_id[0] += 1
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _with_role(db, profile) -> StudentProfile:
    path = CareerPath(
        profile_id=profile.id,
        role_family_key="data_engineer",
        display_role_name="Data Engineer",
        is_primary=True,
        generated_via="catalog",
        role_overview={"core_skills": ["Kafka", "SQL"], "tools": []},
    )
    db.add(path)
    db.commit()
    return profile


def test_create_build_with_role_context_snapshots_role_and_creates_all_stages(db, ai):
    profile = _with_role(db, _profile(db))
    build = svc.create_build(db, profile, "Realtime Pipeline", "Kafka, Flink", ai)

    assert build.target_role_name == "Data Engineer"
    assert build.status == "not_started"
    assert len(build.stages) == 10
    assert {s.stage_key for s in build.stages} == set(STAGE_ORDER)
    assert build.stages[0].order_index == 0
    assert all(s.status == "not_started" for s in build.stages)


def test_create_build_without_role_context_still_works(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Portfolio Project", None, ai)

    assert build.target_role_name is None
    assert len(build.stages) == 10


def test_create_build_ai_failure_uses_fallback_guidance_for_every_stage(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Portfolio Project", None, ai)

    for stage in build.stages:
        assert stage.guidance_text == FALLBACK_GUIDANCE[stage.stage_key]


def test_list_builds_orders_most_recent_first_and_is_isolated_per_profile(db, ai):
    profile = _profile(db)
    other = _profile(db)
    svc.create_build(db, profile, "First", None, ai)
    second = svc.create_build(db, profile, "Second", None, ai)
    svc.create_build(db, other, "Someone else's build", None, ai)

    mine = svc.list_builds(db, profile.id)
    assert [b.title for b in mine] == ["Second", "First"]
    assert mine[0].id == second.id
    assert len(svc.list_builds(db, other.id)) == 1


def test_get_build_raises_not_found_for_another_profile(db, ai):
    profile = _profile(db)
    other = _profile(db)
    build = svc.create_build(db, profile, "Mine", None, ai)

    with pytest.raises(svc.ProjectBuildNotFoundError):
        svc.get_build(db, other.id, build.id)


def test_first_stage_starts_unlocked(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    stage = svc.start_stage(db, profile, build.id, STAGE_ORDER[0])
    assert stage.status == "in_progress"
    reloaded = svc.get_build(db, profile.id, build.id)
    assert reloaded.status == "in_progress"


def test_second_stage_is_locked_until_first_is_completed(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    with pytest.raises(svc.StageLockedError):
        svc.start_stage(db, profile, build.id, STAGE_ORDER[1])
    with pytest.raises(svc.StageLockedError):
        svc.complete_stage(db, profile, build.id, STAGE_ORDER[1], None, ai)


def test_start_stage_is_idempotent(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    first = svc.start_stage(db, profile, build.id, STAGE_ORDER[0])
    second = svc.start_stage(db, profile, build.id, STAGE_ORDER[0])
    assert first.started_at == second.started_at


def test_complete_stage_unlocks_the_next_one(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    svc.complete_stage(db, profile, build.id, STAGE_ORDER[0], "Solved it.", ai)
    stage = svc.start_stage(db, profile, build.id, STAGE_ORDER[1])
    assert stage.status == "in_progress"


def test_duplicate_completion_does_not_change_notes_or_completed_at(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    first = svc.complete_stage(db, profile, build.id, STAGE_ORDER[0], "Original notes.", ai)
    second = svc.complete_stage(db, profile, build.id, STAGE_ORDER[0], "Different notes -- should be ignored.", ai)

    assert second.completed_at == first.completed_at
    assert second.notes == "Original notes."


def test_unknown_stage_key_raises_stage_not_found(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    with pytest.raises(svc.StageNotFoundError):
        svc.start_stage(db, profile, build.id, "not_a_real_stage")


def test_completing_every_stage_in_order_marks_build_completed_and_generates_proof(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Realtime Pipeline", "Kafka, Flink", ai)

    for stage_key in STAGE_ORDER:
        svc.complete_stage(db, profile, build.id, stage_key, f"Notes for {stage_key}", ai)

    reloaded = svc.get_build(db, profile.id, build.id)
    assert reloaded.status == "completed"
    assert reloaded.completed_at is not None
    assert all(s.status == "completed" for s in reloaded.stages)

    proof = svc.get_proof(db, profile.id, build.id)
    assert proof.resume_bullet
    assert proof.readme_markdown
    assert len(proof.defense_questions) > 0


def test_get_proof_raises_not_ready_before_completion(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Project", None, ai)

    with pytest.raises(svc.ProofNotReadyError):
        svc.get_proof(db, profile.id, build.id)

    svc.complete_stage(db, profile, build.id, STAGE_ORDER[0], None, ai)
    with pytest.raises(svc.ProofNotReadyError):
        svc.get_proof(db, profile.id, build.id)


def _repo_response(status_code=200, **overrides):
    body = {"language": "Python", "stargazers_count": 5, "pushed_at": "2026-08-01T12:00:00Z", **overrides}
    return httpx.Response(status_code, json=body, request=httpx.Request("GET", "https://api.github.com/repos/x/y"))


def test_check_github_persists_evidence_on_the_build(db, ai, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        if url.endswith("/readme"):
            return httpx.Response(404, request=httpx.Request("GET", url))
        return _repo_response()

    monkeypatch.setattr(httpx, "get", fake_get)

    profile = _profile(db)
    build = svc.create_build(db, profile, "Realtime Pipeline", "Python", ai)

    evidence = svc.check_github(db, profile, build.id, "https://github.com/octocat/Hello-World")
    assert evidence.repo_exists is True
    assert evidence.primary_language == "Python"

    reloaded = svc.get_build(db, profile.id, build.id)
    assert reloaded.github_evidence is not None
    assert reloaded.github_evidence.repo_exists is True


def test_check_github_raises_not_found_for_another_profiles_build(db, ai, monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response())
    profile = _profile(db)
    other = _profile(db)
    build = svc.create_build(db, profile, "Mine", None, ai)

    with pytest.raises(svc.ProjectBuildNotFoundError):
        svc.check_github(db, other, build.id, "https://github.com/octocat/Hello-World")


def test_check_github_propagates_invalid_url_error(db, ai):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Mine", None, ai)

    with pytest.raises(github_verification_service.InvalidRepoUrlError):
        svc.check_github(db, profile, build.id, "not a url")


def test_check_github_propagates_api_error_and_does_not_overwrite_prior_evidence(db, ai, monkeypatch):
    profile = _profile(db)
    build = svc.create_build(db, profile, "Mine", "Python", ai)

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response())
    svc.check_github(db, profile, build.id, "https://github.com/octocat/Hello-World")

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _repo_response(status_code=403))
    with pytest.raises(github_verification_service.GitHubApiError):
        svc.check_github(db, profile, build.id, "https://github.com/octocat/Hello-World")

    reloaded = svc.get_build(db, profile.id, build.id)
    assert reloaded.github_evidence is not None
    assert reloaded.github_evidence.repo_exists is True
