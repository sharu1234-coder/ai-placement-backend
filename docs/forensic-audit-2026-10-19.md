# TALVIORA AI — cross-repository forensic audit and repair, 2026-10-19

SGLD Quant Labs. Backend repository `ai-placement-backend`, branch
`professional-presence-intelligence`, baseline HEAD `212df833b60bf07baa3b7c3c6c5f8ebe4cdcfae1`.
Android repository `ai-placement-android`, branch `career-strategy-intelligence`, baseline HEAD
`3ad14db8085dbc30bc49e7a27ae353f6532c0e2a`.

Both baselines were clean (`git status --porcelain=v2 -uall` returned zero entries) and level
with their upstreams, so **no pre-existing uncommitted work existed to preserve** and every
change in the working tree afterwards belongs to this audit. Nothing was committed or pushed.

---

## 1. Verdict

**PARTIAL.** Four confirmed defects repaired with regression tests; one confirmed and left
open with its reproduction recorded; one confirmed to be **less severe than reported**, with
the correcting measurement; one flaky test made deterministic. The gates that remain shut are
physical-device voice acceptance, PostgreSQL validation and GitHub-side CI confirmation.

The product must not be called release-ready: no screen has been seen rendered, no voice has
been heard on a handset, and the PostgreSQL suite has never run on this machine.

---

## 2. Findings

### Finding 7 — CI never ran on the development branches (confirmed, fixed)

Both workflows triggered on `master` only:

```yaml
on:
  push:
    branches: [master]
  pull_request:
    branches: [master]
```

while all work happens on `professional-presence-intelligence` and
`career-strategy-intelligence`. Confirmed against GitHub rather than inferred: `gh run list`
shows only Dependabot runs on `master` and on `dependabot/*` PR branches. **Zero runs exist for
either development branch head**, including the 305-file push made earlier the same day.

This is the finding that explains the others. Two `I001` import-sorting errors were sitting in
`app/speech/pyttsx3_tts.py` and `tests/unit/test_voice_selection.py` from that very push --
lint would have caught them on any CI run, and there was no CI run to catch them.

**Fixed:** each workflow now triggers on `master` plus its own development branch, and
`pull_request` carries no branch filter so a PR into any branch is checked. Validated by
parsing both files: triggers correct, `permissions: {contents: read}` unchanged, jobs intact
(`test`, `test-postgres`; `build`). No check was deleted and no deployment permission added.

**Caveat:** this cannot be confirmed from here. The workflow change is local and unpushed, so
GitHub has still run nothing. A local pass is not a CI pass.

### Finding 3 — a conversation was recorded `completed` for almost every abnormal ending (confirmed, fixed)

The most serious correctness defect found. `app/api/voice_session.py` had:

```python
ended_status = "interrupted"
try:
    await _pump(connection)
    ended_status = "completed"
except WebSocketDisconnect:
    pass
```

with the comment *"interrupted unless the pump returns of its own accord. A dropped connection
is not a completed interview, and the student own record must not claim it was."*

The effect was the exact inverse of that comment. `_pump`'s own docstring says it returns when
"the peer goes away **or breaks a rule**", and every one of its exits returned normally:

| `_pump` exit | Reality | Recorded as |
|---|---|---|
| `asyncio.TimeoutError` | idle timeout | `completed` |
| `websocket.disconnect` message | peer dropped | `completed` |
| registry eviction | session gone | `completed` |
| text frame over 8 KiB | limit breach | `completed` |
| audio frame over 64 KiB | limit breach | `completed` |
| invalid audio frame | protocol violation | `completed` |
| session audio over 25 MiB | limit breach | `completed` |
| `_handle_text` returning False | protocol violation | `completed` |

`CLIENT_MESSAGE_TYPES` was `{commit_turn, interrupt, resume, cancel_operation, heartbeat}` --
there was **no way for a client to say it had finished** -- and `_handle_text` returned `False`
only on a protocol violation. So `completed` was reachable *exclusively* through an abnormal
ending, and `interrupted` only when `WebSocketDisconnect` was raised rather than delivered as a
message. A student who timed out or lost signal mid-interview got a history entry saying they
finished it.

**Fixed**, protocol and both sides together:

- `end_session` added to `CLIENT_MESSAGE_TYPES`. Additive and backward compatible: a client
  that never sends it never produces a `completed` conversation, which is the safe direction.
- `_pump` now returns a `PUMP_*` reason; `_handle_text` returns a reason or `None` instead of a
  bool, because "stop" and "stop because the student finished" are different facts.
- `_PUMP_REASON_STATUS` maps each reason to one of the approved
  `CONVERSATION_STATUSES = ("active", "completed", "interrupted")`. No new status, so no
  migration. `status_for_pump_reason` raises on an unmapped reason rather than defaulting.
- Android: `RealtimeClientEvent.EndSession`, encoded as `{"type":"end_session"}`, sent from
  `RealtimeVoiceSession.stop(deliberate = true)`. The default is `false`, and that default is
  the safety property -- `onLeftForeground()` (`ON_STOP`) and `onCleared()` both call `stop()`,
  and neither is a completion. Only the "End session" button is, so the screen now calls
  `viewModel::endSession`.

**Tests:** `tests/api/test_voice_conversation_persistence.py` gained
`TestOnlyADeliberateEndIsCompleted` (explicit end → `completed`; protocol violation, oversized
control message, invalid audio frame and idle timeout → `interrupted`) and
`TestPumpReasonMapping` (every reason has a decided status; **only** `CLIENT_END` maps to
`completed`; an unknown reason raises). Android: `EndSessionContractTest` (codec output;
deliberate stop sends it; plain stop does not; it is sent before disconnect). 13 backend + 4
Android, all passing.

**A test was tightened, not weakened.** `test_closing_the_socket_records_how_the_session_ended`
asserted `status in ("completed", "interrupted")` -- it passed whichever value the code
produced, which is how this survived. It now asserts `interrupted`, because a socket that was
merely closed is not a finished conversation.

### Finding 1 — account deletion reported an erasure that had not happened (confirmed, fixed)

Two layers both swallowed the same failure.

`app/storage/s3_provider.py:38` caught every `BotoCoreError`/`ClientError`, logged a warning and
returned. Its signature is `-> None`, so **a caller could not distinguish a deletion from a
failure**. `app/services/account_service.py:335` then wrapped the call in `except Exception`,
logged another warning, and continued to `db.commit()` -- the comment said *"File deletion is
best-effort and never blocks the DB delete."*

Together: a storage outage during account deletion committed the database rows holding the only
copy of `file_path` while the object remained in the bucket. The result is an orphan nobody can
locate, belonging to a user who was told their data was erased.

**Fixed:**

- The `StorageProvider.delete` contract now states that an already-absent object is success
  (deletion stays idempotent) and anything else raises `StorageError`.
- S3 classifies `NoSuchKey`/`NoSuchBucket`/`404`/`NotFound` as already-gone and raises on
  everything else, including `BotoCoreError` -- a request that never reached the service did not
  delete anything.
- Local storage raises on `OSError` (a read-only mount or a permission error), keeping
  `missing_ok` for genuine absence.
- `delete_account` attempts **every** object, collects failures, and raises before any commit if
  any remain. Nothing is written, so every `file_path` survives for a retry. The invariant is
  the point: *never destroy the last pointer to an object that is still there.*

**Tests:** 8 added to `tests/unit/test_account_service.py` -- abort rather than orphan; a retry
after recovery completes; every object attempted before giving up; and per-adapter coverage of
absent-is-success versus raise-on-real-failure for both S3 and local, including the
transport-level case.

**An existing test asserted the defect.** The full suite surfaced exactly one failure:
`tests/unit/test_storage.py::test_s3_storage_delete_does_not_raise_on_failure`, which asserted
that `delete` swallowed a `ClientError`. That is the defect written down as intended behaviour.
It was **inverted, not deleted** -- renamed to `..._raises_on_a_real_failure` with the reasoning
recorded in the test -- and the half of the old contract that was right (an already-absent key
is success, so deletion stays idempotent) is now asserted explicitly by a second test,
`test_s3_storage_delete_is_idempotent_for_a_key_that_is_already_gone`. Net effect on that file:
11 tests to 12, all passing.

