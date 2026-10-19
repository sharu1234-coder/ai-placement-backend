# Voice Career Coach (VA-8)

**Status: a coaching layer over VA-6's read-only orchestrator, plus one HTTP boundary. No
speech, no audio, no voice transport, no write access, no live job provider. Nothing persisted.**

The name is the product's, and this document is explicit about what the word "voice" does not
yet mean here: there is no speech-to-text, no text-to-speech and no realtime path in VA-8. What
exists is the coaching capability a spoken interface will eventually call, built so that
connecting speech later changes no logic in this layer.

## 1. What VA-8 added

| Scenario | Before | Now |
|---|---|---|
Career readiness | one tool, the headline number only | `career_twin.get_readiness` + `skills.get_passport` + `weaknesses.list`, so the answer can name its evidence and its gaps |
Role selection | **nothing** | `Intent.ROLE_SELECTION` → `career_goals.list`, `career_path.get_current`, `career_twin.get_readiness`, joined by `coaching.role_fit` |
Skill-gap analysis | `SKILL_GAP_VS_MARKET` + `grounding.skill_gap` | unchanged, reused |
Opportunity discovery | six world intents | unchanged, reused |
Career roadmaps | **nothing** | `Intent.CAREER_PLAN` + `coaching.build_plan` for 7/30/60/90 days |
Daily coaching | `missions.get_today` only | + readiness and weaknesses, with `coaching.next_step` |
Follow-up reasoning | VA-6 session memory | unchanged, reused; horizon and weekly hours added as bounded entities |

### Two defects found, not introduced

**`career_goals.list` and `career_path.get_current` were dead capabilities.** Registered in
VA-4, planned by no intent, and with no branch in `grounding.from_personal` — where an
unhandled tool silently returns no claims and no uncertainties. They would have run, spent
budget and contributed nothing, with no error to notice. Both are now wired, and
`test_every_plannable_tool_has_a_grounding_branch` asserts the planner and the grounding layer
cannot disagree again.

**"is my resume ready" classified as `READINESS`.** `\bread(y|iness)\b` is checked before the
resume rules and matched first, so a question about a resume got an answer about a target role.
The existing VA-6 budget test passed only because `READINESS` happened to map to one tool, not
for the reason its comment gave. The readiness rule now declines sentences that mention a
resume, CV or ATS, rather than reordering the table — reordering would have let `\bresume\b`
start swallowing genuine readiness questions instead.

A third, smaller one: the role pattern was singular-only, so "what skills do **engineers**
need" produced a clarifying question whose answer was already in the sentence. Plurals are
accepted and normalised back to the singular before they reach a provider query.

## 2. Why the coaching logic is code

A coaching answer makes claims about a student's prospects. Three things must hold about it:
every statement names its support, a recommendation never reads as a fact, and nothing promises
an outcome. As prompt instructions those hold until the model has a bad day. As invariants:

- `Claim` cannot be constructed without evidence (VA-6).
- `Milestone` cannot be constructed without evidence **and** is refused outright if its text
  contains promise language — `guarantee`, `will get you`, `will be placed`, `100%`, `assured`.
  That is not a style rule. "You will get a job" is a prediction about a hiring decision made
  by people this system has never seen, and the cost of being believed lands on a student
  deciding how to spend their months.
- `ActionOutcome`-style self-consistency: a `CoachingPlan` refuses a milestone from another
  horizon.

No model is required, consulted or imported anywhere in `app/agent/coaching.py`.

## 3. The five evidence categories

VA-8 requires stored facts, retrieved observations, derived analysis, recommendations and
unknowns to stay apart. They are **not** re-derived: they are exactly the existing
`EvidenceKind` values, and `CoachingReport.from_answer` partitions the turn's own claims by
`Claim.weakest_kind`.

| Category | Evidence kind | Source |
|---|---|---|
Stored facts | `KNOWN` | authenticated Placement OS records |
Retrieved observations | `LIVE_OBSERVED` | a world provider, with provider + retrieval time required at construction |
Derived analysis | `DERIVED` | a comparison this code performed, carrying its sample |
Recommendations | `RECOMMENDED` | a suggestion, never a fact |
Unknowns | `UNKNOWN` + `uncertainties` | missing, stale, undated, insufficient, unsupported or unavailable |

A second opinion about which category a sentence belongs in could disagree with the grounding
layer, and the disagreement would be invisible. Partitioning makes that impossible: a joined
conclusion built from a personal record *and* a market sample is `DERIVED`, so it cannot be
promoted into a fact about the student by sitting next to one.

## 4. Plans

```
horizon → milestone budget:  7 → 2 ·  30 → 4 ·  60 → 6 ·  90 → 8
order within a horizon:      evidence → practice → learn → process
```

Milestone sources, each evidence-linked and each from this turn's tool outputs only:

| Kind | Built from | Evidence |
|---|---|---|
`evidence` | the twin's `biggest_gap_skill` | `KNOWN` |
`practice` | top three `weaknesses.list` rows | `KNOWN` |
`learn` | market-named skills absent from the passport at ≥60% | `DERIVED`, naming the advert sample |
`process` | `applications_total == 0`, unanalysed goals | `KNOWN` |

