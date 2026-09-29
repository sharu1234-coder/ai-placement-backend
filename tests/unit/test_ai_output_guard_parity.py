"""Repository-level parity guard: no production AI call site without output protection.

The defect class this exists for is the one the account-deletion parity test already guards one
layer down (tests/unit/test_account_service.py::
test_every_profile_owned_model_is_covered_by_account_deletion), and it is an omission, not a
logic error. app/ai/output_guard.py has existed for a long time and every service that applies
it applies it correctly -- but applying it was opt-in, so at the start of this work seven of the
thirteen AI-calling service modules simply never did. No test could see that, because every
output-leak test asserts against a service that already has the guard. The one service nobody
guarded is exactly the one nobody asserts on.

This inverts it. Instead of listing the services that should be guarded, it asks the AIProvider
ABC which methods generate text, finds every call to one of them anywhere under app/, and
requires the guard to be reachable from each call site. Adding a new AI service without output
protection now fails here rather than in production.

WHY THIS IS A TEST AND NOT A WRAPPER IN AIProvider
--------------------------------------------------
Enforcing the guard centrally -- having generate_structured itself raise on a leak -- was
considered first and rejected on evidence, not taste. assessment_run_service.get_next_question
does not catch AIGenerationError; its established behaviour on a leaked generation is to
substitute a safe generic question, which tests/api/test_assessments.py::
test_leaked_prompt_text_never_reaches_the_student asserts on directly. A provider that raised
instead would turn that substitution into a 422 the student cannot get past, changing a
user-visible failure contract in order to tighten a check the service already performs. Several
services likewise fall back to deterministic template text, and one (interview evaluation)
deliberately persists an ungraded row; a single central policy cannot express all three. So the
guard stays where the service's own failure architecture lives, and this test is what makes
"every service" true and keeps it true.

WHAT THIS TEST CAN AND CANNOT PROVE
-----------------------------------
It proves reachability: from the function containing an AI call, following calls within the same
module, the guard is referenced. It cannot prove the guard was applied to *that* call's output
rather than some other value -- no static rule can. That half is proved behaviourally, per
service, by tests/api/test_ai_output_leak_parity.py, which drives each real service with a
provider returning a leak and asserts nothing leaked reaches the response or the database. The
two together are the coverage; neither alone is.
"""

import ast
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import app
from app.ai.provider import AIProvider

_APP_ROOT = Path(app.__file__).resolve().parent
_GUARD_MODULE = "app.ai.output_guard"

# Provider methods whose return value is not model-authored text, with the reason each is out of
# scope. Kept as data rather than left out of the scan, because a silent omission is the exact
# defect this file exists to prevent -- and _test_non_text_methods_still_exist below fails if one
# of these is renamed or removed, which forces the reasoning to be revisited rather than lost.
_NON_TEXT_PROVIDER_METHODS: Dict[str, str] = {
    "transcribe": (
        "Returns a transcript of the student's own recorded answer. No prompt is sent to a "
        "transcription model, so there is no prompt for it to leak; the transcript is the "
        "student's input, and it is treated as untrusted on the way back IN by "
        "input_guard.render_prompt when interview evaluation interpolates it. Guarding it as "
        "output would mean refusing to store a student's answer because of the words they said."
    ),
    "synthesize_speech": (
        "Returns audio bytes, not text, and its input is InterviewQuestion.question_text -- "
        "already guarded at generation by interview_service before it was ever persisted."
    ),
}

# Modules that call a generation method but are the AI layer itself rather than a consumer of it.
# Narrow on purpose: the three provider implementation files, not all of app/ai/, so that a
# service module added under app/ai/ would still be scanned.
_PROVIDER_LAYER = {
    _APP_ROOT / "ai" / "provider.py",
    _APP_ROOT / "ai" / "anthropic_provider.py",
    _APP_ROOT / "ai" / "ollama_provider.py",
}

