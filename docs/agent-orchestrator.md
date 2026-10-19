# Agent orchestration, context and memory (VA-6)

**Status: read-only. No write tools, no model required, nothing persisted, transport untouched.**

## 1. Data flow

```
AgentRequest
  → intent.classify / intent.extract        deterministic, rule-based
  → [optional] model picks an Intent label  validated against the closed enum
  → planner.plan(intent, entities, available)
        available = registry.describe_for(context)   ← the caller's catalogue
  → registry.invoke_read(name, args, context)        ← the only invocation path
  → grounding.from_personal / from_world             ← claims that name their evidence
  → GroundedAnswer (claims | uncertainties | failures | clarifying_question)
```

Bounded at every step by `TurnBudget`: wall clock, tool calls, rounds. Calls are counted
*before* they run, because counting only what returned is how a failing provider gets retried
six times.

## 2. The four rules

**Grounded by construction.** `Claim(text, evidence=())` raises. An `EvidenceRef` must name the
tool that produced it, and live-observed evidence must additionally name its provider and
retrieval time. There is therefore no code path that emits an ungrounded sentence — a stronger
guarantee than a prompt asking a model to cite sources, because it does not depend on the model
complying.

**A model is optional.** Intent classification is regex-based over a closed `Intent` set. The
LLM is consulted only when the rules return `UNCLEAR`, only to pick a *label*, and its answer is
validated against the enum before it can influence anything. Any model failure — including an
unexpected exception type — degrades to the deterministic path. This is not a placeholder: no
production model has been selected (VA-3's gate is open) and the cost policy is ₹0-first, so an
orchestrator that required an inference would not run here at all.

**Read-only, deterministically.** `invoke_read` is the only invocation method used, and it
refuses anything not classified `READ`. A test registers a real `ACTION` tool in the registry
and asserts across four different requests that its handler never runs.

**Memory is small and honest.** Nothing persists.

## 3. Intent set and tool mapping

| Intent | Tools |
|---|---|
`profile_status` | `profile.get_summary` |
`skill_status` | `skills.get_passport` |
`readiness` | `career_twin.get_readiness` |
`weaknesses` | `weaknesses.list` |
`interview_history` | `interviews.list_history` |
`progress` | `progress.get_summary` |
`resume_status` | `resume.get_latest_summary`, `resume.list_ats_checks` |
`saved_jobs` | `jobs.list_saved` |
`today_plan` | `missions.get_today` |
`job_search` / `fresher_jobs` | `world.find_openings` / `world.find_fresher_openings` |
`role_requirements` / `skill_demand` | `world.get_role_requirements` / `world.get_skill_demand` |
`company_hiring` | `world.get_company_hiring` |
`location_comparison` | `world.compare_locations` |
`skill_gap_vs_market` | `skills.get_passport` + `career_twin.get_readiness` + `world.get_role_requirements` |
`unclear` | none — a clarifying question |
`out_of_scope` | none — an explicit refusal |

**A fixed mapping, not a model's choice**, which is the tool policy as code: what can execute
for a given intent is knowable in advance. `out_of_scope` is matched *before* the read rules, so
"apply to this job" is refused rather than quietly answered as a job search — a request to act
is not a request to look.

`available` comes from `describe_for(context)`, the caller's own catalogue, not the registry's
full set. A premium tool the account lacks is skipped and reported as a limitation; attempting
it would leak its existence through the error.

## 4. Context and memory

| Kind | Lifetime | Origin | Deletion |
|---|---|---|---|
Ephemeral session facts (role, location) | session, in-process | `USER_STATED` only | `forget(key)`, `clear()`, worker restart |
Current-turn evidence | the turn | tool results | discarded at turn end; never carried forward |
Rolling summary (≤ 8 turns) | session, in-process | intents + tool names, **deterministic** | `clear()`, worker restart |
Durable preferences | **not implemented** | — | nothing is written |

Every item records origin, timestamp, scope, confidence and deletion behaviour, so "why does
the agent think this about me?" has an answer.

**A model inference is never a verified fact.** `MemoryItem` raises if a `MODEL_INFERRED` item
claims `KNOWN` evidence, and `remember()` *derives* the evidence kind from the origin rather
than accepting it as an argument — so there is no parameter to misuse. Only `USER_STATED` facts
carry forward between turns, which is why "and in Pune?" resolves against the role the student
actually said rather than one a model guessed.

**The rolling summary is built from intents and tool names, not written by a model.** A
model-written summary is an inference, and an inference that accumulates across turns becomes a
fact nobody verified.

**Nothing persists, deliberately.** Inventing a durable store and a migration to satisfy the
word "memory" would create a retention surface for inferences about a student that no approved
design covers — `MemoryScope.DURABLE` is refused outright with that reason. The cost, stated:
no cross-worker resume, and a restart loses the conversation. The same limitation the VA-2 voice
registry already has, recorded beside it rather than presented as solved.

A session id is **not a credential**: `MemoryStore.for_session` checks the user id and raises
`PermissionError` on a mismatch, so a matching id cannot hand back another user's conversation.

## 5. Evidence states

`known` (verified personal record) · `live_observed` (external, with provenance and freshness) ·
`derived` (computed, carries its sample) · `recommended` (a suggestion, never a fact) ·
`unknown` (we looked and cannot say — a first-class answer).

A claim's `weakest_kind` governs how it reads, so a joined conclusion built from a personal
record *and* a market sample is `derived`, never `known`. "You are missing Kubernetes" is a
statement about one sample of adverts, and the claim says so.

## 6. Personal / world boundary

World arguments are assembled in `planner._world_arguments` from the bounded `Entities` plus a
literal limit. `Entities` has five fields — role, location, locations, skill, experience level —
so there is no path by which a resume, profile, skill list, identifier or token could reach a
provider. The tool's `extra="forbid"` schema is the second line; the first is that nothing else
is in scope.

Unknown places are **not** extracted: an open-ended place extractor would send arbitrary user
text to a provider as a geography filter, and a provider that does not recognise it silently
widens the search instead of narrowing it — which looks like a result about the asked-for city
and is not.

The join happens inside the orchestrator, after both sides have been retrieved through
authenticated, user-scoped tools. Provenance, timestamps, geography and freshness travel from
the VA-5 observation into the claim's evidence rather than being re-derived.

## 7. Untrusted text and injection

The only path from user or external text into a prompt is `_refine_with_model`, and it renders
through the existing `input_guard.render_prompt`. A test feeds a delimiter-forgery payload and
asserts the markers are filtered and the prompt's own `<<<REQUEST_END>>>` appears exactly once.

**Prompt-injection defences are not authorization**, and nothing here relies on them for it:
tool enumeration, entitlement, ownership and the READ restriction are all deterministic and all
live in the registry. A model that answers with a capability name resolves to no intent at all.

## 8. Failure semantics

| Condition | Result |
|---|---|
Tool not available to this account | recorded in `failures`, other calls continue |
World provider unconfigured / out of quota / down | redacted `failures` entry; **never** an empty "no jobs found" |
Truncated tool result | an uncertainty saying the list was cut |
Insufficient market sample | an uncertainty with VA-5's own reason; no percentage |
Cancellation | `TurnCancelled` — translated from `ToolCancelled` so the contract holds |
Clock or call ceiling | `TurnTimeout` / `TurnLimitReached`, or a stated uncertainty mid-plan |
Model unavailable | silent fallback to the deterministic path |

A partial answer is preferred to a failed turn when some tools succeeded, and the gaps are
listed rather than omitted.

## 9. Not in VA-6

| Item | Phase |
|---|---|
Write/action tools, confirmation, idempotency, action verification | **done in VA-7**, and still unreachable from here: the orchestrator builds the default read-only catalogue |
Voice career coaching | **done in VA-8** (coaching layer + `POST /coach/ask`; still no speech) -- see docs/voice-career-coach.md |
Voice-interview integration | VA-9 |
Security/privacy/observability hardening | VA-10 |
Physical-device acceptance | VA-11 |
Final audit | VA-12 |
Durable memory / preferences | needs an approved persistence design |
Transport wiring (STT → orchestrator → TTS) | later; the gateway is untouched and still emits no `tool_call_started` |
LLM and speech provider selection | VA-3's evidence gate |
Live job provider | owner decision: credentials + terms |
Embeddings, reranking, RAG, document parsing | separately authorised |

## 10. Known limitations, still open

- World provider **not configured by default**; no live retrieval has occurred.
- Provider logging/retention **unknown** where terms are unverified.
- VA-2 session registry and this memory store are both **process-local — no cross-worker
  recovery**.
- Live Android↔FastAPI socket: **NEEDS TEST**. Physical-device audio: **NEEDS TEST**.
- **End-to-end spoken conversation has not been exercised** and is not claimed: there is no
  STT → transport → orchestrator → TTS path, because no speech provider is selected and the
  gateway is not wired to the orchestrator.
- Linux eSpeak TTS unproven.
- `_STATUS_RANK` remains unreachable through the single writer; semantics unchanged.
- The interview flow remains on its file-based path.