**Open product decision.** This makes a storage failure *block* account deletion. Completing it
asynchronously instead needs a durable deletion queue -- an outbox table, a retry worker, and a
defined window in which the account is inaccessible but not yet erased. That is a decision
about erasure timing and the product's deletion promise, not an engineering choice, so it is
left to the owner. The current behaviour is strictly better than before, which silently lied.

### Finding 4 — interview transcripts were not linked to their question (confirmed, fixed)

`conversation_history.append_turn` has always accepted
`interview_question_id: Optional[int] = None`. Both call sites in `voice_session.py` simply
never passed it, so every voice-interview turn was stored unlinked -- a defect nothing fails on,
because the default is `None`.

**Fixed:** `_Connection` now carries `interview_question_id` and both turns of the exchange are
linked. It is **gated on the mode** (`if selected is VoiceMode.INTERVIEW else None`), because
the query parameter is accepted regardless of mode and a career-coach socket handed one must not
acquire interview-only metadata.

Not stale: `build_interview_responder` is constructed once per socket for one question, so one
socket is one question. The prompt's warning about inferring a question from a stale client
value does not apply to a per-socket value that the socket exists to serve.

**Tests:** `tests/unit/test_voice_transcript_metadata.py` -- the column persists; a coach turn
records no question; **both** `append_turn` call sites pass the argument (a behavioural test on
`append_turn` alone would have passed throughout the defect, so the guard asserts the call
sites); and the mode gate is present.

### Finding 6 — worker/session lifetime: confirmed, and **less severe than reported**

Structurally the concern is real. `_Connection.db` is the request-scoped session from
`Depends(get_db)` -- its own docstring calls it *"The one Session that serves this whole
socket"* -- it is committed from turn workers (`connection.db.commit()` at two sites), and the
endpoint's `finally` deliberately leaves those workers running (*"The worker is left to finish:
it may be blocked inside a recogniser that cannot be interrupted"*). When the endpoint returns,
FastAPI closes the dependency.

**But the predicted failure does not occur.** `get_db` ends with `db.close()`, which returns the
connection to the pool and leaves the `Session` fully usable; the next use transparently checks
out another connection and begins a new transaction. Measured: a write through a closed session
**succeeds**.

So this is not the data-loss defect it was filed as. The residual concerns are real but lower:

1. the turn commits outside the request's transaction, so it is no longer atomic with anything
   else that request did, and
2. a task nobody awaits checks a connection out of the pool, which under load is how a pool is
   exhausted by work with no client left to serve.

**Not fixed.** Moving workers onto their own session changes session ownership across the whole
voice path, and the measurement above says the severity does not justify doing that in passing.
`tests/unit/test_voice_transcript_metadata.py::TestWorkerSessionLifetime` pins the real
behaviour and the two structural facts that make the path reachable, so a future change to
either must confront it.

### Finding 5 — the female voice (verified as far as this environment allows)

Re-verified at this HEAD rather than assumed from the previous session's work.

The full route is now traced end to end, and there is exactly one synthesiser on it:
`voice_session.py:427` → `build_turn_runtime()` → `TurnRuntime` → `app.speech.selection` →
`Pyttsx3TextToSpeech`. There is exactly **one** `SynthesizeRequest(` construction in the entire
backend (`app/voice/turn_runtime.py:416`) and it carries **no `voice=` override**, so nothing
supersedes `TTS_VOICE_GENDER`.

Diagnostics through the real adapter, at this HEAD:

```
tts_provider: pyttsx3   tts_voice_gender: female   tts_voice_required: False
adapter_class: Pyttsx3TextToSpeech   adapter_module: app.speech.pyttsx3_tts
voice_selection: outcome=selected requested=female
                 voice=Microsoft Zira Desktop - English (United States) available=2
voice_satisfied: True   sample_rate_hz: 22050   peak_dbfs: -5.43 / -3.58
```

Five consecutive turns through `get_text_to_speech()` all reported `outcome=selected` with
Zira. Root cause and measurements are in section 9 of `docs/speech-provider-evaluation.md`
(two adapters, one of which had no voice selection; plus pyttsx3's queued property writes,
which made a read-back taken before `runAndWait()` report a working selection as broken).

**Why the user may still have heard a male voice:** nothing was listening on port 8000 at any
point during this audit (`Get-NetTCPConnection -LocalPort 8000` returned nothing). The fix is
backend-side and takes effect only on a restarted backend process. The installed APK is
byte-identical (`6b756f48…c92d3`) and embeds `http://192.168.0.113:8000/`, which still matches
this machine's Wi-Fi address — so no reinstall is needed, but a backend must be running.

`tts_voice_required` remains `False`, i.e. fallback to the host default is still permitted. That
is the existing product policy and was **not** changed; the fallback is now observable instead
(warning log, `tts_voice_outcome` in turn metrics, and the diagnostics script).

**Not verified:** nobody has heard it. No device is attached (`adb devices` lists none). A
measured 175.7 Hz median F0 is not a listening test, and this report does not treat it as one.

### Finding 2 — resume does not rebind the original conversation (confirmed, not fixed)

Confirmed by reading the resume handler in `app/api/voice_session.py`. It redeems the token,
records `record.resumed_from = grant.session_id`, rotates the token and replies
`session_accepted`, with the comment *"Deliberately no turn state is carried over."* What it does
**not** do is bind the new socket to the previously persisted conversation: the new connection
has already opened its own conversation row keyed on its own `session_id`, so the transcript
continues in a new conversation while the client reports a successful recovery.

The not-restarting-an-in-flight-turn behaviour is correct and documented, and the ownership check
(`redeem_resume_token(..., user_id=connection.user_id)`) is present, so this is a continuity
defect rather than a security one.

**Not fixed.** Rebinding touches conversation identity, history pagination and the resume
protocol together, and doing it carelessly risks appending one student's turns to the wrong
transcript — worse than the defect. Recorded as the largest open engineering item.

### Known flaky test — fixed, with the measurement

`tests/api/test_va10_hardening.py::TestInFlightDuplicates::test_two_overlapping_executions_produce_one_mutation_and_no_premature_result`

Measured before changing anything: **7 passes / 3 failures over 10 isolated runs**, failing as
`assert ['succeeded', 'succeeded'] == ['in_progress', 'succeeded']`.

Root cause: the assertion required the *loser* to observe the in-flight window. `arm_gate` holds
each thread once, at the claims read, then releases both; after that the winner's finalize write
races the loser's re-read. When the winner finalises first, the loser reads the real, finished
result and correctly reports `succeeded`. **That is the guard working**, not failing — the class
docstring itself says what the refusal protects is "the student not being handed a result for
work that has not finished", which `['succeeded','succeeded']` does not violate.

Fixed without weakening, and the net assertion count went up:

- The test now proves **the race was actually staged** (`len(h.gated_threads) == 2`). The
  harness exposes that set for exactly this purpose and the old test never checked it, so a run
  where the requests did not overlap would have passed just as happily.
- Both legal orderings are accepted, and **every** response is then checked against the real
  invariant: one mutation; an `in_progress` reply carries no result and `verified is False`; a
  `succeeded` reply must be backed by the durable row. Previously only one response was checked
  this way, because the other was required to be `in_progress`.
- The in-flight branch — which the racy test covered only when it won the race — is now covered
  **deterministically and without threads** by
  `test_an_operation_still_in_flight_is_never_reported_as_a_result`, which takes a real claim
  through `idempotency.claim` (operation `tool.jobs.save`, params from the registry's own input
  model, so the fingerprint is genuine) and asserts the duplicate is told `in_progress` with the
  mutation not run.

