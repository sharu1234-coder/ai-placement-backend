"""The skill explanation is written after the response, not on the way to it (F12-05).

Measured on the unchanged backend through the real route, against the local model: a curated
role's generation took 135.7-183.4 seconds, of which 99.9-100% was the skill-explanation AI call.
The structural work -- resolving the role family, building the phase specs, reading and writing
the rows, rendering the response -- totalled 57-117 ms. Nothing computes from the text: the column,
the response field and the Android DTO are all nullable, the one screen that shows it already has
a fallback sentence, and no other service reads CareerPathSkillItem at all.

So select_role() now writes the deterministic sentence that generate_skill_explanations has always
fallen back to, and apply_skill_explanations replaces it with the AI's afterwards. These tests pin
the two halves of that: the path the student gets is complete and usable without the AI, and the
AI's failure can never take it away.

TestClient runs background tasks before returning from the request, so an HTTP test here sees the
deferred pass already done; the service-level tests call it explicitly, which is also how they
model a process that died before it ran.
"""

import threading

import pytest

from app.ai.provider import AIGenerationError, AIProvider, get_ai_provider
from app.ai.schemas.career_path import (
    GeneratedCareerPathExplanations,
    GeneratedRoleCatalog,
    GeneratedRoleOverview,
    GeneratedRolePhase,
    GeneratedRoleSkill,
    GeneratedSkillExplanation,
)
from app.api.deps import get_db
from app.main import app
from app.models.career_path import CareerPath
from app.models.career_path_skill_item import CareerPathSkillItem
from app.models.profile import StudentProfile
from app.services.career_path import service as svc

CURATED_ROLE = "Data Analyst"
CUSTOM_ROLE = "Quantum Data Wrangler"
TEMPLATE_PREFIX = "Builds your"
AI_SENTENCE = "Written by the model for this skill."


class CountingProvider(AIProvider):
    """Counts what select_role costs, and can be told to fail the way the real one does."""

    def __init__(self, explanations_raise: bool = False):
        self.explanations_raise = explanations_raise
        self.lock = threading.Lock()
        self.definition_calls = 0
        self.explanation_calls = 0

    def generate_structured(self, prompt, schema):
        if schema is GeneratedRoleCatalog:
            with self.lock:
                self.definition_calls += 1
            return GeneratedRoleCatalog(
                overview=GeneratedRoleOverview(
                    short_explanation="A generated role.",
                    responsibilities=["Do the work"],
                    core_skills=["Skill A"],
                    tools=["Tool A"],
                    difficulty="moderate",
                    entry_level_expectations="Baseline.",
                    interview_focus=["Fundamentals"],
                    portfolio_expectations="One project.",
                ),
                phases=[
                    GeneratedRolePhase(
                        phase_key="foundations",
                        skills=[GeneratedRoleSkill(skill_key="skill_a", title="Skill A", level="beginner")],
                    )
                ],
            )
        if schema is GeneratedCareerPathExplanations:
            with self.lock:
                self.explanation_calls += 1
            if self.explanations_raise:
                raise AIGenerationError("the model is unavailable")
            keys = [line.split(" | ")[0].strip() for line in prompt.splitlines() if line.count(" | ") == 3]
            return GeneratedCareerPathExplanations(
                explanations=[GeneratedSkillExplanation(skill_key=key, why_it_matters=AI_SENTENCE) for key in keys]
            )
        raise AssertionError(f"Unexpected schema requested: {schema}")

    def generate_text(self, prompt):
        return "fake text"


