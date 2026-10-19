# Security and reliability hardening (VA-10)

**Status: an audit of the guards built in VA-0.5 through VA-9, five defects found and fixed,
and a mutation programme proving the critical guards are load-bearing. No provider was enabled,
no credential created, no dependency installed, and nothing about the spoken pipeline changed.**

This phase did not add a feature. It read the guards, attacked them, fixed what was broken, and
wrote down what remains untestable without a provider or a device.

## 1. What was found

Five defects, all in code written by earlier phases of this project, all found by reading the
guard rather than by a test failing.

### 1.1 The output guard never flattened whitespace — medium

`app/ai/output_guard.py` shipped `normalize_whitespace` with a docstring stating it exists "so
marker checks aren't defeated by a line-wrapped phrase (e.g. `security\nrule`)". It had **no
caller anywhere in the application**. All ~20 guarded fields were matched against raw text, so a
single newline inside any multi-word marker defeated the whole phrase list:

| text | before | after |
|---|---|---|
`ignore the security rule above` | caught | caught |
`ignore the security\nrule above` | **missed** | caught |
`reveal the system\n  prompt` | **missed** | caught |
`these\ninstructions say` | **missed** | caught |

Wrapped text is the ordinary shape of model output, and the module exists because a live model
was observed echoing this application's own prompt text into a generated field. The longer the
marker phrase, the likelier a wrap falls inside it — so the bypass was most available for
exactly the markers that matter most.

**Fix:** flatten inside `looks_like_prompt_leak`, not at the call sites, so no present or future
caller can forget. Both the flattened and the raw form are checked for forged delimiters,
because flattening cannot repair a delimiter broken mid-token while the raw form cannot match a
phrase broken across a line. `leaking_field`'s recursive walk inherits it for free.

The false-positive side was re-proven, including the adversarial case for the fix itself:
`"Rule one: write the test first.\nSecurity matters, so validate every input."` contains both
words, wrapped, and is not flagged.

### 1.2 One account could evict every other account's pending approval — low

`ConfirmationStore` capped itself at 256 pending grants and evicted **the globally oldest**. A
client previewing in a loop therefore pushed out every other student's pending approval. Not a
data leak and not an unauthorised mutation: a student looking at a confirmation dialog was told
their approval had expired because somebody else was busy.

**Fix:** a per-account quota (`MAX_PENDING_PER_USER = 16`) enforced on issue, and global
eviction that takes from **whoever holds the most** rather than from whoever is oldest. A
student with one pending approval now keeps it while a flooder loses one.

### 1.3 Resume grants had no bound at all — low

`SessionRegistry._grants` was unbounded. Grants deliberately outlive their sessions — that is
what makes resume possible — so the per-user session cap (3) never bounded this map. An
authenticated client that connected and dropped in a loop accumulated one grant per connection
for the whole 300-second TTL window.

**Fix:** `DEFAULT_MAX_RESUME_GRANTS_PER_USER = 8`, enforced per account on issue, newest kept.
Per account rather than global, so a noisy client cannot evict anyone else's recovery option.

### 1.4 A cancellation arriving mid-call burned the student's approval — low

Stated precisely, because the first version of this finding overstated it and the mutation
programme is what caught that: an *already* cancelled call was always safe. `_prepare_action`
checks cancellation at stage `preparing`, before any approval is touched, so a cancel set before
the call was refused with the grant intact.

The unsafe window was narrower. `invoke_action` consumed the confirmation and only then checked
cancellation at `about to execute`, so a cancellation arriving *between* those two points — a
cancel button pressed while the request was in flight, a voice session dropping — spent an
approval on a call that did nothing. The student had to approve the same sentence twice, and
anyone able to trigger a cancellation could deny them their own action. VA-7 had already reasoned
that a *failed* confirmation attempt must not burn the grant for exactly this reason; the
concurrently-cancelled case was the same argument unapplied.

**Fix:** the check moved ahead of consumption. That is also the last honest place for it: past
that line the approval is spent and the claim is being taken, which is exactly the granularity
`CancellationToken` documents — a cancelled call does not *start*.

The first test written for this passed with the fix reverted, because it cancelled before the
call and so exercised `_prepare_action`'s older guard. It is kept, renamed to say what it
actually covers, and a second test uses a `CancellationToken` that arms itself after the first
checkpoint — which makes the real window deterministic rather than a thread race. Both
directions are also tested: a cancelled call never reports success, and a cancellation arriving
mid-write never disowns a mutation that committed.

### 1.5 A guessable session id could lock a student out of coaching, and told the caller it existed — low

`POST /coach/ask` keyed the agent memory store on the client-supplied `session_id`, defaulting
to `f"turn-{user_id}"`. The ownership check inside `MemoryStore.for_session` correctly refused
to hand over another account's conversation — so there was no leak — but it refused with a 404,
and that had two consequences the refusal was not meant to have:

