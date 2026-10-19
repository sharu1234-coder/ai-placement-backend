# Safe app actions: confirmation, idempotency and verified outcomes (VA-7)

**Status: two low-risk, reversible write tools. No high-impact action is registered. No voice
integration. Nothing persisted beyond the existing `operation_claims` table.**

## 1. The flow

```
POST /actions/preview            POST /actions/execute
  capability + arguments            capability + arguments + confirmation_token
                                    Idempotency-Key: <required>
  1 resolve name -> ACTION only    1 resolve name -> ACTION only
  2 entitlement                    2 entitlement            (again, not inherited)
  3 argument schema, extra=forbid  3 argument schema        (again)
  4 cancellation                   4 cancellation
  5 tool.preview(live state)        5 Idempotency-Key present
      -> ownership check            6 consume confirmation  (single use, burned here)
  6 mint single-use confirmation    7 tool.preview(live state) again
                                        -> ownership + business rules re-evaluated
                                        -> resource identity compared with the grant
                                    8 already satisfied? -> verify, no write
                                    9 idempotency.run_once -> existing domain service
                                   10 tool.verify() -> authoritative read
  <- description, token,            <- state, verified, detail
     resource, risk, expiry
```

Every refusal that can be made without touching the database is made before it is; every
refusal that can be made without running the handler is made before it runs. Both steps share
one `_prepare_action`, because a preview applying a weaker rule than the execute is the gap
worth attacking, and two copies of five checks is how that gap appears.

## 2. The action implemented, and why

`jobs.save` and `jobs.unsave`. The smallest real mutation the product has: reversible by the
student in one tap, affecting only rows they own, touching no money, no credentials, no account
state and nothing another person can see. Proving the boundary on a dangerous action teaches
nothing extra, and a mistake while building it here costs a tracker row.

**Not a second implementation.** Both call `app/services/job_hunt/service.py`'s existing
`save_job` and `delete_saved_job`, the same functions `POST /job-hunt/saved` and
`DELETE /job-hunt/saved/{id}` call. What VA-7 adds is the part the screen-driven route does not
need: a preview the student sees, a confirmation bound to it, and a verified outcome. The
existing routes are untouched -- a student tapping Save has already confirmed by tapping, and
asking twice would be worse, not safer.

**Separate transitions, not a toggle.** A toggle is the wrong shape for anything retryable: a
duplicate "flip it" undoes the first submission and reports success while leaving the student
with the opposite of what they asked for. Each tool names its target state, so a repeat is a
no-op.

**`jobs.save` does not pass a status through.** `save_job` also writes status, and a repeat save
on a job the student has moved to `interview` would rewrite their own pipeline back to `saved`
-- real history lost to an idempotent-looking retry. So a job already tracked at any status
other than `dismissed` is reported as already satisfied and **nothing is written**. Changing a
pipeline status is a different intent and will be its own action with its own sentence.

**Arguments are `listing_id`, not `saved_job_id`.** One identity in both directions, which lets
save and unsave share a resource key and keeps the tracker's row id internal.

## 3. Confirmation

A confirmation is not a boolean. `confirmed=true` in a request body is supplied by whoever sent
the request, which on this path eventually means a language model relaying a student's words.
What the server accepts is a token it minted, bound to everything that made the preview true:

| Bound to | Why |
|---|---|
Authenticated user id | a grant cannot be carried to another account |
Exact capability name | a grant for a save cannot execute an unsave |
Canonical argument fingerprint | changing an argument after the preview invalidates it |
Resource key (`job_listing:41`) | a grant for one job cannot execute against another |
`expires_at` (120 s) | approval of a state of affairs goes stale |
Single-use consumption | a replayed confirmation cannot cause a second mutation |

The token reads `<confirmation_id>.<secret>`: the id is an index, the secret is compared in
constant time against a stored SHA-256 digest. The token itself is never stored, so a memory
dump of the store yields no usable approval, and it is returned exactly once -- in the response
that minted it.

A **failed** attempt does not burn the grant. Burning it would let a wrong-capability request
invalidate the student's pending approval, turning a refused call into a denial of their own
action. A **successful** validation burns it before the handler is entered, under a lock, so a
request racing it finds nothing to spend.

**The store is in-process.** 120 seconds of pre-execution state, deliberately not a table and
not a migration -- the same judgement VA-6 made about memory: a durable store is a retention
surface, and an approval that outlives its screen is not safer for being persisted. The cost is
real: **with more than one worker, a preview and its execute must be served by the same worker,
or the execute is refused and the student must confirm again.** The guarantee that genuinely has
to survive a worker -- "this mutation happens once" -- is not here; it is the unique constraint
on `operation_claims`, enforced by the database across every worker. A cross-worker confirmation
would need a signed token plus a shared consumption record, and that is a later decision with
its own test, not something to add quietly.

## 4. Idempotency and the state machine

```
REQUESTED -> AUTHORIZED -> CONFIRMATION_REQUIRED -> CONFIRMED -> EXECUTING
                                                                    |
            SUCCEEDED / FAILED / IN_PROGRESS / UNKNOWN / VERIFICATION_FAILED
```

`Idempotency-Key` is **required**, not optional, for an action declaring `KEY_REQUIRED`. A key
minted server-side would be unique to that request, so the claim row it created could never
match the duplicate it exists to collapse -- the mechanism would be present and would protect
nothing.

Execution runs inside the existing `app/services/idempotency.py` `run_once`, with
`operation="tool.<capability>"` and the validated arguments as params. No parallel idempotency
service, and no second state store.