@pytest.fixture()
def auth_headers(client):
    client.post("/auth/signup", json={"email": "defer@example.com", "password": "supersecret123"})
    token = client.post(
        "/auth/login", json={"email": "defer@example.com", "password": "supersecret123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def db_session(client, auth_headers):
    """The same session the route would get, on the same engine -- which is what the deferred
    pass binds to, so a service-level test observes exactly what the route's would."""
    return next(app.dependency_overrides[get_db]())


@pytest.fixture()
def profile(client, auth_headers, db_session):
    client.get("/users/me", headers=auth_headers)  # creates the profile, as the app's first call does
    db_session.expire_all()
    return db_session.query(StudentProfile.id).scalar()


@pytest.fixture()
def use_deferred_ai():
    """Installs a CountingProvider for the route and removes it afterwards."""
    def _use(explanations_raise: bool = False):
        provider = CountingProvider(explanations_raise=explanations_raise)
        app.dependency_overrides[get_ai_provider] = lambda: provider
        return provider

    yield _use
    app.dependency_overrides.pop(get_ai_provider, None)


def _items(db, path_id):
    return (
        db.query(CareerPathSkillItem)
        .filter(CareerPathSkillItem.career_path_id == path_id)
        .order_by(CareerPathSkillItem.order_index, CareerPathSkillItem.id)
        .all()
    )


def _factory(db):
    return svc.explanation_session_factory(db)


# --- the structural path, without any explanation AI -------------------------------------


def test_a_curated_role_is_generated_with_no_ai_call_at_all(db_session, profile):
    provider = CountingProvider()

    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)

    # The whole of F12-05: the student's path exists and nothing waited on the model.
    assert provider.explanation_calls == 0
    assert provider.definition_calls == 0
    assert result.id is not None
    assert result.is_primary is True
    assert sum(len(p.skill_items) for p in result.phases) > 0


def test_the_returned_path_is_structurally_complete(db_session, profile):
    result = svc.select_role(db_session, profile, CountingProvider(), custom_role=CURATED_ROLE)

    assert result.phases, "a path with no phases is not a usable path"
    items = [i for p in result.phases for i in p.skill_items]
    assert items
    assert all(i.skill_key for i in items), "stable skill keys are what progress is carried by"
    assert len({i.skill_key for i in items}) == len(items), "skill keys are unique within a path"
    assert all(i.status == "not_started" for i in items)
    # Every sentence is already there -- the field is never empty, only later improved.
    assert all(i.why_it_matters for i in items)
    assert all(i.why_it_matters.startswith(TEMPLATE_PREFIX) for i in items)


def test_a_custom_role_still_pays_for_its_structure_but_not_its_wording(db_session, profile):
    provider = CountingProvider()

    svc.select_role(db_session, profile, provider, custom_role=CUSTOM_ROLE)

    # The role definition is structural -- it produces the phases and the skill keys -- so it
    # stays on the critical path. Only the explanatory call moved off it.
    assert provider.definition_calls == 1
    assert provider.explanation_calls == 0


# --- the deferred pass ---------------------------------------------------------------------


def test_the_deferred_pass_replaces_the_template_with_the_ai_sentence(db_session, profile):
    provider = CountingProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)
    assert all(i.why_it_matters.startswith(TEMPLATE_PREFIX) for p in result.phases for i in p.skill_items)

    assert svc.apply_skill_explanations(result.id, provider, _factory(db_session)) is True

    db_session.expire_all()
    assert provider.explanation_calls == 1
    assert [i.why_it_matters for i in _items(db_session, result.id)] == [AI_SENTENCE] * len(
        _items(db_session, result.id)
    )


def test_a_failed_explanation_leaves_the_path_intact_and_usable(db_session, profile):
    provider = CountingProvider(explanations_raise=True)
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)

    # generate_skill_explanations swallows the AI failure and returns the templates, so the pass
    # reports that it ran; what matters is that nothing was lost.
    svc.apply_skill_explanations(result.id, provider, _factory(db_session))

    db_session.expire_all()
    path = db_session.query(CareerPath).filter(CareerPath.id == result.id).first()
    assert path is not None, "a failed explanation must never remove the path"
    assert path.is_primary is True
    items = _items(db_session, result.id)
    assert items, "a failed explanation must never remove the skills"
    assert all(i.why_it_matters.startswith(TEMPLATE_PREFIX) for i in items)


def test_an_unexpected_failure_in_the_pass_is_contained(db_session, profile):
    """The AI's own failures are already handled inside generate_skill_explanations, which
    returns templates rather than raising. This is the other class: a provider that breaks in a
    way that function does not catch. It runs after the response, so there is nobody left to tell
    -- it has to be logged and dropped, not raised into the background task."""
    class BrokenProvider(CountingProvider):
        def generate_structured(self, prompt, schema):
            if schema is GeneratedCareerPathExplanations:
                raise RuntimeError("the provider itself is broken")
            return super().generate_structured(prompt, schema)

    provider = BrokenProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)

    assert svc.apply_skill_explanations(result.id, provider, _factory(db_session)) is False

    db_session.expire_all()
    assert db_session.query(CareerPath).filter(CareerPath.id == result.id).first() is not None
    items = _items(db_session, result.id)
    assert items and all(i.why_it_matters.startswith(TEMPLATE_PREFIX) for i in items)


def test_an_unavailable_path_is_not_an_error(db_session, profile):
    # What a deferred pass finds if the account was deleted between the response and its turn.
    assert svc.apply_skill_explanations(999_999, CountingProvider(), _factory(db_session)) is False


def test_a_process_that_dies_before_the_pass_leaves_a_complete_path(db_session, profile):
    result = svc.select_role(db_session, profile, CountingProvider(), custom_role=CURATED_ROLE)

    # No deferred pass ever runs -- the server restarted. Nothing about the path is provisional.
    db_session.expire_all()
    items = _items(db_session, result.id)
    assert items
    assert all(i.why_it_matters for i in items)
    assert db_session.query(CareerPath).filter(CareerPath.id == result.id).first().is_primary is True


