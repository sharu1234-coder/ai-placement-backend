"""The Placement OS Tool Registry (VA-4).

What this is: a catalogue of named capabilities a future orchestrator may *request*, each with
a typed input schema, a bounded output schema, server-derived identity and an explicit
classification. VA-4 registered **read-only** tools only; VA-7 added two low-risk, reversible
write tools (`jobs.save`, `jobs.unsave`) behind a separate `invoke_action` boundary that
requires a server-minted single-use confirmation, an `Idempotency-Key` and an authoritative
post-execution read. They are absent from `build_default_registry()` unless a caller passes
`include_actions=True`; see docs/safe-actions.md.

What this is not, and must not become:

* **A second authorization system.** Identity comes from the existing bearer-token path
  (`app.api.deps.authenticate_access_token`), not from a copy of it. A tool cannot be handed a
  user or profile identifier -- the caller supplies a token and the server derives the rest.
* **A database door.** Tools call existing domain services. No tool composes SQL, and no tool
  hands back an ORM object a caller could mutate.
* **An LLM adapter.** Nothing here imports a provider SDK or knows a function-calling payload
  format. The orchestrator that translates between the two does not exist yet, and the shape
  it will need is documented in `docs/tool-registry.md` rather than guessed at in code.
* **Agent orchestration.** The VA-2 gateway is unchanged and still performs no tool calling,
  and the VA-6 orchestrator builds the default (read-only) catalogue, so it cannot reach a
  write tool at all. No spoken request executes an action.

The reason for the hard line on identity: a recogniser's output reaches this layer as text. If
a tool took a profile id as an argument, a mis-transcription or a prompt injection would be a
cross-account read. So the argument does not exist.
"""
