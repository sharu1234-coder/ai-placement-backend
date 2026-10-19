"""Agent reasoning, context and memory (VA-6). Read-only.

The orchestrator sits above VA-4's Tool Registry and VA-5's World Intelligence and below
whatever eventually drives it. Its job is narrow: work out what was asked, choose a few
approved read tools, run them through the registry, and build an answer in which **every claim
points at a tool result**.

Four rules shape it:

* **Grounded by construction.** A `Claim` cannot exist without at least one `EvidenceRef`, and
  an `EvidenceRef` can only be made from a real `ToolResult` or an explicit `Unknown`. There is
  no code path that produces an ungrounded sentence, which is a stronger guarantee than a
  prompt asking a model to cite its sources.
* **A model is optional, not required.** Intent classification is deterministic and rule-based;
  the LLM is reached only to disambiguate an unclear request or to phrase an answer, and both
  degrade to the deterministic path when no provider can do it. VA-6 is ₹0-first and no
  production model has been selected, so an orchestrator that needed one would not run.
* **Read-only, deterministically.** Tools are discovered through `describe_for` and invoked
  only through `invoke_read`. A name a model proposes is validated against the registry before
  anything executes. Prompt-injection defences are not authorization: the permission checks
  stay in the registry where they are deterministic.
* **Memory is small and honest.** Session state is in-process and bounded; nothing persists, so
  there is no migration and no new retention surface. A model inference is never recorded as a
  verified personal fact.

VA-8 added `coaching.py` and `Orchestrator.coach` on top of all four rules rather than beside
them. A coaching answer is a *partition* of the same turn's claims by evidence kind, so the
coaching surface cannot call something a fact that the grounding layer called a suggestion, and
`Milestone` extends the no-evidence-no-claim rule with one more invariant: a step that promises
an outcome cannot be constructed at all. Still read-only, still no model required, still
nothing persisted. See docs/voice-career-coach.md.

Not here, and assigned elsewhere: write/action execution, confirmation and idempotency (VA-7,
reached through its own confirmed path and never by the agent); speech input, transport and
speech output (VA-9 and VA-3's provider gate); interview integration (VA-9). The VA-2 gateway
is untouched and still performs no orchestration.
"""
