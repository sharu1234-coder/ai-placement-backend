"""TEST-1: the racing helpers must not contaminate the rest of the suite.

The defect these guard against cost 16 failures in `tests/api` that had nothing to do with the
code under test. `tests/fixtures/career_path_helpers.py` patched
`ai_generation.template_skill_explanations` at **module import time** and never restored it, and
the wrapper waited on a module-global `_active_barrier` that only a `RacingProvider` built
*without* a barrier ever reset. The last test in
`tests/unit/test_career_path_uniqueness_concurrency.py` arms a 2-party barrier, so from that
point on every call through the wrapper anywhere in the suite waited the full 30s and raised
`BrokenBarrierError`. Those tests passed when run alone and failed when run after it.

Two different things have to hold, and a test for either one alone would miss the other:

* the hook is genuinely installed while a racing test runs -- otherwise the fix would have
  turned guaranteed races back into lucky ones with every test still green, which is precisely
  the hazard `pre_write_barrier`'s own docstring describes; and
* nothing survives the test that installed it.
"""

import importlib
import threading

from app.services.career_path import ai_generation
from tests.fixtures import career_path_helpers
from tests.fixtures.career_path_helpers import (  # noqa: F401 - pre_write_barrier is an autouse fixture
    RacingProvider,
    pre_write_barrier,
)


def test_the_barrier_hook_is_installed_while_a_racing_test_runs():
    """Without this, a fixture that quietly failed to apply would still look green."""
    assert getattr(ai_generation.template_skill_explanations, "is_pre_write_barrier", False), (
        "the pre_write_barrier fixture did not install its hook, so a race in this module "
        "would no longer be guaranteed -- it would merely be likely"
    )


def test_every_test_starts_with_no_armed_barrier():
    """Runs after the test below arms one, in file order, and must still see None.

    This is the assertion that fails if the fixture stops resetting the global.
    """
    assert career_path_helpers._active_barrier is None


def test_arming_a_barrier_here_does_not_leak_to_the_next_test():
    # Deliberately left armed. The fixture teardown is what has to clean it up; the two tests
    # either side of this one prove that it does.
    RacingProvider(barrier=threading.Barrier(2))

    assert career_path_helpers._active_barrier is not None


def test_a_later_test_still_sees_no_armed_barrier():
    assert career_path_helpers._active_barrier is None


def test_importing_the_helpers_does_not_patch_production_code():
    """The import-time mutation must not come back.

    Order-independent on purpose: it compares the production function across a reload rather
    than against a hardcoded identity, so it holds whether or not the autouse fixture is
    currently active. Under the old module-level `_install_pre_write_barrier()` call, reloading
    replaced the function with a fresh wrapper and this fails.
    """
    before = ai_generation.template_skill_explanations

    importlib.reload(career_path_helpers)

    assert ai_generation.template_skill_explanations is before, (
        "importing tests/fixtures/career_path_helpers.py patched application code as a side "
        "effect of import; the patch belongs in the pre_write_barrier fixture so it is undone"
    )