Gating the finalize write instead was rejected on the harness's own recorded evidence: gating a
write deadlocks SQLite (one thread holds the write lock while waiting at the barrier), which is
the lock-contention/500 outcome a previous attempt produced.

---

## 3. Automated scans

Tracked files only, both repositories.

| Scan | Backend | Android |
|---|---|---|
| `TODO`/`FIXME`/`XXX`/`HACK` | 0 | 0 |
| bare `except:` | 0 | n/a |
| `except Exception: pass` | 0 | n/a |
| empty `catch {}` | n/a | 0 |
| `printStackTrace` | n/a | 0 |
| genuine `print(` in app code | 0 | n/a |
| insecure `http://` usage | 0 | 0 |
| hardcoded `Color(0xFF…)` outside `ui/theme/` | n/a | 0 |
| `debug=True` / `debuggable true` | 0 | 0 |

Two scans needed correcting before they could be reported. `print(` first showed 28 hits, all of
which were `fingerprint(`; the 6 real matches are command strings inside documentation. The 5
`http://` hits are SSRF-blocklist examples in `app/services/job_hunt/normalization.py` (addresses
that must be *rejected*) and one user-facing error message. `usesCleartextTraffic` is correctly
flavour-scoped: `true` in `app/src/dev`, `false` in `app/src/prod`.

---

## 4. Coverage, stated honestly

The brief asked for every tracked file to be read in full. That was not done, and is not
claimed: 685 tracked files in the backend and 663 in the Android app.

- **Read in full or in substantial part:** the files named in Findings 1-7 and their call
  paths -- `voice_session.py`, `realtime_protocol.py`, `turn_runtime.py`, `pyttsx3_tts.py`,
  `selection.py`, `voice_selection.py`, `account_service.py`, the three storage providers,
  `conversation_history.py`, `conversation.py`, `operation_claim.py`, `idempotency.py`,
  `registry.py`, `deps.py`, `replay_harness.py`, both CI workflows, and on the Android side
  `RealtimeTransport.kt`, `WebSocketRealtimeTransport.kt`, `RealtimeVoiceSession.kt`,
  `AndroidAudioOutputRouter.kt`, `NetworkModule.kt`, `VoiceAgentViewModel.kt`,
  `VoiceAgentScreen.kt`.
- **Scanned automatically:** all tracked source in both repositories, per section 3.
- **Not inspected:** the remainder, which is the majority. The three PDFs under `docs/source/`
  were identified by name and size only and received no semantic review.

---

## 5. Validation

Every figure below comes from a command run during this audit, with the process exit code
captured separately from any pipeline.

| Check | Result |
|---|---|
| `pytest tests` (full suite) -- **historical, see §5a** | **4379 passed, 0 failed, 3 skipped**, `PYTEST_EXIT=0`, 5071 s (1:24:30). Measured **before** the guard in §7; the suite has since gained the 72 tests in `test_db_safety.py` and lost none, and the full suite has not been re-run since. The 33 modules the guard touched were re-run and are the row below |
| `pytest tests` (previous run) -- **historical, see §5a** | **4377 passed, 1 failed, 3 skipped**, exit 1. The single failure was `test_s3_storage_delete_does_not_raise_on_failure`, which asserted the old swallow-the-error contract (see Finding 1). 4377 + the inverted test + the new idempotency test = 4379 |
| `ruff check app tests scripts` | **All checks passed**, exit 0 (2 `I001` errors found and fixed) |
| `pytest tests/unit/test_db_safety.py` (the guard, §7) | **72 passed, 0 failed, 0 skipped**, exit 0 |
| `pytest` over the 33 modules the guard rewiring touched, on the SQLite default | **467 passed, 2 skipped, 0 failed**, exit 0, 229 s. The two skips are the pre-existing PostgreSQL gates: `test_backup_restore_roundtrip.py:307` ("needs a TEST_DATABASE_URL that already exists; credentials are never probed for") and `test_professional_presence_migration.py:215` ("partial index definitions are PostgreSQL-specific") |
| Hostile `TEST_DATABASE_URL` probes (4, §7) | each refused at **collection, exit 4** — no test executed, no connection attempted, no password in any message. `app.db` size and mtime unchanged after the `sqlite:///./app.db` probe |
| `ruff check app tests scripts` (after the guard) | **All checks passed**, exit 0 |
| `pytest tests/unit/test_downgrade_guard.py` (migration downgrade guard, written ahead of the outbox migration) | **19 passed, 0 failed**, exit 0. SQLite only; **not run against PostgreSQL** |
| `alembic heads` | single head `d4a1c08f52e7` |
| Android `:app:testDevDebugUnitTest` | **1542 tests, 0 failures, 0 errors, 0 skipped**, 177 suites, counted from `TEST-*.xml` |
| Android `:app:lintDevDebug` | **0 errors**, 32 warnings, all pre-existing advisories |
| Android `:app:assembleDevDebug` | succeeded; APK sha256 `6b756f48…c92d3` |
| `TestInFlightDuplicates` | **14 passes / 0 failures over 14 isolated runs** after the fix, against 7/3 before |
| Skipped tests (3) | Genuine environment skips, each naming its reason -- not silent passes. Two are the PostgreSQL gate (`test_backup_restore_roundtrip.py`: "needs a TEST_DATABASE_URL that already exists; credentials are never probed for"; `test_professional_presence_migration.py`: "partial index definitions are PostgreSQL-specific"). The third is `test_tts_linux_contract.py`: *"The eSpeak path is Linux-only and this host is not Linux... until it runs on Linux with espeak-ng installed, Linux TTS is NOT PROVEN."* |
| **PostgreSQL suite** | **NOT RUN.** No authorised disposable `TEST_DATABASE_URL`. Not substituted with SQLite. Corrects an earlier statement in this report that `psql` was absent: PostgreSQL 17 and 18 are installed and running on this host and `psycopg2` is in `.venv312` -- availability was never the blocker, authorisation is. Any such run must now pass the guard in §7 |
| **GitHub CI** | **NOT RUN** for either branch head. Workflow fix is local and unpushed |
| **Physical device** | **BLOCKED.** `adb devices` lists none |
| **Audible voice** | **BLOCKED.** Not heard by anyone |


### 5a. Historical evidence versus current reproducible evidence

The table above mixes two kinds of claim, and after the `.env` discovery below they have to be
read differently.

**Historical (not reproducible as the environment now stands).** The **4379 passed, 0 failed,
3 skipped**, `PYTEST_EXIT=0`, 5071 s full-suite run. It happened and its log survives
(`final2-backend.log`), but it cannot be reproduced from the repository root on this machine,
and the reason is now identified rather than suspected.

`tests/unit/test_config_validation.py` and
`tests/unit/test_speech_selection.py::TestConfiguredSelection::test_that_is_the_default` read
the live `app.core.config.settings` singleton, which pydantic populates from the process
environment and from `.env` (`env_file=".env"`, resolved relative to the working directory).
This machine's `.env` configures development speech providers and GitHub OAuth values, so
those tests assert the declared defaults against a correctly configured developer machine.
Measured both ways:

| Command | From the repository root | With `.env` not discoverable (cwd outside the repo, `PYTHONPATH` at it) |
|---|---|---|
| `pytest tests/unit/test_config_validation.py -q -p no:randomly` (**as measured before the correction below**) | **20 failed, 40 passed**, exit 1 | **60 passed**, exit 0 |
| `pytest "tests/unit/test_speech_selection.py::TestConfiguredSelection::test_that_is_the_default"` | failed (before the fix below) | passed |

So the historical run executed in an environment where `.env` was not in effect. *Which*
environment that was cannot be reconstructed: `.env`'s `LastWriteTime` is 2026-10-18 20:31,
before the run, its contents have not changed since, no test or application code mutates
either setting (`grep` for assignment to `settings.stt_provider` / `settings.tts_provider`
across `app/`, `scripts/` and `tests/` returns nothing but `monkeypatch`, which restores), and
the run's invocation is no longer in the task log. The figure is therefore left recorded as
measured and is **not** restated as current evidence.