- **Denial.** Whoever claimed an id owned it. The default key is derived from the victim's own
  user id, so it is trivially guessable, and every coaching request they made without supplying
  an id of their own then returned 404. They had no way to avoid the collision.
- **A weak existence oracle.** A 404 rather than a working request told the caller that the id
  was in use by *somebody*, which is the kind of thing the 404 was chosen to avoid disclosing.

**Fix:** `MemoryStore.for_session` namespaces the key by the authenticated user id, so a
cross-account collision is impossible by construction rather than caught by a check: the second
caller gets its own conversation, learns nothing, and the first keeps theirs. The check is kept
as the invariant's last line and still tested directly, by planting the record a mis-built key
would produce. Two existing tests asserted the old 404; both now assert the stronger property
and say in their docstrings why the refusal was the weaker guarantee.

## 2. Threat matrix

Every row names the test that proves it. "Existing" means a prior phase built the guard and its
test, and VA-10 verified the test is load-bearing by mutation rather than re-writing it.

| # | Attack | Boundary | Expected | Evidence |
|---|---|---|---|---|
1 | Missing / malformed / expired / wrong-type credential | HTTP + WS | 401, opaque | existing: `test_auth.py`, `test_voice_session_ws.py::TestAuthentication`; mutants M03, M04, M23 |
2 | Token issued before a logout | HTTP | 401 | existing `token_version` check; mutant M04 |
3 | Refresh token used to open a voice session | WS | closed 4401 | existing `test_a_refresh_token_cannot_open_a_voice_session`; mutant M03 |
4 | Token in the query string | WS | not authenticated | existing `test_a_token_in_the_query_string_does_not_authenticate` |
5 | User A reads/alters B's interview, question, listing, goal, session, confirmation | all | 404, existence undisclosed | existing per-feature isolation suites; **new** `TestOwnershipGuardsAreLoadBearing`; mutants M01, M02, M06 |
6 | Question from the student's *other* interview | service | refused | **new** `test_a_question_from_the_students_other_interview_is_refused` (M01) |
7 | Premium tool discovered or invoked by a free account | tool registry | omitted from the catalogue, refused at execution | existing `TestEntitlement`; mutants M05, M05b |
8 | Model names a tool, or asks for an action | agent | ignored / refused, no mutation | existing `test_a_model_naming_a_tool_cannot_make_it_run`, `test_an_action_tool_is_never_planned_or_invoked` |
9 | ACTION tool driven through `invoke` / `invoke_read` | tool registry | refused | existing `TestReadAndWritePathsStaySeparate`; mutant M11 |
10 | Forged delimiter in untrusted input | prompt render | neutralised | existing `input_guard`; mutant M20 |
11 | Model echoes prompt scaffolding, wrapped across a line | AI output | never reaches or is persisted for the student | **new** `TestWrappedMarkers`, `TestLeakedOutputAtTheRealBoundary`; mutants M18, M19 |
12 | Malicious transcript / job text | interview, coach | stored as data, changes nothing | existing `test_a_malicious_transcript_is_stored_as_data_and_changes_nothing`, `test_job_hunt_untrusted_text.py` |
13 | Confirmation presented by another account | action | refused, grant intact | existing; mutant M06 |
14 | Confirmation reused for another capability or argument | action | refused, grant intact | existing; mutants M07, M08 |
15 | Confirmation replayed | action | refused | existing; mutant M09 |
16 | Resource substituted between preview and execute | action | refused | existing; mutant M10 |
17 | Idempotency key reused for a different request | action | 409 | existing; mutant M12 |
18 | Duplicate request while the first is in flight | action | in-progress, one mutation | existing; mutant M13 |
19 | Lost response, then a retry | action | reconciled from authoritative read, never blindly re-run | existing `TestOutcomesAreHonest`; mutants M14, M15 |
20 | Handler committed then failed | action | SUCCEEDED, verified | existing `test_a_failure_after_the_mutation_committed_reconciles_to_succeeded` |
21 | Verification unreadable | action | UNKNOWN, no auto-retry | existing `test_an_unreadable_verification_is_unknown_not_success_or_failure` |
22 | Cancel racing authorization | action | nothing minted | **new** `test_cancelling_before_authorization_mints_no_approval_at_all` |
23 | Cancel racing confirmation consumption | action | refused, **approval survives** | **new** `test_a_cancelled_action_does_not_burn_the_students_approval` (M26) |
24 | Cancel racing handler entry | action | mutation not disowned | **new** `test_cancelling_during_the_handler_does_not_disown_a_committed_mutation` |
25 | Cancel racing verification | action | outcome not falsified | **new** `test_cancelling_during_verification_does_not_falsify_the_outcome` |
26 | Same answer submitted twice | interview | one row, advances once | existing VA-9 `TestReplayAndDuplicates`; mutant M16 |
27 | Different answer for an answered question | interview | refused, nothing advances | existing; mutant M16 |
28 | Concurrent submissions for one question | interview | one row, gated and proven to interleave | existing `TestConcurrentTurns`, `test_answer_result_replay.py` |
29 | Stale result from an earlier question | interview | cannot answer a newer one | existing `test_a_stale_callback_for_an_earlier_question_cannot_answer_a_newer_one` |
30 | Evaluation claimed ready before completion | interview | `not_ready`, 409 from `/evaluation` | existing; mutant M17 |
31 | Resume grant from another account | WS | indistinguishable from unknown | existing `test_another_users_token_is_useless`; mutant M21 |
32 | Resume grant replayed | WS | single use | existing `test_a_token_is_single_use`; mutant M22 |
33 | Unknown resume token | WS | healthy session survives | existing `test_an_unknown_token_does_not_kill_a_healthy_session` |
34 | Oversized control frame / audio frame / session audio | WS | closed 1009 | existing `TestMessageHandling`, `TestLifecycle` |
35 | Unparseable frame, unknown message type | WS | closed 1008 | existing |
36 | Idle session | WS | expired and cleaned up | existing `test_the_session_audio_budget_is_bounded`, idle timeout |
37 | Concurrency limit, disconnect cleanup | WS | capped, released | existing `TestLifecycle` |
38 | Reconnect/preview loop growing process-local state | WS + action store | bounded per account | **new** `TestResumeGrantBounds`, `TestConfirmationStoreIsolation` (M24, M25) |
39 | One account's flood evicting another's state | action store, WS registry | impossible | **new**, see 1.2 and 1.3 |
40 | Guessable session id claimed by another account | coach | victim keeps their conversation | **new**, see 1.5 |
41 | Provider unavailable / malformed / slow | AI paths | typed failure, no fabrication, no provider detail disclosed | existing `test_ai_error_disclosure.py`, `TestRecoverableStates` |
42 | Deadline exceeded during a mutation | action | reconciled, not reported failed | existing `test_a_slow_but_committed_mutation_is_not_reported_as_a_timeout` |
43 | Stale / undated / contradictory world observations | world tools | reported, never presented as fresh | existing `test_world_tools_security.py`, `test_world_contracts.py` |
44 | Secret-shaped canary through every new surface | logs + error bodies | absent | **new** `TestSecretsAreAbsentFromLogsAndErrors` |
45 | Password, bearer token, forged token | logs + error bodies | absent, not echoed | **new**, plus existing `test_validation_input_privacy.py` |