# Production AI call sites that deliberately do not apply the output guard, each with the reason
# it is not required. Empty, and kept as the documented escape hatch rather than removed: the
# alternative when a genuine exception appears is editing this test's logic under time pressure,
# which is how an exception becomes an accident.
#
# Keyed by module path relative to app/ (e.g. "services/foo_service.py").
_OUTPUT_GUARD_EXEMPTIONS: Dict[str, str] = {}


def _generation_methods() -> Set[str]:
    """The AIProvider methods that return model-authored text, derived from the ABC itself.

    Deliberately derived rather than hardcoded: a provider method added tomorrow lands in the
    guarded set by default, so the failure mode of forgetting to update this file is a test
    failure rather than a silent hole. That is the same fail-closed direction the rest of this
    file takes.
    """
    public = {
        name
        for name in vars(AIProvider)
        if not name.startswith("_") and callable(getattr(AIProvider, name, None))
    }
    return public - set(_NON_TEXT_PROVIDER_METHODS)


class _ModuleScan:
    """One module's AI call sites and the reachability of the guard from each.

    Scopes are attributed to the innermost enclosing function, so a nested helper is analysed as
    itself rather than as part of whatever encloses it.
    """

    def __init__(self, tree: ast.Module, guard_names: Set[str], methods: Set[str]) -> None:
        self._guard_names = guard_names
        self._methods = methods
        # scope key -> (calls_ai, references_guard, callees)
        self.scopes: Dict[str, Tuple[bool, bool, Set[str]]] = {}
        self._walk(tree, scope="<module>")

    def _walk(self, node: ast.AST, scope: str) -> None:
        calls_ai = False
        refs_guard = False
        callees: Set[str] = set()
        nested: List[Tuple[ast.AST, str]] = []

        body = node.body if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)) else []
        stack: List[ast.AST] = list(body)
        while stack:
            current = stack.pop()
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A nested/own function is its own scope; do not fold its body into this one.
                nested.append((current, current.name))
                continue
            if isinstance(current, ast.ClassDef):
                nested.append((current, current.name))
                stack.extend(current.body)
                continue
            if isinstance(current, ast.Call):
                func = current.func
                if isinstance(func, ast.Attribute) and func.attr in self._methods:
                    calls_ai = True
                if isinstance(func, ast.Name):
                    callees.add(func.id)
                elif isinstance(func, ast.Attribute):
                    callees.add(func.attr)
            if isinstance(current, ast.Name) and current.id in self._guard_names:
                refs_guard = True
            if isinstance(current, ast.Attribute) and current.attr in self._guard_names:
                refs_guard = True
            stack.extend(ast.iter_child_nodes(current))

        self.scopes[scope] = (calls_ai, refs_guard, callees)
        for child, name in nested:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._walk(child, scope=name)

    def guard_reachable(self, scope: str) -> bool:
        """True if `scope`, or anything it calls within this module (transitively), touches the
        guard. One level would already cover every current service -- assessment and placement
        alarm both guard inside a _sanitize_generated_question helper -- but the closure is
        computed in full so that a service organised with one more helper in between is not
        reported as a violation it is not.
        """
        seen: Set[str] = set()
        pending = [scope]
        while pending:
            current = pending.pop()
            if current in seen or current not in self.scopes:
                continue
            seen.add(current)
            _, refs_guard, callees = self.scopes[current]
            if refs_guard:
                return True
            pending.extend(callees)
        return False

    def unguarded_scopes(self) -> List[str]:
        return sorted(
            scope for scope, (calls_ai, _, _) in self.scopes.items()
            if calls_ai and not self.guard_reachable(scope)
        )


def _guard_names(tree: ast.Module) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _GUARD_MODULE:
            names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _GUARD_MODULE:
                    # `import app.ai.output_guard as og` -- the whole module is the guard symbol.
                    names.add((alias.asname or alias.name).split(".")[-1])
    return names


