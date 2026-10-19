"""Turning tool results into grounded claims.

One function per tool shape, because the alternative -- a generic "describe this dict" -- would
produce sentences whose relationship to the data nobody can check. Every claim built here names
the tool that produced it, and a tool result that says "unavailable" becomes an uncertainty
rather than silence.

Nothing in this module invents a number, rounds one into a different meaning, or states a
world figure without its sample. The world side reuses the provenance the VA-5 layer already
attached, so freshness and provider travel into the claim rather than being re-derived.
"""

from __future__ import annotations

import datetime
from typing import Any, Optional

from app.agent.contracts import Claim, EvidenceKind, EvidenceRef

_MAX_LISTED = 5


def _world_ref(tool: str, provenance: Any, *, kind: EvidenceKind, summary: str, sample: Optional[str] = None) -> EvidenceRef:
    return EvidenceRef(
        kind=kind,
        tool=tool,
        summary=summary,
        provider=getattr(provenance, "provider", None),
        retrieved_at=_parse(getattr(provenance, "retrieved_at", None)),
        published_at=_parse(getattr(provenance, "published_at", None)),
        freshness=getattr(provenance, "freshness", None),
        sample=sample,
    )


def _parse(value: Any) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _known(tool: str, summary: str) -> EvidenceRef:
    return EvidenceRef(kind=EvidenceKind.KNOWN, tool=tool, summary=summary)


# --------------------------------------------------------------------------- personal