`result_id` is recorded as `None` on purpose. A replay is answered from the authoritative read
rather than from a recorded row id, which is the only answer that stays true for an action whose
durable effect is a **deletion** -- there is no row left to point at, and a mechanism needing
one would have to pretend otherwise.

| Situation | Result |
|---|---|
Same key, same request, already settled | not re-run; `replayed=true`, then verified |
Same key, different request | `IdempotencyConflictError` -> 409, nothing runs |
Duplicate still in flight | `IN_PROGRESS`; the second mutation never starts |
Handler raised | reconciled from the authoritative read (below) |
Cancelled before execution | `ToolCancelled`, nothing written, grant intact |

Cancellation is honest about its granularity: it guarantees a cancelled call does not *start*.
A synchronous write already in progress cannot be interrupted from Python, and nothing here
claims it was rolled back.

## 5. Verification is authoritative

`ToolSpec.verify` is a required declaration for every non-READ tool and is run **by the
registry**, not inside the handler, so the registry can refuse to report success without it. A
handler that returned a success-shaped output is not evidence; neither is an HTTP 200, nor an
ORM object the write is still holding -- that object is the write's own view of itself, which is
exactly the evidence that cannot distinguish a commit from a rollback.

`ActionOutcome.__post_init__` makes the dishonest payloads unconstructible: `verified=True` is
only valid with `SUCCEEDED`, and `SUCCEEDED` is only valid with `verified=True`.

After a failed attempt, the authoritative read decides between three answers and no fourth:

| Read says | Outcome | Reasoning |
|---|---|---|
the state was reached | `SUCCEEDED`, verified | it committed despite the error; reporting failure would invite a retry of something that happened |
the state was not reached | `FAILED` | nothing durable; an explicit retry is a genuine retry |
the read itself failed | `UNKNOWN` | never retried automatically -- a blind retry is how one requested change becomes two |

## 6. What the model may and may not do

It may name a capability, validated against the registry, and supply arguments, validated
against that capability's schema with `extra="forbid"`. It cannot supply identity -- that is
derived from the bearer token by the same `authenticate_access_token` the HTTP routes use -- and
it cannot supply a confirmation, because the only confirmation the server accepts is one it
minted and handed to the student's own client. A model emitting `confirmed: true`, `yes I
confirm` or a plausible-looking token gets a typed refusal.

`invoke` was narrowed to READ in VA-7. Before that, any caller holding the registry could run a
mutation with no confirmation, no claim and no verification, and "the caller remembered to use
the other method" is not a boundary. The three entry points now are:

| Method | Runs |
|---|---|
`invoke_read` | READ only |
`invoke` | READ only |
`invoke_action` | non-READ only, confirmation required |

`build_default_registry()` still returns **read tools only**. The write catalogue needs
`include_actions=True`, asked for in exactly one place (`app/api/actions.py`), so "who can reach
a mutation tool" is one grep away. The VA-6 orchestrator builds the default catalogue and
therefore cannot see these tools at all.

## 7. Risk classification inventory (not implemented)

Recorded so the next phase starts from a list rather than from whatever route is nearest. None
of these is registered, and none should be without its own confirmation sentence, verification
read and test.

| Candidate | Risk | Confirmation | Verification | Notes |
|---|---|---|---|---|
`jobs.save` / `jobs.unsave` | low | **implemented** | re-read tracker row | VA-7 |
Change a saved job's pipeline status | low | explicit, naming both statuses | re-read status | needs its own sentence; must not be folded into `jobs.save` |
Add/edit a saved job's notes or follow-up date | low | explicit, showing the new text | re-read field | free text: must be stored, never interpreted |
Save or delete a saved search | low | explicit, naming the search | re-read by id | deleting one leaves its provenance dangling by design |
Create or delete a job alert | medium | explicit, naming cadence | re-read alert | quota-bearing; schedules future outbound work |
Mark a mission or roadmap step complete | medium | explicit | re-read progress | feeds scores a student is judged on; a wrong one is visible and demoralising |
Start an assessment or mock interview | medium | explicit, naming the cost | re-read attempt | spends a free-use slot and AI quota |
Upload or replace a resume | medium | explicit, naming the file | re-read latest | file bytes; never fingerprinted by content in a log |
Edit profile fields (college, branch, target role) | medium | explicit, old -> new | re-read profile | re-derives matching and recommendations |
Delete an interview, assessment or project | high | typed/second-factor confirmation | re-read absence | irreversible loss of the student's own history |
Subscription purchase, upgrade, cancel | high | **not via this path** | provider webhook | money; must stay in the app's own purchase flow |
Account deletion, email or password change | high | **not via this path** | out of scope | account-level; requires re-authentication |
Anything writing another user's rows | — | **never** | — | no such capability may exist |

VA-7 deliberately adds no route to the registry merely because it exists.

## 8. Known limitations

- **No voice integration.** The VA-2 gateway carries audio frames and does not call the VA-6
  orchestrator; the orchestrator executes read tools only and cannot see these capabilities. A
  spoken "save that job" does not reach this path. **End-to-end voice-action execution is not
  implemented and not claimed.**
- **The confirmation store is process-local** -- no cross-worker preview/execute, as in §3. The
  VA-2 session registry and the VA-6 memory store have the same property.
- **Live device and network checks: NEEDS TEST.** Everything proven here is deterministic and
  offline, through the app's own test client. No physical-device run of the confirmation dialog
  has been performed.
- `UNKNOWN` requires a human decision by design. There is no background reconciler, and adding
  one would need its own authorization story.
- The Android `ActionConfirmationHost` is a reusable surface and is **not yet mounted on any
  screen**; mounting it is a UI decision per screen, and the existing tracker screens keep
  their current direct-save behaviour.