def _scan_backend(source_override: Optional[Tuple[Path, str]] = None) -> Dict[str, List[str]]:
    """Every module under app/ that calls a generation method, mapped to its unguarded scopes.

    `source_override` replaces one file's source text, used by the self-check below to prove this
    rule can actually fail without breaking a real service to find out.
    """
    methods = _generation_methods()
    violations: Dict[str, List[str]] = {}

    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path in _PROVIDER_LAYER:
            continue
        relative = path.relative_to(_APP_ROOT).as_posix()
        if relative in _OUTPUT_GUARD_EXEMPTIONS:
            continue
        if source_override is not None and source_override[0] == path:
            source = source_override[1]
        else:
            source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        scan = _ModuleScan(tree, _guard_names(tree), methods)
        unguarded = scan.unguarded_scopes()
        if unguarded:
            violations[relative] = unguarded
    return violations


def test_every_production_ai_call_site_can_reach_the_output_guard():
    violations = _scan_backend()

    assert not violations, (
        "These functions call an AI provider generation method but the output guard is not "
        "reachable from them, so whatever the model returned can be persisted or returned to a "
        "student unchecked:\n"
        + "\n".join(f"  {module}: {', '.join(scopes)}" for module, scopes in sorted(violations.items()))
        + "\n\nApply app/ai/output_guard.py to the generated value before it is used -- "
        "contains_prompt_leak(generated) for a whole model, looks_like_prompt_leak(text) for a "
        "single string -- following that service's existing failure behaviour (raise "
        "AIGenerationError where it has no fallback, skip the field where it has a deterministic "
        "template). If the call genuinely does not need the guard, add its module to "
        "_OUTPUT_GUARD_EXEMPTIONS with the reason."
    )


def test_the_parity_guard_actually_detects_an_unguarded_call_site():
    """Proves the rule above can fail -- a test that only ever passes is not a guard.

    Rather than temporarily unguarding a real service, this re-runs the identical rule against
    one module's source with its guard import removed, and asserts the module is reported.
    """
    target = _APP_ROOT / "services" / "roadmap_service.py"
    crippled = target.read_text(encoding="utf-8").replace(
        "from app.ai.output_guard import leaking_field\n", ""
    ).replace("leaking_field(generated)", "None")

    violations = _scan_backend(source_override=(target, crippled))

    assert "services/roadmap_service.py" in violations, (
        "The parity rule did not report a service whose guard was removed, so it would not "
        "report a new service that never had one."
    )
    assert "generate_roadmap" in violations["services/roadmap_service.py"]


def test_a_brand_new_unguarded_ai_service_would_be_caught():
    """The §64 question, asked directly: a developer adds a new AI service tomorrow and forgets
    the guard -- does the suite fail? This runs the rule against exactly that module.
    """
    methods = _generation_methods()
    new_service = ast.parse(
        "from app.ai.provider import AIProvider as Provider\n"
        "\n"
        "def summarise_something(payload, engine: Provider):\n"
        "    result = engine.generate_structured(payload, object)\n"
        "    return result.text\n"
    )
    scan = _ModuleScan(new_service, _guard_names(new_service), methods)

    assert scan.unguarded_scopes() == ["summarise_something"], (
        "A new module that calls generate_structured with no guard anywhere must be reported."
    )


def test_aliasing_the_provider_import_does_not_evade_the_rule():
    """The rule matches the method name on the call, never the imported symbol, so renaming the
    import or the variable changes nothing."""
    methods = _generation_methods()
    aliased = ast.parse(
        "from app.ai import provider as p\n"
        "\n"
        "def _helper(thing):\n"
        "    return thing\n"
        "\n"
        "def sneaky(anything):\n"
        "    return _helper(anything.generate_structured('prompt', dict))\n"
    )
    scan = _ModuleScan(aliased, _guard_names(aliased), methods)
    assert scan.unguarded_scopes() == ["sneaky"]


