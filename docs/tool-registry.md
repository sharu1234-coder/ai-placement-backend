# Tool Registry (VA-4) — read-only

**Status: 13 read-only capabilities. No write tools, no orchestrator, no LLM.** The VA-2 voice
gateway is unchanged and still performs no tool calling.

## What a tool declares

`app/tools/contracts.py`. Every field exists because otherwise a reviewer would have to infer
it from a handler's body.

| Field | Why |
|---|---|
`name` | Stable `domain.verb_noun`. It appears in prompts, transcripts and logs, so renaming one is a breaking change |
`kind` | `READ` / `ACTION` / `NAVIGATION`. Enforced, not documentation — see below |
`input_model` | A pydantic model with `extra="forbid"` |
`output_model` | Purpose-built, field by field |
`requires_premium` | Server-side entitlement, checked before arguments are parsed |
`side_effects` | Human-readable. A `READ` tool must declare none |
`idempotency` | `natural` / `key_required` / `not_idempotent` |
`verification` | `none` / `user_confirmation` (VA-7) |
`max_items`, `timeout_seconds` | Bounded output and a wall-clock budget |

**`READ` is enforced at construction.** A `READ` tool that declares a side effect, a
non-natural idempotency or a confirmation requirement raises `ValueError`. Without that,
"read-only" would be a naming convention and the registry would have no basis for running a
tool without confirmation. Conversely an `ACTION`/`NAVIGATION` tool *must* declare both a
verification requirement and a real idempotency policy — so VA-7 cannot add one that forgot to
say whether it is safe to retry.

## Authorization

**Identity is derived from a token, never accepted as an argument.**
`ToolContext.authenticate(token, db)` delegates to `app.api.deps.authenticate_access_token` —
the same function the HTTP routes and the VA-2 WebSocket gateway use. A third implementation
would be a third set of rules to keep in step, and the one that drifted would be a bypass no
test of the other two could see.

No tool accepts `profile_id`, `user_id`, `email` or any other owner identifier, and a test
asserts that across the whole catalogue. This is the point: the caller will eventually be a
language model reading a student's own words back to the server, so a mis-transcription or a
prompt injection must not be able to name a different account. There is no comparison here to
get wrong because there is no argument to compare.

`extra="forbid"` means an invented field is **rejected, not ignored**. From the caller's side
"ignored" and "honoured" look identical, so a model probing for a way in would keep trying.

**Ownership on resource ids** is enforced by the domain service that already filters on
`profile_id`. A resource belonging to someone else returns the same "not found" as a
non-existent one — a different answer for the two would let a caller enumerate which ids exist.

**No profile is ever created.** The HTTP routes call `get_or_create_profile`, which writes.
That is fine for a request a user initiated and wrong for a read, so the registry looks the
profile up read-only and raises `ProfileNotReady` when there is none.

**Enumeration requires a context.** The catalogue is a map of what the product does with a
student's data; there is no anonymous listing. A premium-only tool is *omitted* for a free
account rather than listed as locked — a locked entry in a model's tool list is one it will
try, fail and have to explain, and the paywall belongs in the app's own UI.

## The inventory

All 13 are `READ`, no side effects, natural idempotency, no verification, none premium-gated.

| Capability | Arguments | Service it calls | Returns |
|---|---|---|---|
`profile.get_summary` | none | `profile` row + `Role` lookup | name, college, degree, branch, year, experience level, target role, onboarding flag |
`skills.get_passport` | none | `skill_passport_service.list_skills` | ≤20 skills: level, confidence, band, state, last verified |
`resume.get_latest_summary` | none | `resume_service.get_latest_resume` | has_resume, parse status, uploaded at |
`resume.list_ats_checks` | none | `ats_check_service.list_ats_checks` | ≤10 checks: score, category, date |
`career_goals.list` | none | `career_goal_service.list_goals` | ≤20 goals: title, company, status, analysed, application status |
`career_twin.get_readiness` | none | `career_twin_service.get_current` | coverage %, estimate flag, biggest gap, next action hint, application counts |
`career_path.get_current` | none | `career_path.service.get_current` | role, primary flag, generated via, phase and gap **counts** |
`jobs.list_saved` | `status?` | `job_hunt.service.list_saved_jobs` | ≤20 jobs: title, company, location, status, applied at |
`interviews.list_history` | none | `interview_service.list_interviews` | ≤20: type, mode, status, overall score, date |
`weaknesses.list` | none | `weakness_record_service.list_for_profile` | ≤20: skill key, status, occurrences, last score, last seen |
`jobs.get_preparation_plan` | `job_description_id` | `jd_service.get_preparation_plan` | found, job title, company, gap count, top 5 gaps |
`missions.get_today` | none | scoped ORM read (see below) | today's mission or `has_mission: false` |
`progress.get_summary` | none | `progress_service.get_dashboard` | performance score, counts, roadmap progress, top 5 weaknesses |