**Current, reproducible from the repository root.** Every row below was re-measured after the
`.env` finding, with the exit code taken unpiped or from `${PIPESTATUS[0]}`:

| Command | Result |
|---|---|
| `pytest "tests/unit/test_speech_selection.py::TestConfiguredSelection::test_that_is_the_default" -q -p no:randomly` | **1 passed**, exit 0 |
| the same, with no environment prefix at all (`env -u` on the four usual blanked vars) | exit 0 |
| the same, with `STT_PROVIDER=faster-whisper TTS_PROVIDER=pyttsx3` set in the process | exit 0 — the point of the fix |
| `pytest tests/unit/test_speech_selection.py -q -p no:randomly` | **24 passed**, exit 0 |
| `pytest tests/unit/test_db_safety.py tests/unit/test_downgrade_guard.py -q -p no:randomly` | **91 passed**, exit 0 |
| `ruff check app tests scripts` | All checks passed, exit 0 |
| `pytest tests/unit/test_config_validation.py tests/unit/test_development_speech_config.py tests/unit/test_speech_selection.py tests/unit/test_voice_selection.py tests/unit/test_tts_linux_contract.py -q -p no:randomly` | **20 failed, 100 passed, 1 skipped**, exit 1 *at the time* — all 20 failures in `test_config_validation.py`, same `.env` cause. **Corrected since: 157 passed, 1 skipped, exit 0** |

**The fix, and its scope.** `test_that_is_the_default` now asserts
`Settings.model_fields["stt_provider"].default == "none"` — the declared default, which is the
claim the test exists to make — and then constructs `Settings(_env_file=None)` with
`STT_PROVIDER` and `TTS_PROVIDER` removed from the process environment, so a validator or
alias that rewrote the value on the way in would still be caught. Test-only: 24 insertions, 5
deletions, one function, no change to `app/core/config.py` and no change to any production
STT or TTS default (`stt_provider: str = "none"`, `tts_provider: str = "none"` are untouched).

**Resolved, as a separate test-only change (2026-10-19).** Both affected modules are now
independent of `.env` and of process-environment overrides, and the mechanism that coupled
them is asserted against rather than merely fixed.

The diagnosis was not twenty bad assertions. `tests/unit/test_config_validation.py` sets up a
production-ready baseline on the global `settings` singleton and then varies one setting per
test; its `_production_base` helper pinned the nine settings production requires to be
*non-default* and left the other eight to whatever the machine was configured with. When
`validate_for_production()` later grew a check on `STT_PROVIDER`/`TTS_PROVIDER`, pydantic's
`.env` loading supplied those values, and every "production-ready" test inherited a
development machine's speech configuration — failing on a setting it had never mentioned.

**The correction.** `_production_base` now pins **every** setting the validator reads: the
nine explicit production values it already had, plus `stt_provider`, `tts_provider`,
`token_encryption_key` and the three GitHub OAuth fields, each set to its **declared default**
via `Settings.model_fields[...].default` rather than a literal — so a default that moves in
`config.py` moves the baseline with it. Six tests that inlined the same eight-line preamble
now call the helper instead; each keeps the overrides it is actually about. `AI_PROVIDER` and
`OLLAMA_BASE_URL` are deliberately exempt: several tests in the module are about them, and
`_production_ready_ai` is the opt-in for the rest.

**And the mechanism is now guarded.** `TestTheBaselineCannotSilentlyStopCoveringTheValidator`
scans `validate_for_production`'s source for the settings it reads and asserts every one is
pinned by a helper or declared exempt, with the failure message naming the uncovered field.
Two supporting tests keep that guard honest: one pins the `monkeypatch.setattr(target, name,
value)` signature the scan's recorder imitates, one asserts each exempt setting is genuinely
varied by a test in the module, and the invariant itself refuses to pass on a scan that found
fewer than ten settings — a regex that matched nothing would otherwise satisfy a subset check
while verifying nothing.

One assertion was **strengthened** in the process. `test_the_error_does_not_echo_the_key` used
`pytest.raises(RuntimeError)` with no `match`, and on a developer machine the `.env` speech
guard raised first — a different error that also happened not to echo the key. It passed for
the wrong reason. It now requires `match="TOKEN_ENCRYPTION_KEY"`.

**Measured, from the repository root, exit codes unpiped or from `${PIPESTATUS[0]}`:**

| Command | Result |
|---|---|
| each of the 20 formerly failing tests, run individually | **20 run, 0 non-zero exit codes** |
| `pytest tests/unit/test_config_validation.py -q -p no:randomly` | **63 passed, 0 failed**, exit 0 (60 + 3 new) |
| the same, with no environment prefix at all (`env -u` on the four usual blanked vars) | **63 passed**, exit 0 |
| the same, with `STT_PROVIDER=faster-whisper TTS_PROVIDER=pyttsx3 RAZORPAY_KEY_ID=rzp_test_zzz DEBUG=true` in the process | **63 passed**, exit 0 |
| the same, with `.env` not discoverable (cwd outside the repository, `PYTHONPATH` at it) | **63 passed**, exit 0 |
| `pytest test_config_validation.py test_development_speech_config.py test_speech_selection.py test_voice_selection.py test_tts_linux_contract.py test_jobvetta_configuration.py -q -p no:randomly` | **157 passed, 1 skipped**, exit 0 |
| `ruff check app tests scripts` | All checks passed, exit 0 |

The four identical counts across four different environments are the result that matters:
before this change the module returned 20 failed / 40 passed from the repository root and 60
passed with `.env` absent.

**Accidental dependence versus deliberate exercise.** The distinction was checked rather than
assumed. No test in the suite calls `monkeypatch.setenv` to drive `Settings`, so there was no
intentional environment-variable loading test to preserve. Two tests legitimately concern
`.env` and were left untouched: `test_development_speech_config.py:120` already constructs
`Settings(_env_file=None)` for exactly this reason — the established pattern this change and
the `test_speech_selection.py` fix both follow — and
`test_jobvetta_configuration.py::test_the_real_env_file_is_not_tracked` asserts `.env` is
gitignored, which is about the file's status and not its contents. Nothing was disabled
globally: no `conftest.py` was touched, no module-level `env_file` override was introduced, and
`app/core/config.py` is unchanged, as are every production STT/TTS default.

**Scope.** Test-only, two files across the two changes: `tests/unit/test_config_validation.py`
(108 insertions, 46 deletions, one file) and the earlier one-function fix in
`tests/unit/test_speech_selection.py`. No assertion was weakened, one was strengthened.

**What this does not change.** The historical **4379-pass** run is still historical and is
**not** the current baseline. The current baseline was measured after these corrections and
is §5b: **4473 passed, 0 failed, 3 skipped**, exit 0. PostgreSQL integration remains **BLOCKED**
pending an authorized, independently verified disposable instance
(`docs/postgres-test-environment.md`); the deletion outbox and voice resume remain
unimplemented; and the device-audio, visual-UI, Linux-TTS and GitHub-CI gates remain open.

**PostgreSQL stays BLOCKED.** Nothing in this change affects it. No disposable instance has
been authorized, no credential has been guessed or probed, and the requirements for one are in
`docs/postgres-test-environment.md`. PostgreSQL coverage remains NOT RUN until an isolated
disposable instance is authorized *and* independently verified by the negative `CONNECT`
check in that document.

---


---

### 5b. Current backend baseline (2026-10-19, after the configuration-test corrections)

This replaces §5's historical figures as the number to quote. It is a full run, from the
repository root, with `.env` discoverable and nothing blanked out of the environment — the
condition under which the suite had never previously been measured green.

**Tree state at the time of the run.** Branch `professional-presence-intelligence`, HEAD
`212df833b60bf07baa3b7c3c6c5f8ebe4cdcfae1`, 0 ahead and 0 behind `origin`, 0 staged, 40
modified and 9 untracked files. `app/core/config.py`, `.env.example`, `alembic.ini`,
`pyproject.toml`, `requirements.txt`, `requirements-dev.txt` and `app/main.py` have **zero**
changed lines; `stt_provider` and `tts_provider` are still `"none"`, `storage_provider`
`"local"`, `tts_voice_gender` `"female"`.