def test_a_second_pass_does_not_pay_for_the_same_sentences_twice(db_session, profile):
    provider = CountingProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)
    svc.apply_skill_explanations(result.id, provider, _factory(db_session))
    assert provider.explanation_calls == 1

    assert svc.apply_skill_explanations(result.id, provider, _factory(db_session)) is False

    assert provider.explanation_calls == 1, "a path already explained is left alone"


def test_two_passes_leave_one_row_and_one_sentence_per_skill(db_session, profile):
    provider = CountingProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)

    svc.apply_skill_explanations(result.id, provider, _factory(db_session))
    svc.apply_skill_explanations(result.id, provider, _factory(db_session))

    db_session.expire_all()
    assert db_session.query(CareerPath).filter(CareerPath.profile_id == profile).count() == 1
    items = _items(db_session, result.id)
    assert len({i.skill_key for i in items}) == len(items), "no skill gained a second row"
    assert all(i.why_it_matters == AI_SENTENCE for i in items), "and none has two versions"


def test_progress_survives_the_deferred_pass(db_session, profile):
    provider = CountingProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)
    item = _items(db_session, result.id)[0]
    item.status = "completed"
    item.completed_via = "assessment"
    db_session.commit()

    svc.apply_skill_explanations(result.id, provider, _factory(db_session))

    db_session.expire_all()
    after = db_session.query(CareerPathSkillItem).filter(CareerPathSkillItem.id == item.id).first()
    assert (after.status, after.completed_via) == ("completed", "assessment")
    assert after.why_it_matters == AI_SENTENCE, "the wording still improved"


def test_the_pass_writes_only_the_wording(db_session, profile):
    provider = CountingProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)
    path = db_session.query(CareerPath).filter(CareerPath.id == result.id).first()
    before = (path.role_family_key, path.display_role_name, path.is_primary, path.generated_via)
    item_keys = sorted(i.skill_key for i in _items(db_session, result.id))

    svc.apply_skill_explanations(result.id, provider, _factory(db_session))

    db_session.expire_all()
    path = db_session.query(CareerPath).filter(CareerPath.id == result.id).first()
    assert (path.role_family_key, path.display_role_name, path.is_primary, path.generated_via) == before
    assert sorted(i.skill_key for i in _items(db_session, result.id)) == item_keys


def test_a_reselection_after_the_pass_is_explained_again(db_session, profile):
    """A re-selection rebuilds the children, which rewrites every sentence back to the template.
    The next pass has to notice that and explain them again -- matching on (career_path_id,
    skill_key), so it writes to whichever rows now hold those keys, adding and losing nothing."""
    provider = CountingProvider()
    result = svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)
    svc.apply_skill_explanations(result.id, provider, _factory(db_session))
    db_session.expire_all()
    keys_before = sorted(i.skill_key for i in _items(db_session, result.id))
    assert all(i.why_it_matters == AI_SENTENCE for i in _items(db_session, result.id))

    svc.select_role(db_session, profile, provider, custom_role=CURATED_ROLE)

    db_session.expire_all()
    rebuilt = _items(db_session, result.id)
    assert all(i.why_it_matters.startswith(TEMPLATE_PREFIX) for i in rebuilt), (
        "precondition: the re-selection really did rebuild the children"
    )

    assert svc.apply_skill_explanations(result.id, provider, _factory(db_session)) is True

    db_session.expire_all()
    items = _items(db_session, result.id)
    assert sorted(i.skill_key for i in items) == keys_before, "no row was added or lost"
    assert all(i.why_it_matters == AI_SENTENCE for i in items)


# --- through the route ----------------------------------------------------------------------


def test_the_route_returns_a_complete_path_and_then_improves_its_wording(client, auth_headers, use_deferred_ai):
    provider = use_deferred_ai()

    response = client.post("/career-path/generate", json={"custom_role": CURATED_ROLE}, headers=auth_headers)

    assert response.status_code == 201
    body = response.json()
    assert sum(len(p["skill_items"]) for p in body["phases"]) > 0
    assert all(i["why_it_matters"] for p in body["phases"] for i in p["skill_items"])

    # TestClient has already run the background task by now.
    current = client.get("/career-path/current", headers=auth_headers).json()
    assert all(i["why_it_matters"] == AI_SENTENCE for p in current["phases"] for i in p["skill_items"])
    assert provider.explanation_calls == 1


def test_a_failing_explanation_still_returns_201(client, auth_headers, use_deferred_ai):
    use_deferred_ai(explanations_raise=True)

    response = client.post("/career-path/generate", json={"custom_role": CURATED_ROLE}, headers=auth_headers)

    assert response.status_code == 201, "the Career Path succeeded; only its wording did not improve"
    current = client.get("/career-path/current", headers=auth_headers).json()
    items = [i for p in current["phases"] for i in p["skill_items"]]
    assert items
    assert all(i["why_it_matters"].startswith(TEMPLATE_PREFIX) for i in items)