def test_a_guard_applied_in_a_called_helper_counts():
    """The shape assessment_run_service and placement_alarm_service both use: generate in one
    function, guard in a helper it calls. That must pass, or the rule would push a refactor it
    has no business demanding."""
    methods = _generation_methods()
    indirect = ast.parse(
        "from app.ai.output_guard import contains_prompt_leak\n"
        "\n"
        "def _sanitize(generated):\n"
        "    if contains_prompt_leak(generated):\n"
        "        return None\n"
        "    return generated\n"
        "\n"
        "def run(engine):\n"
        "    return _sanitize(engine.generate_structured('p', dict))\n"
    )
    scan = _ModuleScan(indirect, _guard_names(indirect), methods)
    assert scan.unguarded_scopes() == []


def test_non_text_provider_methods_still_exist():
    """A method exempted from the scan must still be on AIProvider.

    If one is renamed or dropped, the exemption silently stops matching anything and its reason
    stops being reviewed -- so this fails instead, and whoever renamed it has to restate why the
    new name is out of scope.
    """
    missing = sorted(name for name in _NON_TEXT_PROVIDER_METHODS if not hasattr(AIProvider, name))
    assert not missing, (
        f"_NON_TEXT_PROVIDER_METHODS names methods AIProvider no longer has: {missing}. "
        "Update the exemption and restate why the method is not model-authored text."
    )


def test_every_provider_method_is_either_scanned_or_exempted_with_a_reason():
    """No third category. A method added to AIProvider is scanned by default, and the only way
    out is an entry here with a written reason -- which is what stops a new generation method
    from quietly becoming an unguarded surface."""
    public = {
        name
        for name in vars(AIProvider)
        if not name.startswith("_") and callable(getattr(AIProvider, name, None))
    }
    classified = _generation_methods() | set(_NON_TEXT_PROVIDER_METHODS)

    assert public == classified, (
        f"AIProvider methods that are neither scanned nor exempted: {sorted(public - classified)}"
    )
    assert all(reason.strip() for reason in _NON_TEXT_PROVIDER_METHODS.values()), (
        "Every non-text exemption must carry a reason."
    )


def test_exemption_registry_is_empty_and_any_entry_carries_a_reason():
    """Not a style check. Every production AI call site is guarded today, and the registry is the
    only place a future exception could hide -- so an entry appearing here is a change that has
    to be argued for in review rather than one that slips in with a passing suite.
    """
    assert all(reason.strip() for reason in _OUTPUT_GUARD_EXEMPTIONS.values()), (
        "An exemption without a reason is a silent gap, which is what this file exists to prevent."
    )
    assert _OUTPUT_GUARD_EXEMPTIONS == {}, (
        "A production AI call site has been exempted from output guarding: "
        f"{sorted(_OUTPUT_GUARD_EXEMPTIONS)}. That may be correct, but it is a security decision "
        "and this assertion is here so it cannot be made quietly."
    )


def test_the_scan_actually_finds_the_known_ai_services():
    """Guards the guard: if the AST walk silently stopped matching anything -- a renamed method,
    a broken traversal -- every other assertion here would still pass while checking nothing.
    """
    methods = _generation_methods()
    assert "generate_structured" in methods and "generate_text" in methods

    found = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        if path in _PROVIDER_LAYER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scan = _ModuleScan(tree, _guard_names(tree), methods)
        if any(calls_ai for calls_ai, _, _ in scan.scopes.values()):
            found.append(path.relative_to(_APP_ROOT).as_posix())

    expected = {
        "services/assessment_run_service.py",
        "services/ats_check_service.py",
        "services/career_path/ai_generation.py",
        "services/interview_service.py",
        "services/jd_service.py",
        "services/placement_alarm_service.py",
        "services/placement_path_service.py",
        "services/professional_presence/audit_service.py",
        "services/project_builder/ai_generation.py",
        "services/project_proof_service.py",
        "services/recruiter_attack_service.py",
        "services/resume_service.py",
        "services/roadmap_service.py",
    }
    missing = expected - set(found)
    assert not missing, (
        f"The scan no longer sees known AI-calling modules: {sorted(missing)}. Either the call "
        "moved, or the traversal is broken and every other assertion in this file is vacuous."
    )