### The run

```
$ ./.venv312/Scripts/python.exe -m pytest tests -q -rs
```

| | |
|---|---|
| **PYTEST_EXIT** | **0** (captured from `$?`, not from a pipeline) |
| Duration | **4319.12 s (1:11:59)**, 22:34:27 to 23:47:15 |
| Collected | **4476** |
| Passed | **4473** |
| Failed | **0** |
| Skipped | **3** |
| Errors | **0** |
| Collection failures | **none** |
| `ruff check app tests scripts` | **All checks passed**, exit 0 |

### The run before it, and the failure it found

The first full run of this stage, same command without `-rs`, was **4472 passed, 1 failed, 3
skipped**, `PYTEST_EXIT=1`, 4175.58 s. It is recorded because the failure it found was real and
is the reason the numbers above differ by one test.

`tests/api/test_voice_realtime_turn.py::TestTheApprovedDefault::test_no_provider_is_configured_so_a_turn_reports_it_honestly`
failed with `AssertionError: assert 'faster-whisper' == 'none'`. The third instance of the
defect §5a describes, and the clearest of the three: the test's docstring said "No fakes, no
monkeypatching: the real `build_turn_runtime` against real settings", and its first two lines
asserted that this machine has no speech provider configured. That is the test's **premise**,
not its subject — so reading the live singleton made the premise depend on `.env`, and a
developer with `STT_PROVIDER=faster-whisper` (a correct development setup) saw it fail on the
precondition having exercised nothing.

Corrected by pinning both settings to their declared defaults with `monkeypatch` before the
preconditions, and rewriting the docstring to say so rather than claiming no monkeypatching.
Nothing after that line changed: `app/speech/selection.py` reads these settings per call, so
the selection path the test drives is still the production one. 15 insertions, 3 deletions, one
test file. No assertion weakened, no production default touched.

### The three skips, from this run's own `-rs` output

Not converted, each naming an environment it needs:

| Test | Reason |
|---|---|
| `tests/scripts/test_backup_restore_roundtrip.py:307` | "PostgreSQL backup/restore needs a TEST_DATABASE_URL that already exists; credentials are never probed for" |
| `tests/unit/test_professional_presence_migration.py:215` | "partial index definitions are PostgreSQL-specific" |
| `tests/unit/test_tts_linux_contract.py:112` | "The eSpeak path is Linux-only and this host is not Linux... until it runs on Linux with espeak-ng installed, Linux TTS is NOT PROVEN" |

Two are the PostgreSQL gate and one is the Linux TTS gate. No disposable PostgreSQL instance
has been provisioned, authorized or verified since §7 and `docs/postgres-test-environment.md`,
so **PostgreSQL integration remains BLOCKED** and the first two skips are the correct outcome
rather than a deficiency in this run.

### One intermittent: NOT REPRODUCED in 7 attempts, with the variance mechanism identified

While verifying the fix above, a standalone run of `tests/api/test_voice_realtime_turn.py`
with the four usually-blanked environment variables *unset* reported **1 failed, 43 passed**
in 276.23 s. That run printed counts only — the command piped its output through `tail -1` —
so **the failing node ID and its traceback are unrecoverable.** Confirmed by inspection: the
surviving task output for that run is 16 lines of section headers and summary lines, with no
`FAILED` line, no `=== FAILURES` block and no traceback, and no other log in this session
records a failure in this module.

**Replication: 7 attempts, 0 failures.** Two before this investigation, five during it, each
logged separately with `-v -rs --tb=long -p no:randomly` and its own process exit code. The
five replications deliberately reproduced the *sequence* of the failing run, not just the
command: a prefixed run followed by an `env -u` run in the same shell, twice, plus one
standalone unset run.

| Run | Environment | Exit | Result | Wall time |
|---|---|---|---|---|
| pairA_1 | four vars blanked | 0 | 44 passed | 136.78 s |
| pairA_2 | four vars **unset** | 0 | 44 passed | 138.13 s |
| pairB_1 | four vars blanked | 0 | 44 passed | 208.61 s |
| pairB_2 | four vars **unset** | 0 | 44 passed | 190.79 s |
| solo | four vars **unset** | 0 | 44 passed | 210.31 s |
| *(the original)* | four vars **unset** | **1** | **1 failed, 43 passed** | **276.23 s** |

It is recorded as **not reproduced**, not as resolved.

**What the environment hypothesis is worth: nothing.** The module references `github`,
`token_encryption` and `professional_presence` **zero** times, so those four variables cannot
reach its logic. The durations say the same thing independently — both modes span the whole
observed range (blanked: 136.78 s and 208.61 s; unset: 138.13 s, 190.79 s, 210.31 s), so the
variable state does not even affect runtime. Ordering is eliminated too: every run, including
the failing one, used `-p no:randomly`, so the order was identical. Cross-test state leakage
would be deterministic under a fixed order; it is not.

**What is left, and it is measurable: in-process CPU contention from a real model load.**
`app/main.py`'s lifespan starts a daemon thread calling `_get_whisper_model()` whenever
`warm_speech_models_on_startup` and `ai_provider == "ollama"` — and on this host both hold
(`warm=True`, `ai_provider=ollama`). `tests/conftest.py`'s `client` fixture enters
`with TestClient(app)`, which runs that lifespan. Proven by probe rather than inferred: a
`TestClient` context was opened and the live threads enumerated —

```
warm threads before: 0 during: 1
alive during: [True]
```

`faster_whisper` is importable in `.venv312` and `models--Systran--faster-whisper-tiny.en` is
cached on disk (911 MB in the Hugging Face hub cache), so that thread performs a genuine model
load, not a fast failure. `_whisper_model` is a process-global behind a lock, so the load
happens **once per pytest process**.

That gives the asymmetry that matters. In a standalone run of this module the load lands in the
first seconds — exactly where the module's **13** `Event.wait(timeout=...)` barriers sit, four
of which assert things like "recognition never began" within 5 seconds. In a 4476-test run it
lands during whatever executes first, hundreds of tests before this module. Which is precisely
the pattern observed: the failure appeared in a standalone module run and in neither full run,
and the failing run was the slowest execution of this module ever measured here.

**No code was changed.** The failure is unidentified, so no fix can be shown to fix it, and
changing a barrier, a timeout or the warm-up on a hypothesis would convert an open question
into an invisible one. Nothing is currently red: the module passes 44/44 and the suite passes
4473/0/3. `ruff check app tests scripts` is exit 0 and the working tree is byte-identical to
before this investigation (the probe lives in the session scratchpad, not the repository).

**For the owner, if this is to be closed rather than watched.** Two candidate changes, neither
taken here: set `WARM_SPEECH_MODELS_ON_STARTUP=false` for the test session, which removes a
real model load from every test process and is honest because the warm-up is a production
latency optimisation with nothing in this suite asserting it; and/or capture full output on
every CI test run so the next occurrence arrives with a node ID instead of a count. The second
is what makes the first unnecessary to guess at.

### What this baseline is not

- **Not a claim of production readiness.** It is one suite, on one host, on SQLite.
- **Not PostgreSQL coverage.** See the skips above.
- **Not a device, audio, or UI result.** No handset, no rendered screen, no audible voice.
- **Not a CI result.** No workflow has run on either branch head.
- **Not comparable to the 4379 figure in §5.** That run had a different test count and, as §5a
  establishes, executed in an environment where `.env` was not in effect. The two are not two
  measurements of the same thing.

No commit, push, deployment, reset, stash, rebase or branch switch was performed. A secret scan
over the whole diff plus every untracked file found no credential: the only matches are the
suite's pre-existing signup password constant, which is present in HEAD, and one deliberately
fake URL in `tests/unit/test_db_safety.py`.

## 6. Remaining owner decisions

