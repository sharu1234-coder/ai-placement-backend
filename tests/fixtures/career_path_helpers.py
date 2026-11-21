"""A deterministic AI provider for the Career Path concurrency tests.

Shared rather than duplicated because two test modules race the same flow -- one through the
service, one through the HTTP route -- and two drifting copies of "what the model returns" is how
a suite starts passing against content production never produces.

The barrier is what makes a race certain rather than lucky. select_role() does its AI work before
it writes anything, so releasing every thread from inside the provider guarantees they have all
finished the "does this role already have a path?" lookup and none has begun its write.
"""

import threading

import pytest

from app.ai.provider import AIProvider
from app.ai.schemas.career_path import (
    GeneratedCareerPathExplanations,
    GeneratedRoleCatalog,
    GeneratedRoleOverview,
    GeneratedRolePhase,
    GeneratedRoleSkill,
    GeneratedSkillExplanation,
)
from app.services.career_path import ai_generation

_active_barrier = None


@pytest.fixture(autouse=True)
def pre_write_barrier():
    """Release racing threads at the last point before select_role() writes anything.

    That point used to be the skill-explanation AI call, so a barrier inside the provider was
    enough. Since F12-05 select_role() makes no AI call at all for a curated role -- the
    deterministic template_skill_explanations() took that place, in the same position, still
    before the first write -- so a provider-side barrier would simply never be reached and these
    races would become lucky rather than guaranteed, with every test still passing. Hooking the
    call that replaced it keeps the guarantee exactly where it was.

    A no-op until a RacingProvider is given a barrier, so a test that arms none behaves exactly
    as if this were not here.

    **Why this is a fixture and not an import-time call.** It used to be the latter, and the
    patch was therefore permanent for the whole pytest process while `_active_barrier` -- a
    module global -- was only ever reset by the side effect of constructing a RacingProvider
    without a barrier. The last test in tests/unit/test_career_path_uniqueness_concurrency.py
    arms a 2-party barrier and nothing reset it, so every later call through this wrapper
    anywhere in the suite waited the full 30s and then raised BrokenBarrierError: 16
    career-path tests in tests/api failed purely on run order, and passed when run alone. As a
    fixture the patch lasts one test and both the original function and the global are put back,
    so no test can be contaminated by another.

    Autouse, and imported into each racing module rather than placed in a conftest: pytest
    registers fixtures found in a test module's namespace, so `autouse` applies to exactly the
    two modules that import it and to nothing else in the suite.
    """
    global _active_barrier
    original = ai_generation.template_skill_explanations

    def wrapper(role_name, items):
        result = original(role_name, items)
        if _active_barrier is not None:
            _active_barrier.wait(timeout=30)
        return result

    # Lets a guard test prove the hook is actually installed. Without that, a fixture that
    # silently failed to apply would turn these races back into luck with every test still
    # green -- the exact failure this module's own docstring warns about.
    wrapper.is_pre_write_barrier = True

    ai_generation.template_skill_explanations = wrapper
    try:
        yield
    finally:
        ai_generation.template_skill_explanations = original
        _active_barrier = None


class RacingProvider(AIProvider):
    """Fixed content, optionally released together.

    The barrier is installed at the pre-write seam by [_install_pre_write_barrier]; assigning it
    here (at construction or later) is what arms it. Each AI call's number goes into the text it
    returns, so a stored row says which request built it, and the call counters say how much AI
    work a race cost -- including the work the deferred explanation pass does after the response.
    """

    def __init__(self, barrier: threading.Barrier = None):
        self.lock = threading.Lock()
        self.definition_calls = 0
        self.explanation_calls = 0
        self.barrier = barrier

    @property
    def barrier(self):
        return self._barrier

    @barrier.setter
    def barrier(self, value):
        global _active_barrier
        self._barrier = value
        _active_barrier = value

    def generate_structured(self, prompt, schema):
        if schema is GeneratedRoleCatalog:
            with self.lock:
                self.definition_calls += 1
                call = self.definition_calls
            return GeneratedRoleCatalog(
                overview=GeneratedRoleOverview(
                    short_explanation=f"Definition #{call}.",
                    responsibilities=["Do the work"],
                    core_skills=["Skill A"],
                    tools=["Tool A"],
                    difficulty="moderate",
                    entry_level_expectations="Baseline.",
                    interview_focus=["Fundamentals"],
                    portfolio_expectations="One project.",
                ),
                # A stable skill_key across calls: a custom role's path stores the definition it
                # was built from and carries progress forward by key, so two requests generating
                # the same role have to agree on the keys for carry-forward to mean anything.
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
                call = self.explanation_calls
            keys = [line.split(" | ")[0].strip() for line in prompt.splitlines() if line.count(" | ") == 3]
            return GeneratedCareerPathExplanations(
                explanations=[
                    GeneratedSkillExplanation(skill_key=key, why_it_matters=f"Explained by call #{call}.") for key in keys
                ]
            )
        raise AssertionError(f"Unexpected schema requested: {schema}")

    def generate_text(self, prompt):
        return "fake text"