## 3. Secret redaction, measured

`TestSecretsAreAbsentFromLogsAndErrors` generates a unique canary per run, drives a real request
through a real boundary, and then searches everything the application logged at DEBUG. Covered:
a real confirmation token (whole, and its secret half separately, in case a log line split it on
the separator), a confirmation id, an idempotency key, a bearer token, a forged bearer token, a
password, an interview transcript, a coaching question, and a real WebSocket resume grant.

Two things make this evidence rather than decoration:

- `TestTheCanaryMethodIsSound` proves the capture is live (it finds a line the application
  really wrote) and that a canary is distinctive enough to find. Without those, every absence
  assertion could be passing against an empty capture.
- The resume-grant test asserts the session id **is** present. It is a correlation handle with
  no authority, the module says so, and pinning it keeps that a decision rather than a drift.

Result: no leak found on any of those surfaces. That is a negative result, and it is reported as
one — the tests are now there to keep it true.

## 4. Reliability and lifecycle

Reviewed, with the outcome stated rather than assumed:

| Property | Finding |
|---|---|
Bounded loops | the WS pump reads one message at a time and buffers nothing; no retry loop anywhere auto-retries an uncertain mutation |
Memory bounds | agent memory 256 sessions / 16 facts / 8 summary turns; world cache bounded with eviction counting; confirmations 256 global + 16 per account (**fixed**); resume grants 8 per account (**fixed**); voice sessions 3 per account |
Cancellation | honoured up to the point the approval is spent, and never reported as success; a cancellation arriving later does not disown a committed write |
Unknown outcomes | stay unknown; `ActionOutcome.__post_init__` makes a dishonest payload unconstructible |
One bad turn | tool failures are absorbed into `failures` per turn; turn evidence is discarded at the end of each turn, so a failed turn cannot poison the next |
Provider failure | degrades to the deterministic path; the file-based interview and the rest of the app keep working |
Process-local state | three registries are process-local with no cross-worker recovery, documented in each module and unchanged here |