1. **Deletion semantics (Finding 1).** Block account deletion on a storage failure, as now, or
   build a durable deletion queue and accept a window where the account is inaccessible but not
   yet erased. A compliance question about the erasure promise.
2. **Resume continuity (Finding 2).** Rebinding a resumed socket to its original conversation,
   with the history and pagination consequences that implies.
3. **Production speech provider.** Still undecided. Faster-Whisper and pyttsx3 remain
   development-only; `validate_for_production()` refuses them outside development. Kokoro
   reviewed, **not installed** (Apache-2.0 code and weights, 82 M parameters, requires `torch`
   and still requires espeak-ng, so the open Linux question does not go away). ElevenLabs
   excluded.
4. **`TTS_VOICE_REQUIRED`.** Left at `False`, preserving the existing fallback policy. Setting
   it `True` makes an unavailable female voice a typed failure instead of a silent downgrade.
5. **A PostgreSQL test database.** Needed before any migration or concurrency claim can be made
   about the real engine.
6. **A handset.** The only way to close the voice gate.
7. **Pushing the CI fix**, which is the only way to learn what CI says about any of this.

---

## 7. The destructive test-database guard (B2 — fixed)

Stage 1 was asked to confirm that the testing configuration could not point at a production
database. It could. This section records what was wrong, what now stops it, and what it still
does not stop.

### What was wrong

`tests/conftest.py` runs `Base.metadata.drop_all(bind=engine)` before **every** test when
`TEST_DATABASE_URL` is set, and the migration tests go further and run `alembic downgrade
base`. Twenty-four modules read that variable straight from the environment, and nothing
anywhere compared it to anything: no check against `settings.database_url`, no naming
requirement, no inspection of what was already in the schema. The only protection was a
comment in `conftest.py` showing an example URL.

So the entire distance between "run the PostgreSQL suite" and "destroy a database" was one
mistyped environment variable — and the variable can arrive without anyone typing it at all,
inherited from a parent shell or a `.env` loaded for an unrelated reason.

### What now stops it

One new module, `tests/db_safety.py`, and the rule that the test suite no longer reads the
variable. The URL is *supplied* by `db_safety.resolve_test_database_url()`, and every
destructive call goes through `db_safety.drop_all_safely()`. That is the design decision worth
recording: making the guard the supply route rather than an extra call at each site means a
PostgreSQL test added next year is protected by the ordinary act of asking for the URL, and a
test that wants to bypass the guard has to reach for `os.environ` explicitly, where review can
see it. Two tests assert that no module in `tests/` does either.

| # | Requirement | How it is met |
|---|---|---|
| 1 | Explicit test-only opt-in | `TEST_DATABASE_DESTRUCTIVE` must be truthy. A URL alone is not consent; this variable has no meaning outside the suite, so it cannot be inherited by accident |
| 2 | Reject the application's database | Identity is compared as a normalised tuple — driver suffix dropped, host lower-cased with loopback spellings collapsed, omitted port defaulted, query string ignored, database name case-folded — so none of the seven spellings tested can disguise a match |
| 3 | Database name must end in `_test` | Enforced, and the message says it is a typo-catcher and *not* a security boundary |
| 4 | Missing, malformed, ambiguous configuration | Unparseable, non-PostgreSQL, hostless, nameless and roleless URLs are each refused with a distinct message. Absent — the default — returns `None` and keeps the SQLite behaviour every existing run depends on |
| 5 | A dedicated role, not just the name | `postgres`, `root`, `admin`, `sa`, `superuser`, `rdsadmin` are refused unless `TEST_DATABASE_ALLOW_ADMIN_ROLE` is set. CI sets it, because its `services: postgres` container has one role and dies with the job; that exemption is explicit rather than inferred from `CI=true` |
| 6 | Inspect the existing schema | `drop_all_safely` lists the target's tables and refuses if any is not in `Base.metadata` (plus `alembic_version`). Documented as evidence, not isolation: an *empty* production database passes it |
| 7 | Every safeguard before `drop_all` | The static checks run at module import, before any fixture exists. The schema check is inside `drop_all_safely`, above the `drop_all` call, so the ordering is structural rather than a convention to remember |
| 8 | Credentials out of logs and messages | `redact()` rebuilds the URL from host, port and database only — the password is never read and the username never printed. A URL that fails to parse is not echoed *at all*, because SQLAlchemy's own `ArgumentError` embeds the string it was handed |

### Proven, with the commands

`tests/unit/test_db_safety.py`, 72 tests, all passing, exit 0. The refusals below were measured
by running a real `pytest` against a hostile `TEST_DATABASE_URL`, not by reading the code. Each
command was `pytest tests/unit/test_idempotency.py -q -p no:randomly` with the environment
shown:

```
TEST_DATABASE_URL=postgresql+psycopg2://app_user:<pw>@db.invalid.example:5432/production
  -> UnsafeTestDatabase: TEST_DATABASE_URL is set, but TEST_DATABASE_DESTRUCTIVE is not. [...]
     (The URL is not shown here because it may contain credentials.)
  -> exit 4

  + TEST_DATABASE_DESTRUCTIVE=1
  -> UnsafeTestDatabase: TEST_DATABASE_URL's database name must end in '_test'; got
     postgresql://<redacted>@db.invalid.example:5432/production.
  -> exit 4

TEST_DATABASE_URL=sqlite:///./app.db  TEST_DATABASE_DESTRUCTIVE=1
  -> UnsafeTestDatabase: TEST_DATABASE_URL must name a PostgreSQL database; got backend
     'sqlite' (sqlite://<redacted>@?/./app.db).
  -> exit 4

TEST_DATABASE_URL=postgresql+psycopg2://postgres:<pw>@127.0.0.1:5432/aiplacement_test
  TEST_DATABASE_DESTRUCTIVE=1
  -> UnsafeTestDatabase: TEST_DATABASE_URL connects as an instance-wide administrative role
     [...] set TEST_DATABASE_ALLOW_ADMIN_ROLE=1.
  -> exit 4
```

Exit code 4 is pytest's collection error: the run stopped before a single test executed. The
third case is the one that matters most — that is the development `app.db`, named by the
obvious typo, and it was refused rather than dropped; its size and mtime were unchanged
afterwards. No password appears in any of the four messages.

Two defects were found in the guard by its own tests and fixed:

- `make_url` raises **`ValueError`**, not only `ArgumentError`, for an unbracketed IPv6 host
  (`@::1:5432/db`) — SQLAlchemy reaches `int("1:5432")`. Catching only `ArgumentError` would
  have let that escape the guard uncaught, carrying the URL, and therefore the password, into
  the traceback. Now caught, with a regression case.
- The accessor was first called `test_database_url`. Imported into a test module, pytest
  collects anything named `test_*` — so the function that hands out the destructive URL would
  have been *executed as a test case*. Renamed to `resolve_test_database_url`, with a test
  asserting the module exports no `test_`-prefixed callable.


### One thing the owner's role has to be able to do

Ten migration tests do not use the URL's database directly: they connect to the `postgres`
maintenance database with the same credentials and run `CREATE DATABASE
"<name>_test_<uuid8>"`, migrate it, then `DROP DATABASE ... WITH (FORCE)` at teardown. Each
drops only the uuid-suffixed database the fixture itself created moments earlier, so the
pattern is safe -- but the dedicated test role therefore needs **CREATEDB**, and the guard
cannot check what those throwaway databases are, only that the credentials came from a
validated URL. That is worth knowing before the role is created, because a role with no
CREATEDB fails those ten tests at fixture setup rather than at an assertion.

### What it does not do

- **It is not isolation.** A role that can reach production can still be made to destroy
  production by someone who means to; the guard is against accident. Isolation is a
  database-side property — a dedicated role with no production grants — and cannot be created
  from the client side. The admin-role check exists so that the `_test` suffix is not the only
  thing being trusted, but a correct deployment still has to create that role.