def from_personal(tool: str, output: Any) -> tuple[tuple[Claim, ...], tuple[str, ...]]:
    """Claims from one personal read tool's output. Returns (claims, uncertainties)."""
    claims: list[Claim] = []
    unknowns: list[str] = []

    if tool == "profile.get_summary":
        role = getattr(output, "target_role", None)
        if role:
            claims.append(Claim(f"Your target role is {role}.", (_known(tool, f"profile target_role={role}"),)))
        else:
            unknowns.append("Your profile does not name a target role yet.")
        college = getattr(output, "college", None)
        year = getattr(output, "graduation_year", None)
        if college and year:
            claims.append(
                Claim(f"You are at {college}, graduating {year}.", (_known(tool, f"profile college/year={college}/{year}"),))
            )

    elif tool == "skills.get_passport":
        skills = list(getattr(output, "skills", ()) or ())
        if not skills:
            unknowns.append("No skills are recorded in your passport yet, so I cannot compare them to anything.")
        else:
            strong = [s.skill for s in skills if (s.confidence_percent or 0) >= 60]
            weak = [s.skill for s in skills if (s.confidence_percent or 0) < 60]
            if strong:
                claims.append(
                    Claim(
                        f"You have evidence for {', '.join(strong[:_MAX_LISTED])}.",
                        (_known(tool, f"{len(strong)} skill(s) at 60%+ confidence"),),
                    )
                )
            if weak:
                claims.append(
                    Claim(
                        f"These are still thin on evidence: {', '.join(weak[:_MAX_LISTED])}.",
                        (_known(tool, f"{len(weak)} skill(s) below 60% confidence"),),
                    )
                )

    elif tool == "career_twin.get_readiness":
        if not getattr(output, "has_role", False):
            unknowns.append("No career path is set, so readiness cannot be computed.")
        else:
            coverage = getattr(output, "evidence_coverage_percent", None)
            if coverage is not None:
                estimate = " (an estimate)" if getattr(output, "is_estimate", False) else ""
                claims.append(
                    Claim(
                        f"Your evidence coverage is {coverage}%{estimate}.",
                        (_known(tool, f"career twin coverage={coverage}%"),),
                    )
                )
            gap = getattr(output, "biggest_gap_skill", None)
            if gap:
                claims.append(Claim(f"Your biggest gap is {gap}.", (_known(tool, f"biggest_gap_skill={gap}"),)))

    elif tool == "weaknesses.list":
        rows = list(getattr(output, "weaknesses", ()) or ())
        if not rows:
            unknowns.append("No recurring weaknesses are recorded yet.")
        else:
            named = ", ".join(f"{r.skill_key} ({r.occurrences}x)" for r in rows[:_MAX_LISTED])
            claims.append(Claim(f"Recurring weak areas: {named}.", (_known(tool, f"{len(rows)} weakness record(s)"),)))

    elif tool == "interviews.list_history":
        rows = list(getattr(output, "interviews", ()) or ())
        if not rows:
            unknowns.append("You have no completed mock interviews yet.")
        else:
            scored = [r.overall_score for r in rows if r.overall_score is not None]
            claims.append(
                Claim(
                    f"You have {len(rows)} recorded interview(s)."
                    + (f" Most recent score: {rows[0].overall_score}." if scored else ""),
                    (_known(tool, f"{len(rows)} interview(s)"),),
                )
            )

    elif tool == "progress.get_summary":
        score = getattr(output, "performance_score", None)
        if score is None:
            unknowns.append("No performance score has been computed yet.")
        else:
            claims.append(Claim(f"Your performance score is {score}.", (_known(tool, f"performance_score={score}"),)))
        done = getattr(output, "roadmap_tasks_done", None)
        total = getattr(output, "roadmap_tasks_total", None)
        if done is not None and total:
            claims.append(
                Claim(f"You have completed {done} of {total} roadmap tasks.", (_known(tool, f"roadmap {done}/{total}"),))
            )

    elif tool == "resume.get_latest_summary":
        if getattr(output, "has_resume", False):
            status = getattr(output, "parse_status", None)
            claims.append(Claim(f"A resume is uploaded (parse status: {status}).", (_known(tool, f"resume status={status}"),)))
        else:
            unknowns.append("No resume is uploaded yet.")

    elif tool == "resume.list_ats_checks":
        rows = list(getattr(output, "checks", ()) or ())
        if rows and rows[0].overall_score is not None:
            claims.append(
                Claim(
                    f"Your latest ATS score is {rows[0].overall_score} ({rows[0].score_category}).",
                    (_known(tool, f"ats score={rows[0].overall_score}"),),
                )
            )
        elif not rows:
            unknowns.append("No ATS check has been run on your resume yet.")

    elif tool == "jobs.list_saved":
        rows = list(getattr(output, "jobs", ()) or ())
        if not rows:
            unknowns.append("You have no saved jobs yet.")
        else:
            claims.append(
                Claim(
                    f"You have {len(rows)} saved job(s), including {rows[0].title} at {rows[0].company}.",
                    (_known(tool, f"{len(rows)} saved job(s)"),),
                )
            )

    elif tool == "career_goals.list":
        # VA-8. Registered in VA-4 and unreachable until now: no intent planned it and this
        # branch did not exist, so the tool would have run and produced nothing at all.
        goals = list(getattr(output, "goals", ()) or ())
        if not goals:
            unknowns.append("You have not recorded any career goals yet, so I have nothing to compare roles against.")
        else:
            titles = ", ".join(
                f"{g.title}{f' at {g.company}' if g.company else ''}" for g in goals[:_MAX_LISTED]
            )
            claims.append(
                Claim(
                    f"You have {len(goals)} recorded career goal(s): {titles}.",
                    (_known(tool, f"{len(goals)} goal row(s)"),),
                )
            )
            unanalysed = [g for g in goals if not g.is_analyzed]
            if unanalysed:
                # Named as a limitation on the comparison rather than as advice: an unanalysed
                # goal has no requirement breakdown, so any fit statement about it would be
                # weaker than one about an analysed goal and must not read the same.
                unknowns.append(
                    f"{len(unanalysed)} of those have not been analysed yet, so I cannot compare "
                    "your skills against what they ask for."
                )

    elif tool == "career_path.get_current":
        # VA-8, same history as career_goals.list above.
        if getattr(output, "has_path", False):
            claims.append(
                Claim(
                    f"You have a career path for {getattr(output, 'display_role_name', None) or 'your target role'} "
                    f"with {getattr(output, 'phase_count', 0)} phase(s) and "
                    f"{getattr(output, 'skill_gap_count', 0)} recorded skill gap(s).",
                    (_known(tool, "the current career path"),),
                )
            )
        else:
            unknowns.append("You do not have a generated career path yet.")

    elif tool == "missions.get_today":
        if getattr(output, "has_mission", False):
            claims.append(
                Claim(
                    f"Today's mission is: {getattr(output, 'title', None)} ({getattr(output, 'status', None)}).",
                    (_known(tool, "today's mission"),),
                )
            )
        else:
            # Deliberately phrased as "not generated", not "you have nothing to do": the VA-4
            # read tool does not create today's mission, so its absence is a state of the
            # system rather than a statement about the student's day.
            unknowns.append("Today's mission has not been generated yet.")

    return tuple(claims), tuple(unknowns)


# --------------------------------------------------------------------------- world