### 4.1 Harness corrections

Two, both mine, both recorded because a reader of the suite will meet them:

- `replay_harness.post_together` arms the statement gate and never disarms it. Without an
  autouse `disarm_gate` fixture, every later test in the same file that touches
  `operation_claims` blocks on a two-party barrier that never fills, times out after 30 seconds
  and surfaces as a 500. Three tests failed that way before the fixture was added; VA-9's
  concurrency class already carries the same fixture for the same reason.
- A verifier written as `Verification(satisfied=..., description=...)` raises `TypeError`, because
  `Verification` is an enum and a verifier returns a bool. It surfaced as an `UNKNOWN` outcome —
  which is the registry behaving correctly, reporting an unreadable verification rather than
  guessing, and is now covered by a note in the test rather than by a change to the code.

No flaky test was observed in this phase. The two concurrency tests are gated rather than timed,
and the gate asserts that both requests genuinely interleaved rather than assuming it.

## 5. Mutation programme

No mutation-testing package is installed and installing one was not authorised, so VA-10 used
the controlled-probe route the specification allows: snapshot the file's bytes and SHA-256,
assert the guard's text appears exactly once, weaken it, run the targeted test, restore from the
snapshot in a `finally` block, re-hash, and verify. A final pass re-checks every target file
against its opening hash. The harness lives in the session scratchpad, not the repository: it
rewrites production files, and that is not a tool a future reader should be able to run by
accident.

**28 mutants, 27 caught, 1 missed.** Seven survived the first pass, and what each one revealed
was more useful than the twenty that were caught:

| Mutant | Guard weakened | First pass | Final | What the survival meant |
|---|---|---|---|---|
M01 | `owned_question`: question belongs to that interview | MISSED | CAUGHT | `submit_answer` carries its own copy of the same pair of checks, so the route stayed closed and no test could isolate this one. Fixed by testing the service directly. |
M02 | `read_interview`: profile check | MISSED | CAUGHT | same shape — the route-level test was refused earlier by `owned_question`. |
M10 | confirmation resource rebinding | MISSED | CAUGHT | **structurally unreachable** for today's catalogue: `_resource_key` is a pure function of `listing_id`, which the argument fingerprint already binds. Pinned with a probe whose resource is *resolved*, the shape the guard exists for. |
M13 | in-flight duplicate refused | MISSED | CAUGHT | my selector was wrong, not the guard untested: that file's duplicates arrive after the first claim settles. Needed a gated, genuinely overlapping pair. |
M16 | interview answer dedup on the race loser | MISSED | CAUGHT | same: the sequential pre-check refuses first, so only a gated concurrent pair reaches the `IntegrityError` branch. |
M26 | cancellation before approval consumption | MISSED ×2 | CAUGHT | **my own fix's test was not testing it** — twice. See 1.4. |
M23 | WebSocket early return on a missing bearer token | MISSED | **MISSED** | genuinely redundant: with the early return gone, `authenticate_access_token("")` refuses one step later with the same 4401 close code. No observable behaviour differs, so no test was written for it. Reported as a redundant guard, not as a coverage gap. |

The other twenty-one were caught on the first attempt: authentication type and revocation checks,
entitlement at both boundaries, all four confirmation bindings and single use, the READ/ACTION
separation, idempotency key reuse, authoritative verification, unknown-outcome reconciliation,
authoritative evaluation state, both output-guard halves, delimiter neutralisation, WebSocket
resume ownership and single use, and all five of VA-10's own fixes.

Restoration: every probe restored the file from its pre-mutation bytes in a `finally` block and
re-hashed it; the final pass re-checked all eleven target files against their opening SHA-256.
`drifted files: []` on every run.

## 6. Not tested, and not claimed

Unchanged from VA-9, restated because VA-10 touched none of it:

| Gate | Status | What would close it |
|---|---|---|
Spoken end to end (STT → transport → engine → TTS) | **BLOCKED** | an approved STT and TTS provider; the VA-2 gateway performs no recognition |
Real speech-provider run | **BLOCKED** | provider approval; every test here uses fakes, named as such |
Live job-market retrieval | **BLOCKED** | an approved world provider; no live request is made |
Realtime socket interview | **NEEDS TEST** | Android client work plus gateway message types no phase has added |
Physical-device interview, lifecycle, background/foreground | **NEEDS TEST** | a device run; VA-11 |
Actual interruption and recovery | **NEEDS TEST** | a real dropped connection; replay here is simulated by resending |
Cross-worker recovery of process-local state | **NEEDS TEST** | a chosen deployment topology and a shared store |
Live network behaviour of any kind | **NEEDS TEST** | VA-11 |

ElevenLabs remains excluded. Nothing was committed, pushed, deployed or published.