- **The schema check proves ownership, not safety.** An empty production database passes it.
- **It over-rejects a Unix-socket URL.** `postgresql:///db_test?host=/var/run/postgresql`
  has no host component, so the guard refuses it as ambiguous. That is the safe
  direction, but it means a socket-based test database needs a TCP URL instead.
- **No PostgreSQL test has run.** This is a guard, not coverage: every test above uses mocked
  configuration, and the authorised disposable database (B1) is still not configured, so the
  lease-contention, concurrent-worker, partial-failure, crash-recovery and migration tests the
  deletion outbox needs remain **NOT RUN**.
- **The CI path is unverified on GitHub.** `.github/workflows/ci.yml` now declares the
  container database disposable and exempts its superuser, but no workflow has run on either
  branch head, so that declaration has never been exercised.
- **It does not guard the application's own `DATABASE_URL`.** The migration step in CI points
  `DATABASE_URL` at the same disposable database and runs `alembic downgrade base`; that path
  is the application's configuration, not the test suite's, and is outside this guard.

---

## 9. The isolated PostgreSQL test cluster (Stage 1.7, 2026-10-20)

**Status: isolation VERIFIED. The selected PostgreSQL integration tests PASSED.
The backend suite as a whole has NOT run against PostgreSQL.**

Run of 2026-10-20: 16 of 16 isolation checks passed (exit 0), then **207 passed, 0 failed, 0
errors, 7 warnings in 122.81 s, process exit code 0**, against PostgreSQL 17.11 on
`127.0.0.1:55432`. The scope was the destructive-database guard, Alembic URL configuration,
the native PostgreSQL client arguments, the backup/restore round trip and the eleven migration
modules -- 207 tests of roughly 4,500. `docs/postgres-test-environment.md` section 6 records
the command, the scope and the three defects the run exposed. **Everything else in the suite
still runs on SQLite, and this is not a production-readiness claim.**

This partially resolves the PostgreSQL gate recorded in 5b and in section 7: a disposable,
authorized instance now exists, so the blocker is no longer "no database". What has *not*
happened is the run itself -- no application test has executed against PostgreSQL, and the
suite's PostgreSQL-gated tests skipped in every baseline recorded in this document.

What exists: a dedicated PostgreSQL 17.11 cluster in `C:\Users\deepi\AppData\Local\talviora-pgtest\data`, listening on
`127.0.0.1:55432` only (`0.0.0.0` listeners: zero), SCRAM-SHA-256 on two loopback
`pg_hba.conf` rules with no `trust` rule, a dedicated non-superuser role `talviora_test` with
`CREATEDB` and nothing else, and one database it owns. The owner set both passwords
interactively; neither was generated, printed, logged, or stored in either repository.

Evidence: all 16 isolation checks passed, exit code 0. The checks and their expected values
are tabulated in `docs/postgres-test-environment.md` section 5.1, and the two defects that
run exposed -- a Windows `bash` launch failure and a boolean-spelling mismatch in the check
itself, neither a cluster fault -- in section 5.2.

The existing services on ports 5432 and 5433 were not contacted in any stage. Isolation from
them is structural (roles are cluster-local), not measured, which 5.3 states explicitly.

---

## 8. Change control

No commit, no push, no deploy, no publish, no signing. No dependency installed, no model
downloaded, no paid API called, no production data touched, no secret printed — the `.env`
audit reported key names and value lengths only. Both working trees are left dirty with the
changes above visible in `git diff`.

## 10. Stage 2 — the account-deletion outbox (2026-10-20)

**Status: implemented. The selected PostgreSQL outbox tests PASSED. The full backend suite has
NOT run against PostgreSQL. Physical erasure is NOT verified.**

The outbox replaces the synchronous storage deletion that section 7 of this report described.
Phase 1 is one transaction -- copy every storage key into a durable job, revoke access by
bumping `token_version` and clearing `is_active`, overwrite the identifying columns, delete the
profile-owned rows -- and it makes no storage call at all, so a bucket outage can no longer
stop a user leaving. Phase 2 is a worker that claims jobs with `FOR UPDATE SKIP LOCKED`,
erases the objects, and deletes the last user row only after a re-count inside the closing
transaction.

Evidence, through `pgtest.py` so the 16 isolation checks ran first: **60 passed, 0 failed, 0
errors, 52.24 s, exit code 0**, against PostgreSQL 17.11 on `127.0.0.1:55432`. The scope was
the 16 PostgreSQL-gated outbox tests and the 44 unit tests;
`docs/proposal-account-deletion-outbox.md` section 12 records the design decisions, the four
tests whose meaning changed, and what remains unverified.

Migration `f7b3c91de204` is additive on the single head `d4a1c08f52e7`, and its `downgrade()`
refuses while any request is `pending` or `needs_attention`, with no override flag.

What this does **not** establish: that stored objects are physically unrecoverable. The
production bucket is unconfigured and its versioning, Object Lock, replication and retention
settings are unknown, so a successful delete call means the provider accepted the request.
Nor does any worker process exist -- `run_once` is a function, and nothing calls it on a timer.

### 10.1 Coverage gaps closed and the worker runner (2026-10-20)

Two scenarios that had been proven only on SQLite now have PostgreSQL tests: mixed outcomes in
one worker pass (one object fails, one succeeds) and a crash after the storage delete but
before the outcome is recorded. **Written, not yet run** -- they need the isolated cluster.

`scripts/run_deletion_worker.py` is the invocation surface, in the style of
`scripts/run_due_alerts.py`: bounded polling, graceful SIGINT/SIGTERM, a fresh session per
pass, the engine disposed once, and six counters that contain no key, credential or user
detail. It calls the tested worker and implements no claiming or SQL of its own. The web
application does not start it. 26 unit tests, no database required.

Still unverified: physical erasure, the provisional SLA, and whether the runner behaves under a
real scheduler -- no persistent process has been started.

## 11. Stage 3 — voice-resume continuity (2026-10-20)

**Status: root cause confirmed in both repositories and fixed. Focused tests pass. Manual
device verification still required.**

### 11.1 Root cause, with evidence

A reconnect accepted the grant and reported success while starting a different conversation.
The ordering was the defect, not the registry:

* `app/api/voice_session.py` had **no connect-time resume path at all**. The handshake went
  straight to `registry.open()`, which mints a fresh `session_id`. Everything downstream is
  keyed on that id -- `build_coach_responder(session_key="voice-" + session_id)` for the
  responder's memory and `ensure_conversation(session_key="voice-" + session_id)` for the
  durable transcript -- so a new id meant a new memory scope and a new transcript.
* The grant was consumed in `_handle_text` on a `resume` **message**, which by definition
  arrives after `accept`. At that point the new session, responder and conversation already
  existed. The handler set `record.resumed_from = grant.session_id` and rotated the token,
  then replied `session_accepted` -- so the client was told it had resumed.
* On the client, `WebSocketRealtimeTransport.buildUrl` never read `config.resumeToken`: the
  field was dead, and `RealtimeVoiceSession.reconnectLoop` sent the grant as a message after
  connecting.
* `app/voice/session_registry.py` was sound throughout -- single-use, owner-checked, TTL'd,
  quota'd -- which is why the defect survived: every registry-level test passed.

Nothing was missing from the architecture. `ensure_conversation` is idempotent on
`session_key`, so the transcript could always have been reattached. The only missing piece was
knowing the session id *before* anything was built.

### 11.2 The fix

* `registry.resume(grant=...)` re-registers the **original** session id, refusing when a live
  record still holds it. Not counted against `max_sessions_per_user`, because a reconnect must
  not fail precisely when a user is at their limit.
* The handshake resolves the grant before `registry.open()`, before the responder and before
  the transcript. Ownership first: `redeem_resume_token` checks the grant belongs to this
  authenticated user, and another user's grant is indistinguishable from an unknown one.
* `session_accepted` carries an additive `resumed` boolean. The server decides; the client no
  longer infers continuity from having sent a grant.