**Deterministic**: the same outputs give the same plan. **Nothing invented**: a milestone exists
because a tool returned the evidence for it, so an empty account gets the one step its evidence
supports and an explicit reason, not a plausible-looking schedule.

**Horizons are a closed set.** "45 day plan" is recognised as a plan request with an unsupported
horizon and gets a clarifying question — the request was clear, only the horizon is not one we
build. Rounding it to 30 or 60 would hand back a schedule the student did not ask for.

**Time budgets are reported, not resolved.** "10 hours a week" becomes `weekly_hours`, capacity
is `weekly_hours × horizon/7`, and a plan that does not fit says so and **keeps its milestones**.
Quietly dropping steps to fit would read as achievable when it is no longer the plan the
evidence supports. With no stated budget, `fits_time_budget` is `None` — no claim either way.

## 5. Freshness and retrieval

Unchanged from VA-5 and VA-6; VA-8 adds which coaching intents reach it.

`CAREER_PLAN` queries `world.get_role_requirements` **only when a role is named** —
`_world_arguments` returns `None` otherwise, and the plan is then built from personal evidence
alone and says so. Retrieving requirements for a role nobody named would shape a student's next
90 days around a job they never mentioned.

| State | How it reads |
|---|---|
Fresh | a `LIVE_OBSERVED` claim with provider and retrieval time |
Stale / undated | an uncertainty: "could not be confirmed as recent" — never "fresh", never "stale" for an undated advert |
Insufficient sample | VA-5's own reason; no percentage |
Provider unconfigured | a redacted failure; **never** an empty "no jobs found" |
Provider failed | a redacted failure, with the personal side of the answer intact |
Unsupported capability | `LEARNING_RESOURCES` remains declared unsupported |

`CoachingReport.freshness_notes` names provider, freshness and retrieval time, and **only for
evidence that has a provider** — a personal database row has no freshness policy, and attaching
one would imply a retrieval that never happened.

## 6. The HTTP boundary

`POST /coach/ask` → `{text, session_id?, intent_hint?}` → `CoachingTurn.to_payload()`.

This is the provider-neutral integration point. A future speech path supplies a transcript here
and speaks the report's sentences back; neither end needs this route to change.

- Identity comes from the bearer token through the same `get_current_user` every other route
  uses. No body field can influence it.
- `extra="forbid"`: an invented `profile_id`, `user_id` or `tool` is a 422.
- `intent_hint` is validated against the closed `Intent` enum, and an intent maps to a fixed
  set of read tools — so supplying one chooses a question, never a capability.
- A session id is **not a credential**: `MemoryStore.for_session` checks the authenticated user
  and a mismatch is a 404, because confirming the conversation exists is the disclosure.
- The catalogue is `build_default_registry()` — read tools only. The read-only guarantee is a
  property of the catalogue, not of the orchestrator remembering which method to call (it uses
  `invoke_read` as well; both, deliberately).
- Bounded by `TurnBudget` per turn and `60/hour` per account.

## 7. Memory

Unchanged. VA-6's semantics stand: session-scoped, in-process, `USER_STATED` facts only,
`MemoryScope.DURABLE` still refused, nothing persisted, no migration. VA-8 added no persistent
memory behaviour and no new memory kind.

## 8. Not in VA-8

| Item | Phase |
|---|---|
Speech input, transport, speech output, spoken conversation | VA-9 and the VA-3 provider gate |
Voice-interview integration | VA-9 |
Write actions from a coaching turn | VA-7's path, reached deliberately and never by the agent |
Security/privacy/observability hardening, rate-limit tuning | VA-10 |
Physical-device acceptance | VA-11 |
Final audit | VA-12 |
Durable preferences | needs an approved persistence design |
LLM and speech provider selection | VA-3's evidence gate |
Live job provider | owner decision: credentials + approved terms |

## 9. Known limitations, still open

- **No spoken conversation.** No STT, no TTS, no transport wiring, no speech provider selected.
  The realtime protocol is untouched and a test asserts it gained no agent or coaching messages.
- **No live job retrieval has ever happened.** The default provider is `NullJobProvider`; every
  retrieval test uses a fake, and a fake proves the contract and the failure behaviour, not that
  a real provider returns what we expect.
- Provider logging/retention **unknown** where official terms are unverified.
- The VA-2 session registry and the agent memory store are both **process-local — no
  cross-worker recovery**. The VA-7 confirmation store has the same property.
- Live Android↔FastAPI socket: **NEEDS TEST**. Physical-device audio: **NEEDS TEST**.
- Linux eSpeak TTS unproven.
- `_STATUS_RANK` remains unreachable through its single writer; semantics unchanged.
- The interview flow remains on its file-based path.
- Analysed career goals are exercised only by unit tests with synthetic outputs: making one
  analysed in a database test requires a JobDescription and an analysis run, so the API tests
  cover the unanalysed path, which is the one where the coach must state its limitation.
- No Android work in VA-8, so the VA-7 confirmation surface remains unmounted and the tracker
  screens keep their existing direct-save behaviour.
