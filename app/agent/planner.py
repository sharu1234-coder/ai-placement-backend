"""Intent to tool plan. A fixed mapping, validated against the live registry.

Fixed rather than model-chosen, which is the tool policy expressed as code: what can execute
for a given intent is knowable before anything runs, and a model never names a capability. The
model's only influence is on which `Intent` was meant, and that answer is checked back against
the closed set.

Every planned call is validated against what `describe_for(context)` actually offers, so a
capability the caller is not entitled to -- or one that has been removed -- cannot be attempted.
That check is deliberately *not* "is this name in the registry": a premium-gated tool is in the
registry and absent from a free caller's catalogue, and attempting it would leak its existence
through the error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.agent.contracts import Entities, Intent

#: Personal read tools per intent, in the order they should run.
_PERSONAL: dict[Intent, tuple[str, ...]] = {
    Intent.PROFILE_STATUS: ("profile.get_summary",),
    Intent.SKILL_STATUS: ("skills.get_passport",),
    # VA-8: readiness is a coaching answer, not a number. The headline comes from the twin;
    # the skills and weaknesses are what make "and here is the evidence behind it, and here is
    # what is missing" answerable at all. Three cheap database reads, well inside the budget.
    Intent.READINESS: ("career_twin.get_readiness", "skills.get_passport", "weaknesses.list"),
    Intent.WEAKNESSES: ("weaknesses.list",),
    Intent.INTERVIEW_HISTORY: ("interviews.list_history",),
    Intent.PROGRESS: ("progress.get_summary",),
    Intent.RESUME_STATUS: ("resume.get_latest_summary", "resume.list_ats_checks"),
    Intent.SAVED_JOBS: ("jobs.list_saved",),
    # VA-8: a next step the student can act on needs more than whether a mission row exists --
    # what they are weak at and how far their evidence has got decide which step is useful.
    Intent.TODAY_PLAN: ("missions.get_today", "career_twin.get_readiness", "weaknesses.list"),
    # VA-8. Both were registered in VA-4 and reachable by no intent and no grounding branch
    # until now, so the capability existed and could not be used. Role selection is answered
    # from the student's own recorded goals, the path they already have, and their readiness --
    # the market is not consulted, because which goals they recorded is not a market question.
    Intent.ROLE_SELECTION: ("career_goals.list", "career_path.get_current", "career_twin.get_readiness"),
    # A plan is built from the gap it is meant to close, so it reads the same evidence the
    # readiness answer does plus the goals it should aim at.
    Intent.CAREER_PLAN: (
        "career_twin.get_readiness",
        "skills.get_passport",
        "weaknesses.list",
        "career_goals.list",
    ),
    # The join: the student's own skills, then what the market asks for. Both sides are
    # needed, and the order matters only for readability of the audit trail.
    Intent.SKILL_GAP_VS_MARKET: ("skills.get_passport", "career_twin.get_readiness"),
}

#: World read tools per intent.
_WORLD: dict[Intent, tuple[str, ...]] = {
    Intent.JOB_SEARCH: ("world.find_openings",),
    Intent.FRESHER_JOBS: ("world.find_fresher_openings",),
    Intent.ROLE_REQUIREMENTS: ("world.get_role_requirements",),
    Intent.SKILL_DEMAND: ("world.get_skill_demand",),
    Intent.COMPANY_HIRING: ("world.get_company_hiring",),
    Intent.LOCATION_COMPARISON: ("world.compare_locations",),
    Intent.SKILL_GAP_VS_MARKET: ("world.get_role_requirements",),
    # VA-8: a plan aimed at a role should be shaped by what that role actually asks for, which
    # is a current-information question and therefore belongs on the retrieval path rather
    # than in model knowledge. It is dropped when no role is known -- see `_world_arguments`,
    # which returns None -- and the plan is then built from personal evidence alone and says so.
    Intent.CAREER_PLAN: ("world.get_role_requirements",),
}


@dataclass(frozen=True)
class PlannedCall:
    """One intended tool invocation. Arguments are built here, never by a model."""

    tool: str
    arguments: dict[str, Any]
    #: True when this call reaches an external provider: it spends a budget and can fail in
    #: ways a database query cannot, so the orchestrator treats its failure as partial rather
    #: than fatal.
    is_world: bool


def _world_arguments(intent: Intent, entities: Entities, limit: int) -> Optional[dict[str, Any]]:
    """Build a world query from entities only.

    This is the data-minimisation boundary in code: the dict is assembled from the bounded
    `Entities` and a literal limit, so there is no path by which a profile field, a skill list
    or an identifier could reach a provider. The tool's own schema forbids extra keys as a
    second line, but the first line is that nothing else is in scope here.
    """
    if intent is Intent.LOCATION_COMPARISON:
        if len(entities.locations) < 2:
            # Refused rather than padded with a default city: inventing the second location
            # would answer a comparison the student never asked for.
            return None
        return {
            "role": entities.role or entities.skill,
            "locations": list(entities.locations[:3]),
            "experience_level": entities.experience_level,
            "limit": limit,
        }

    role = entities.role or entities.skill
    if intent in (Intent.ROLE_REQUIREMENTS, Intent.SKILL_DEMAND) and not role:
        return None
    if intent is Intent.CAREER_PLAN and not role:
        # No role, no market query. A plan is still produced from the student's own evidence;
        # retrieving requirements for a role nobody named would shape their next 90 days
        # around a job they never mentioned.
        return None

    arguments: dict[str, Any] = {"limit": limit}
    if role:
        arguments["role"] = role
    if entities.location and entities.location != "remote":
        arguments["location"] = entities.location
    if entities.location == "remote":
        arguments["remote_only"] = True
    if entities.experience_level:
        arguments["experience_level"] = entities.experience_level
    if intent is Intent.FRESHER_JOBS:
        arguments["experience_level"] = "fresher"
    return arguments


def plan(
    intent: Intent,
    entities: Entities,
    available: frozenset[str],
    *,
    world_limit: int = 5,
    max_calls: int = 6,
) -> tuple[PlannedCall, ...]:
    """The calls to make for this intent, filtered to what the caller may actually use.

    `available` is the set of capability names from `registry.describe_for(context)` -- the
    caller's own catalogue, not the registry's full one. A tool missing from it is skipped
    silently here and reported as a limitation by the orchestrator, which is the right place:
    the planner's job is to decide, not to explain.
    """
    calls: list[PlannedCall] = []

    for tool in _PERSONAL.get(intent, ()):
        if tool in available:
            calls.append(PlannedCall(tool=tool, arguments={}, is_world=False))

    for tool in _WORLD.get(intent, ()):
        if tool not in available:
            continue
        arguments = _world_arguments(intent, entities, world_limit)
        if arguments is None:
            continue
        calls.append(PlannedCall(tool=tool, arguments=arguments, is_world=True))

    return tuple(calls[:max_calls])


def needs_more_detail(intent: Intent, entities: Entities) -> Optional[str]:
    """The clarifying question this intent cannot proceed without, if any.

    Asked rather than guessed. A role-requirements question with no role named could be
    answered for *some* default role, and the student would get a confident answer about a job
    they never mentioned -- harder to notice than being asked which role they meant.
    """
    if intent in (Intent.ROLE_REQUIREMENTS, Intent.SKILL_DEMAND) and not (entities.role or entities.skill):
        return "Which role or skill did you want me to look at?"
    if intent is Intent.LOCATION_COMPARISON and len(entities.locations) < 2:
        return "Which two cities should I compare?"
    if intent is Intent.CAREER_PLAN and entities.horizon_days is None:
        # Asked rather than defaulted. A 7-day plan and a 90-day plan are different pieces of
        # advice, and picking one would be answering a question about a timescale the student
        # did not choose. "45 day plan" lands here too: the request was clear, the horizon is
        # simply not one we build.
        return "Over what period -- the next 7, 30, 60 or 90 days?"
    return None