* The grant moved to the `X-Voice-Resume` **header**, not a query parameter as
  `docs/proposal-voice-resume-continuity.md` section 8 suggested. A URL reaches server logs,
  proxy logs and crash reports -- the same reason that client keeps the access token out of the
  query string -- and a header is readable before `accept`, which is the property the fix
  needs. This is a documented deviation from the proposal, in the direction the proposal's own
  client comment argues for.
* Compatibility is the proposal's **option A**: the `resume` message still answers, now with
  `resumed: false`, sets no `resumed_from`, and **does not consume the grant** -- so an old
  client's stale message cannot burn the grant an updated one would use.

### 11.3 Security and lifecycle behaviour

No check was weakened. Cross-user resume, expiry, replay and single use are now asserted at
the connect-time path where they actually apply, in addition to the registry unit tests that
already covered them. An ambiguous resume -- a live socket still holding the id -- is refused
and the client is given an honest new session rather than being joined to a transcript another
connection is writing. A dropped socket is still never recorded as `completed`.

### 11.4 Remaining gaps

* **Manual Android device verification is still required.** Everything here is JVM and
  TestClient: no physical device, no real microphone, no real network loss, no backgrounding.
* Multi-worker deployment still cannot resume across workers -- the registry is in-process.
  That is the documented single-worker beta constraint, unchanged by this stage, and the
  database-backed single-writer claim in the proposal's section 7 remains unbuilt.
* A turn in flight when the socket died is still not restarted, deliberately: it may already
  have written an interview answer.

## 12. Stage 4 — storage-erasure verification and worker readiness (2026-10-21)

Sections 1–11 stand; nothing earlier is withdrawn. This section records what the storage side of
account deletion actually guarantees, two defects found while establishing it, and which gates
remain open. The full audit, the per-case acceptance matrix and the owner decisions are in
`proposal-account-deletion-outbox.md` section 13; the operational procedures are in
`runbook-deletion-worker.md`.

### 12.1 The central finding

**Physical erasure is still not verified, and the reason is configuration, not testing.** There
is no production bucket to inspect: `STORAGE_PROVIDER` resolves to `local`, and the bucket,
endpoint, region and credential settings are all unset. No value was read or printed.

What the code proves is weaker than it reads. `S3StorageProvider.delete` calls
`delete_object(Bucket, Key)` with **no `VersionId`**. On an unversioned bucket that removes the
object; **on a versioned bucket it inserts a delete marker and every prior version remains
retrievable.** The adapter never queries bucket versioning, never enumerates versions, and has
no object-lock awareness. So on a versioned bucket, the system as written does not erase — it
hides. Whether that is the production situation is unknown and is an owner decision, not a
test gap.

Replication, lifecycle rules and backup retention are outside the code's knowledge entirely and
always will be; they can only be settled with provider configuration evidence.

No API response, log line or document was found to overclaim erasure. That part needed no
correction.

### 12.2 Defect 1 — a job could be erased against the wrong backend and report success

`deletion_jobs.storage_provider` was written at enqueue and **never read anywhere**. The worker
constructed one provider from its own settings and applied it to every claimed job.

The failure mode is a silent success rather than an error. An object key is only meaningful to
the backend that stored it, so deleting it elsewhere finds it absent; absence is idempotent
success by contract; the job is marked `done`; and closing the request deletes the last row that
held the key. The object remains where it is, unreachable, for a user who was told it was erased
— the one invariant the outbox exists to protect.

The route in is ordinary. The startup guard in `app/core/config.py` actively pushes a deployment
to move `STORAGE_PROVIDER` from `local` to `s3`, and any job enqueued before that change is a job
whose recorded backend no longer matches the worker's. Two processes reading different
environments produce the same mismatch.

**Fix:** each adapter declares its `name`; `execute_job` refuses a job whose recorded provider
differs from the one it holds, flagging it `needs_attention` on the first occurrence because a
configuration fault cannot clear by being retried. Nothing is deleted, the key and the row
survive, the request cannot close, the user row stays, and the job remains claimable — so
correcting the setting is the entire remedy, with no manual requeue. A provider that declares no
name is not second-guessed, and a parity test asserts both real adapters declare one.

### 12.3 Defect 2 — the failure field carried one bit of information

Both adapters wrap every failure in the single `StorageError` class, and there are no
subclasses, so `last_error_class` was the constant string `"StorageError"` for every failure
either backend can produce. An operator triaging a `needs_attention` job could not tell a denied
permission or a retention lock — which never succeeds unattended — from throttling, which clears
on its own. The provider's message is deliberately withheld because it quotes the bucket and the
key, so nothing carried the distinction.

**Fix:** `StorageError` gained a `code` holding the backend's own short machine token (an S3
error code, a botocore transport class, or an `errno` name), and the worker records
`StorageError:AccessDenied` in place of the bare class. The message is still never stored or
logged, and a failure with no code records the class alone.

### 12.4 Test evidence and its limits

| Command | Result | Exit | Duration |
|---|---|---|---|
| `pytest tests/unit/test_storage.py` | 16 passed (12 + 4 new) | 0 | 20.7 s |
| `pytest tests/unit/test_deletion_outbox.py` | 52 passed (44 + 8 new) | 0 | 12.7 s |
| `pytest` — storage, outbox, account service, runner, PG outbox | 110 passed, **20 skipped** | 0 | 36.9 s |
| `pytest tests -q -rs` (full backend) | **4651 passed, 25 skipped, 0 failed** | 0 | 1:34:12 |
| `ruff check app scripts tests` | All checks passed | 0 | — |

4639 before this stage, 4651 after: the 12 new tests and nothing else.

**Limits.** Every storage provider in these tests is a stand-in. They prove the worker's state
machine, its retained references and the absence of leakage; they prove nothing about any real
bucket, and no test in this repository has ever deleted a real object.

**The 20 PostgreSQL outbox tests were skipped, not run** — no `TEST_DATABASE_URL` was supplied
and credentials are never probed for. They passed on the isolated cluster during Stage 2 and
were **not re-executed this stage**. Their storage doubles declare no `name`, so the new
wrong-backend check is inert there and their existing `"StorageError"` assertion still holds;
that is a reading of the code, not an execution. Re-running them under the guarded command
remains an owner action.

### 12.5 Gate status

| Gate | Status |
|---|---|
| Logical deletion requested, both adapters | **PASS** |
| Wrong-backend erasure refused, obligation preserved | **PASS** |
| Failure triage possible from the job row | **PASS** |
| Transient failure, retry, backoff, idempotent re-delete | **PASS** |
| Crash between the delete and the status write | **PASS** (on PostgreSQL, Stage 2; not re-run) |
| No terminal status can abandon an obligation | **PASS** |
| Full backend regression | **PASS** — 4651 passed, exit 0 |
| Android unit tests | **PASS** — unchanged this stage; no Android file was touched |
| **Physical erasure in production** | **NOT VERIFIED** — no bucket configured; versioning unknown |
| Versions and delete markers | **BLOCKED** — owner decision on versioning |
| Object Lock / retention | **NOT VERIFIED** — needs a live locked object |
| Replication, lifecycle, backups | **NOT VERIFIED** — needs provider configuration evidence |
| PostgreSQL outbox suite, this stage | **NOT VERIFIED** — 20 tests skipped |
| Worker deployed, scheduled, monitored | **BLOCKED** — runbook written, nothing deployed |
| Staging validation of the worker | **NOT VERIFIED** — procedure written, never run |
| Pickup / alert / escalation targets | **BLOCKED** — provisional, pending privacy and legal approval |

### 12.6 Unchanged production gates

Everything in section 11.4 still stands: visual UI (0 of 97 screens), Linux TTS unproven,
GitHub CI never run on either branch head, the full backend suite never run against PostgreSQL,
and voice resume confined to a single worker process. Physical Android device verification was
reported by the owner as manually confirmed; this session did not observe it and does not
attest to it.

**TALVIORA AI is not production-ready.** A green regression suite on a developer machine with
no object storage configured does not establish that an account's files are erased.