**`missions.get_today` is the one tool that queries the ORM directly**, and the reason is the
read-only guarantee. `mission_service.get_or_create_today` — what the HTTP route calls —
**inserts and commits** a Mission when today has none. A `READ` tool doing that would be a
write tool with a misleading label. So this is a scoped read filtered by the authenticated
profile and today's date, with no path by which a caller could influence the filter.

## Data minimisation

Outputs are built field by field rather than reusing the API response schemas, because those
carry things a spoken assistant has no business with, and reusing them would expose every field
a future UI feature adds. What was deliberately left out:

| Dropped | From | Why |
|---|---|---|
`email`, `mobile_number` | profile | A contact detail read aloud in a shared room is a disclosure the feature does not need |
`filename`, `file_size_bytes` | resume | A filename is user-chosen and routinely carries a real name |
`url`, `notes` | saved jobs | A URL is useless spoken; notes are the student's own free text |
`reasons` | career twin | The field the HTTP route gates behind Premium. Not exposed at all, so a free account cannot reach it through a second door |
`evidence`, `next_action_hint` | skill passport | A per-skill nested structure meant for a screen |
`phases`, `skill_gaps` contents | career path | Counts instead; the full structures would exceed the size ceiling |

A test asserts no output model — or nested model — carries a field named like a credential or a
contact detail, so a field added later is caught even when today's fixtures leave it empty.

Free-text fields are clipped (`_clip`) and lists capped, with `truncated` reported so a caller
can say "and more" instead of presenting a cut-off list as complete. The registry also enforces
a 20,000-character ceiling on one serialised result: a bounded schema bounds the shape, not the
bytes.

## Errors

| Code | Meaning |
|---|---|
`unauthenticated` | Missing, malformed, expired, revoked, or a refresh token. One opaque message for every cause |
`unknown_capability` | Names what was requested, never what is registered — a model guessing must not be handed the catalogue |
`invalid_arguments` | Field paths and error types, **never the submitted values** |
`permission_denied` | Entitlement, or a non-`READ` tool reached through `invoke_read` |
`profile_not_ready` | Authenticated, but no profile row yet. Not an error to paper over |
`timeout` / `cancelled` | See below |
`execution_failed` | `safe_message()` is "<tool> could not be completed". The cause is logged, not returned |

`execution_failed` is redacted because a SQLAlchemy error's text routinely carries the failing
statement, and handing that to a model that may read it aloud or echo it into a prompt turns an
internal fault into a schema disclosure.

**Logging** records the capability name and the user id. Never arguments, never results — this
line runs on every call, and both are derived from a student's own data.

## Timeout and cancellation — what they actually guarantee

Cooperative, and honest about it. A synchronous database call cannot be interrupted from
Python mid-statement, so:

- **Cancellation** guarantees a cancelled call does not *start*, and that a multi-step handler
  stops between steps.
- **The deadline** is detected after the handler returns. The overrun is reported, not
  pre-empted.

Claiming otherwise would be the dishonest part. Pre-emption would need the work to move to a
cancellable worker, which is a deployment-shaped change.

## The orchestrator seam — defined, not built

**There is no orchestrator.** The VA-2 gateway authenticates, negotiates audio format and
carries bytes; it emits no transcript, no generation and no tool call. VA-3 built the speech
contracts but selected no provider. So the chain from speech to tool does not exist, and VA-4
did not pretend otherwise.

What a future orchestrator will do, and what is already in place for it:

1. **Discover**: `registry.describe_for(context)` → a list of plain dicts, each with a JSON
   Schema. Provider-neutral by construction; translating into one vendor's function-calling
   format is the orchestrator's job, and doing it here would bind the registry to whichever
   vendor was chosen first.
2. **Call**: `registry.invoke_read(name, arguments, context)` → a `ToolResult` whose
   `to_payload()` is JSON-ready. `invoke_read` refuses anything not classified `READ`; it is a
   separate method rather than a flag because a flag defaults to something, and the safe
   default is the one a caller cannot forget to pass.
3. **Report**: the VA-2 client *already parses* `tool_call_started` — it is in the Kotlin
   decoder and therefore in `SERVER_MESSAGE_TYPES`. It is **not** in
   `SUPPORTED_SERVER_MESSAGE_TYPES`, so the gateway still emits nothing of the kind. A test
   asserts both halves, so the day the gateway starts claiming orchestration is a day a test
   fails rather than a day nobody notices.

**No protocol change was needed and none was made.** The message type the eventual
orchestrator needs was already in the contract, and the registry does not import `app.voice`,
`app.api.voice_session`, `app.ai.provider` or `app.speech` — asserted by tests.

## Deferred

| Item | Phase |
|---|---|
Write/action tools, confirmation, idempotency keys, verification | **done in VA-7** (`jobs.save`, `jobs.unsave`) — see docs/safe-actions.md |
Agent reasoning, memory, persistent conversation state | later |
World-intelligence providers | later |
LLM selection, streaming STT/TTS | later (VA-3 selected no provider) |
HTTP exposure of the registry | not required by VA-4; the registry is called in-process |
Live Android↔FastAPI socket, physical-device audio, end-to-end spoken conversation | **NEEDS TEST** |