def from_world(tool: str, output: Any) -> tuple[tuple[Claim, ...], tuple[str, ...]]:
    """Claims from one world tool's output, carrying provenance into the evidence."""
    claims: list[Claim] = []
    unknowns: list[str] = []

    if getattr(output, "available", True) is False:
        # VA-5's honest-unavailability shape. Surfaced with its own reason rather than as an
        # empty result, because an empty list would read as "the market has nothing".
        reason = getattr(output, "reason", None) or "not enough evidence"
        unknowns.append(f"I could not establish this from live data: {reason}")
        return (), tuple(unknowns)

    if tool in ("world.find_openings", "world.find_fresher_openings"):
        rows = list(getattr(output, "openings", ()) or ())
        if not rows:
            unknowns.append("The job source returned no matching openings for that query.")
        else:
            first = rows[0]
            listed = ", ".join(f"{r.title} at {r.company or 'an unnamed company'}" for r in rows[:3])
            claims.append(
                Claim(
                    f"{len(rows)} matching opening(s) were retrieved, including {listed}.",
                    (
                        _world_ref(
                            tool,
                            first.provenance,
                            kind=EvidenceKind.LIVE_OBSERVED,
                            summary=f"{len(rows)} advert(s) retrieved",
                        ),
                    ),
                )
            )
            stale = [r for r in rows if r.provenance.freshness in ("stale", "undated")]
            if stale:
                unknowns.append(
                    f"{len(stale)} of these could not be confirmed as recent "
                    "(the source gave no posting date, or the advert is old)."
                )

    elif tool in ("world.get_role_requirements", "world.get_skill_demand"):
        rows = list(getattr(output, "skills", ()) or ())
        sample = getattr(getattr(output, "sample", None), "description", None)
        if not rows:
            unknowns.append("No recognised skills were found in the retrieved adverts.")
        else:
            named = ", ".join(f"{r.skill} ({r.openings_mentioning}/{r.sample_size})" for r in rows[:_MAX_LISTED])
            claims.append(
                Claim(
                    f"Across the adverts retrieved, the most-named skills were {named}.",
                    (
                        _world_ref(
                            tool,
                            getattr(output, "provenance", None),
                            kind=EvidenceKind.DERIVED,
                            summary=f"{len(rows)} skill(s) counted",
                            sample=sample,
                        ),
                    ),
                )
            )

    elif tool == "world.get_company_hiring":
        rows = list(getattr(output, "companies", ()) or ())
        if rows:
            named = ", ".join(f"{r.company} ({r.openings_in_sample})" for r in rows[:_MAX_LISTED])
            claims.append(
                Claim(
                    f"Companies advertising in this sample: {named}.",
                    (
                        _world_ref(
                            tool,
                            getattr(output, "provenance", None),
                            kind=EvidenceKind.DERIVED,
                            summary=f"{len(rows)} company/companies in sample",
                            sample=getattr(getattr(output, "sample", None), "description", None),
                        ),
                    ),
                )
            )

    elif tool == "world.compare_locations":
        counts = dict(getattr(output, "openings_by_location", {}) or {})
        if counts:
            rendered = ", ".join(f"{place}: {count}" for place, count in sorted(counts.items()))
            claims.append(
                Claim(
                    f"Adverts retrieved per location -- {rendered}.",
                    (
                        _world_ref(
                            tool,
                            getattr(output, "provenance", None),
                            kind=EvidenceKind.DERIVED,
                            summary="per-location advert counts",
                            sample=getattr(getattr(output, "sample", None), "description", None),
                        ),
                    ),
                )
            )
            # The comparison's own limitation, stated beside the numbers rather than in a
            # footnote: provider coverage can differ by city, and that difference is
            # indistinguishable from a real one.
            unknowns.append(
                "These are counts of retrieved adverts, not a measure of the job markets: "
                "the source may index the cities differently."
            )

    for note in list(getattr(output, "capability_limitations", ()) or ())[:2]:
        unknowns.append(note)

    return tuple(claims), tuple(unknowns)


# --------------------------------------------------------------------------- the join


def skill_gap(personal_skills: Any, world_skills: Any, tools: tuple[str, str]) -> tuple[tuple[Claim, ...], tuple[str, ...]]:
    """What the market named that the student has no evidence for.

    The one genuinely joined claim in VA-6, and the reason the evidence kind is `DERIVED`
    rather than `KNOWN`: it is computed from two sources, and presenting it as a fact about
    the student would overstate both. The claim names the sample it came from so "you are
    missing Kubernetes" cannot be read as a market-wide verdict.
    """
    personal_tool, world_tool = tools
    have = {
        (s.skill or "").lower()
        for s in (getattr(personal_skills, "skills", ()) or ())
        if (s.confidence_percent or 0) >= 60
    }
    wanted = [(r.skill, r.openings_mentioning, r.sample_size) for r in (getattr(world_skills, "skills", ()) or ())]
    if not wanted:
        return (), ("I could not retrieve what the market is asking for, so I cannot compute a gap.",)
    if not have:
        return (), (
            "You have no skills at 60%+ evidence yet, so a gap against the market would just "
            "be the whole list.",
        )

    missing = [(skill, hits, size) for skill, hits, size in wanted if skill.lower() not in have]
    if not missing:
        return (
            Claim(
                "Every skill the retrieved adverts named is one you already have evidence for.",
                (
                    _known(personal_tool, f"{len(have)} skill(s) at 60%+"),
                    EvidenceRef(
                        kind=EvidenceKind.DERIVED,
                        tool=world_tool,
                        summary=f"{len(wanted)} skill(s) named in the sample",
                    ),
                ),
            ),
        ), ()

    named = ", ".join(f"{skill} ({hits}/{size} adverts)" for skill, hits, size in missing[:_MAX_LISTED])
    return (
        Claim(
            f"The adverts named these, and you have no strong evidence for them yet: {named}.",
            (
                _known(personal_tool, f"{len(have)} skill(s) at 60%+ evidence"),
                EvidenceRef(
                    kind=EvidenceKind.DERIVED,
                    tool=world_tool,
                    summary=f"{len(missing)} of {len(wanted)} named skill(s) unmatched",
                ),
            ),
        ),
    ), ()
