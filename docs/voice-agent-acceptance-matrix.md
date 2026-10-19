# Voice Agent — acceptance matrix, VA-0 … VA-12

Companion to [`voice-agent-locked-spec.md`](voice-agent-locked-spec.md), which holds the
requirements and their provenance. This file holds **status only**. Created to close finding
**D5** ([`va12-final-audit.md`](va12-final-audit.md) §11).

**Date of this revision:** 2026-10-13. Every status below was re-verified against the working
tree on that date, not carried from a previous report.

---

## The seven states, and what each one does *not* mean

These are deliberately not synonyms. Most of the project's false-confidence incidents came from
collapsing two of them.

| State | Means | Does **not** mean |
|---|---|---|
| **IMPLEMENTED** | The code exists and does the thing. | That anything calls it. |
| **CONNECTED** | A real user-reachable path reaches it. | That it was tested on that path. |
| **TESTED** | Automated tests exercise it and pass. | That the tests exercise real hardware, a real socket, or a real provider. |
| **ACCEPTED** | Validated under the conditions the requirement names — including a physical device where the requirement says so. | — |
| **BLOCKED** | Cannot progress without something unavailable (a device, a credential, an approval). | That it failed. |
| **NEEDS TEST** | Implemented and connected; the test that would accept it has not been run. | That it is broken. |
| **OWNER DECISION REQUIRED** | An engineering answer cannot settle it. | That engineering is finished. |

**The rule this project keeps relearning: a connected UI plus green JVM tests is `TESTED`, never
`ACCEPTED`.** Every one of the 1,400-odd Android unit tests fakes the transport and every piece
of audio hardware. Nothing in this repository has yet held a spoken conversation on a phone.

---

## Matrix

### VA-0 — baseline and injection containment

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Baseline recorded; no deployment image claimed | **ACCEPTED** | `docs/PRODUCTION_DEPLOYMENT.md`; repo inspection | — |
| Untrusted input cannot forge prompt delimiters | **ACCEPTED** | `app/ai/input_guard.py`; mutation-verified (M18–M20), ~20 guarded call sites | — |
| Model output cannot leak scaffolding | **ACCEPTED** | `app/ai/output_guard.py`; parity test | — |

### VA-1 — shared Voice Core (Android)

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Ports abstraction, typed session states | **TESTED** | `voice/core/`; 59 JVM tests | device run |
| Focus requested around capture and playback; abandoned on teardown | **TESTED** | `VoiceSession.kt:118,177`; `VoiceInterviewManager.kt:77,93` — both paths use the one `AudioFocusController` port | device run |
| Focus loss stops capture rather than ducking | **TESTED** | `FakeFocus`-driven tests | device run |
| Regaining focus never silently restarts recording | **TESTED** | `RealtimeVoiceSession.kt:594` | device run |
| **Mounted on a real screen** | **CONNECTED** | `VoiceAgentScreen` → `createRealtimeVoiceSession`; drawer → `Screen.VoiceAgent` → `NavGraph.kt:383` | — |

### VA-2 — authenticated realtime transport

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Identity from the handshake bearer token only | **TESTED** | `app/api/voice_session.py`; no message carries a user id | device run |
| Session lifecycle, resume with rotating token, expiry | **TESTED** | `app/voice/session_registry.py`; VA-2 WS suite | device run |
| Bounded frames, turn bytes and turn seconds | **TESTED** | `app/voice/turn_channel.py`; `tests/unit/test_voice_turn_channel.py` (28) | — |
| One versioned vocabulary; no second event set | **TESTED** | `realtime_protocol.py`; `FORBIDDEN_SERVER_MESSAGE_TYPES` is **derived**, so adding a type cannot leave a guard passing | — |
| Gateway emits nothing it cannot substantiate | **TESTED** | five protocol guards, two of which were found blind to multi-line sends and `send_bytes` and widened | — |
| Client/server parity: types, fields, binary layout, modes, handshake params | **TESTED** | `tests/unit/test_realtime_protocol_parity.py` — reads the real Kotlin source | — |
| **A client actually constructs the socket** | **CONNECTED** | `NetworkModule.createRealtimeVoiceSession` | — |
| Out-of-order **inbound audio** detection | **NOT APPLICABLE** | inbound audio frames carry no sequence number by VA-2 design (20 ms raw PCM; an envelope would inflate the most frequent message type). Stale turn/generation ids on *control* messages are rejected. | — |

### VA-3 — speech

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Contracts defined; provider evaluated, not chosen | **ACCEPTED** | `app/speech/`; `docs/speech-provider-evaluation.md` | — |
| Defaults remain `none` | **TESTED** | `Settings(_env_file=None)` asserted in `tests/unit/test_development_speech_config.py` | — |
| Production rejects development-only adapters | **TESTED** | `validate_for_production()`; the artifact's **own values** are fed to the real validator | — |
| Unavailable adapter yields the typed condition, never a transcript | **TESTED** | `SpeechNotConfigured` → `stt_unavailable` / `tts_unavailable` | — |
| Empty synthesized audio treated as failure | **TESTED** | `turn_runtime.py`; asserted in the end-to-end run | — |
| Failed TTS still returns the answer with `spoken: false` | **TESTED** | `assistant_message(spoken=False)`; parity test pins the field's default to `false` | — |
| **Development speech deliberately configured** | **CONNECTED** | `.env.development.speech` (reviewable, secret-free), applied **per server process**, never through `.env` -- the suite loads `.env` and `conftest.py` pins neither provider, so that route silently rewrote what the tests covered | — |
| **Real STT ran** | **ACCEPTED (this host)** | faster-whisper `tiny.en`, 115 894 PCM bytes @ 22 050 Hz → 8 of 8 words | not evidence for any other host |
| **Real TTS ran** | **ACCEPTED (this host, Windows SAPI5)** | 115 940 bytes, decodable WAV 22 050 Hz mono 16-bit, 2.628 s, peak amplitude 30 721 | — |
| Linux TTS | **NOT PROVEN** | `tests/unit/test_tts_linux_contract.py` **skips**: no host speech driver | a Linux host with espeak-ng, or a different adapter |
| Production speech provider | **OWNER DECISION REQUIRED** | none approved; ElevenLabs excluded | an approval |

### VA-4 / VA-5 / VA-6 — tools, world data, orchestration

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Closed read-only registry; no arbitrary execution; entitlement enforced | **ACCEPTED** | mutation-verified (M05, M05b, M11); 36 security + 23 contract tests | — |
| `turn_runtime.py` imports no tools/agent/services | **TESTED** | `tests/unit/test_tool_registry_contracts.py` | — |
| World data with provenance and freshness; honest absence | **TESTED** | 108 tests, with fakes | — |
| Live market data | **BLOCKED** | `job_provider` is `none`; the Null provider makes the agent say so rather than fabricate | owner decision |
| Orchestration: intent, planning, bounded context, grounding, memory; no writes | **TESTED** | 106 tests | — |
| **Reached by a real flow** | **CONNECTED** | `responders.build_coach_responder` → `Orchestrator.coach()` | — |

### VA-7 — safe actions

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Confirmation bound to user/capability/arguments; expiring; single-use | **ACCEPTED** | mutation-verified (M06–M15, M26); 96 backend tests | — |
| Idempotency per logical operation; deliberate retry reuses the key | **TESTED** | `ActionConfirmationViewModelTest` (21) | device run |
| Outcomes verified against authoritative state | **TESTED** | saved / already-satisfied / failed / expired / unknown kept distinct | — |
| Uncertain mutation reconciled, never auto-retried | **TESTED** | `an unknown outcome is never retried automatically` | — |
| Per-user rate limits on preview and execute | **TESTED** | `app/api/actions.py` `@limiter.limit("60/hour")` ×2 | — |
| Confirmation host mounted | **CONNECTED** | `VoiceAgentScreen` mounts `ActionConfirmationHost` | — |
| **A real listing supplies `listing_id`** | **CONNECTED** | `JobDetailScreen` → `onDiscussWithCoach` → `voiceAgentRoute(COACH, listingId)`; the control renders only when a listing is in scope | device run |
| Argument correctness (`listing_id`, never `job_id`, never empty) | **TESTED** | `VoiceAgentReachabilityTest` reads the production source | — |
| A spoken "yes" cannot confirm | **TESTED** | exactly **one** call site of `confirmations.request`, and it is the button's lambda; the reply is never parsed for intent; the voice catalogue has no ACTION tool; `operation_outcome` is forbidden | device run |
| No new high-risk actions | **ACCEPTED** | catalogue unchanged | — |

### VA-8 — Career Coach

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Evidence-grounded; no fabrication, no promises | **ACCEPTED** | 97 tests; promise language unconstructible | — |
| Spoken turn reaches the orchestrator | **CONNECTED** | `build_coach_responder`; `MAX_SPOKEN_CHARACTERS = 1_200` with an explicit trim note | — |
| Reachable by a student | **CONNECTED** | drawer "Voice Career Coach"; and from a job advert with that listing in scope | — |
| An actual spoken coach conversation | **BLOCKED** | no device | a phone |

### VA-9 — Interview Coach

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Spoken turn reuses the interview engine | **ACCEPTED** (backend) | 42 tests; mutation-verified (M01, M02, M16, M17) | — |
| Replay- and concurrency-safe; no double advance | **ACCEPTED** (backend) | `voice_interview.submit_spoken_turn`; `InterviewResponder._retry_next_question()` retries question generation only, never re-submits the answer | — |
| Ownership verified **before** the socket is accepted | **TESTED** | `build_interview_responder` → `interview_service.owned_question`; one answer for unknown, missing and someone else's | — |
| Cancelled turn reports text, never audio | **TESTED** | `assistant_message(spoken=False)` | — |
| **Reachable with authoritative ids** | **CONNECTED** | `VoiceInterviewScreen` → `onLiveConversation(current.interviewId, current.question.id)` → `voiceAgentRoute(INTERVIEW, …)`; ids come off the server-confirmed `Turn`, never a local index | device run |
| Return lands on the authoritative state | **TESTED** | `VoiceInterviewHandoffTest` (11): re-reads after a hand-off, re-reads **nothing** otherwise, survives process death, lands on `Completed`, errors rather than redrawing a stale question | device run |
| Typed-answer and file-based audio interview preserved | **TESTED** | `VoiceInterviewLifecycleTest` (13) unchanged and green | — |
| Spoken end-to-end on a device (STT → transport → engine → TTS) | **BLOCKED** | no device | a phone |

### VA-10 — hardening

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| Critical guards demonstrably load-bearing | **ACCEPTED** | mutation programme; realtime increment: 12 caught, 1 redundant, 1 structurally unreachable, 0 file drift | — |
| Secrets absent from logs and errors | **ACCEPTED** | canary-measured across 9 surfaces | — |
| Bounded memory on all process-local state | **ACCEPTED** | unit-proven bounds; `MAX_RENDERED_TURNS = 50` on the UI side | — |
| Rate limiting on every authenticated mutation boundary | **ACCEPTED** | D4 closed; re-verified in this increment | — |
| Voice diagnostics carry no transcript or free text | **TESTED** | `VoiceDiagnostics` events expose a closed set of typed fields (ids, byte counts, booleans) | — |
| Process-local session state across workers | **OWNER DECISION REQUIRED** | documented single-worker beta constraint; a missing grant after restart fails honestly | a shared store is a new dependency |

### VA-11 — physical-device acceptance

Every row below is **BLOCKED**, on the same thing, with the same evidence:

```
$ adb devices -l
List of devices attached            (empty)
$ adb shell getprop ro.product.model
adb.exe: no devices/emulators found
```

| Scenario | Status |
|---|---|
| Microphone permission and real capture | **BLOCKED** |
| Playback and audio focus under real interruption | **BLOCKED** |
| Rotation / background / foreground | **BLOCKED** |
| Wired and Bluetooth routing | **BLOCKED** |
| Interruption and cancellation | **BLOCKED** |
| Network loss and recovery | **BLOCKED** |
| Repeated turns and resource cleanup | **BLOCKED** |
| An actual spoken Career Coach conversation | **BLOCKED** |
| An actual spoken Interview Coach conversation | **BLOCKED** |

No emulator was substituted. See `VOICE_DEVICE_ACCEPTANCE_VA11.md` for the per-scenario
procedure and §1b for what each layer's constructor is.

### VA-12 — integration closure

| Requirement | Status | Evidence | Remaining |
|---|---|---|---|
| The three halves connected, not merely each tested | **CONNECTED** | `turn_channel.py` + `responders.py` + rewritten gateway; Android client mounted | device run |
| Interview mode reachable with valid ids | **CONNECTED / TESTED** | this increment | device run |
| Save-job confirmation reachable from a real listing | **CONNECTED / TESTED** | this increment | device run |
| Development speech deliberately configurable | **CONNECTED / TESTED** | this increment | — |
| Acceptance criteria version-controlled | **IMPLEMENTED** | this file and the locked spec; requirements traced to `LS §10`–`§12` and `§17` from the inspected PDFs | the three normative PDFs are readable on this host but **outside version control** — locked spec §0 |
| `PROJECT_HANDOFF.md` status corrected | **IMPLEMENTED** | `docs/project-handoff-status-correction.md`; original preserved | owner decides which document is authoritative |
| Multi-worker resilience | **OWNER DECISION REQUIRED** | — | — |
| Realtime voice agent in V1 scope at all | **OWNER DECISION REQUIRED** | `PROJECT_HANDOFF.md` names Voice Interview and not a realtime agent | a scope call |

---

## Metrics

`LS §13.3 (p.18)` names six required performance metrics. As of 2026-10-14 the server-side
stages are instrumented (`app/voice/turn_metrics.py`) and measured; the ones that need a handset
or a per-tool boundary the active path does not expose remain unmeasured, and are not estimated
from the server figures.

| Metric required by `LS §13.3` | Status |
|---|---|
| Time from user stop to first spoken response | **NOT MEASURED** — needs a device. The *server* portion is now known (below), but it excludes capture, upload, playback and the network |
| Time to first tool invocation and tool completion | **NOT MEASURED** — the coach runs its whole read fan-out inside one synchronous `Orchestrator.coach()` call, so the runtime sees one `respond` boundary. Which capabilities ran *is* recorded (`tools_used`); when each started is not available and is not invented. Same limitation that makes `tool_call_started` a forbidden frame |
| Interruption latency | **PARTIAL, honestly labelled** — `cancelled_at` names the checkpoint that observed cancellation and `total_ms` says when. That is observation latency, not time-until-work-stopped: a recogniser already running in a worker thread is not interrupted, its result is discarded |
| Reconnect time | **NOT MEASURED** — a client-side figure |
| Voice provider error rate | **MEASURABLE** — every turn emits a typed `outcome=`, so a rate per category is an aggregation over these lines. No counter service was added |
| Tool success/failure rate | **PARTIAL** — `tools_used` records which capabilities ran; per-tool success is not exposed to the runtime |

### Measured server-side turn, 2026-10-14

Host: Windows (`win32`), `ENVIRONMENT=development`, `STT_PROVIDER=faster-whisper`,
`TTS_PROVIDER=pyttsx3` — exported per process, not via `.env`. Input: a real 2.628 s synthesized
utterance at 22 050 Hz mono, recognised by real CPU inference. Three turns on one runtime,
verbatim from the emitted metrics lines:

| Turn | total_ms | stt_ms | tts_ms | tts_first_audio_ms | rtf | outcome |
|---|---|---|---|---|---|---|
| 1 — **cold**, includes loading the weights | 8 484 | 8 266 | 218 | 218 | 3.147 | ok |
| 2 — warm | 2 515 | 2 312 | 202 | 202 | 0.880 | ok |
| 3 — warm | 2 233 | 2 014 | 219 | 219 | 0.767 | ok |

`respond_ms=0` throughout: the responder was a stub, because reaching the real orchestrator would
need a database and an AI provider this run may not assume. `transcript_chars=44`,
`speech_bytes=69 916`, reply decoded as a 22 050 Hz mono WAV with 34 935 frames.

**What these numbers mean.** Steady-state recognition on this host is *faster than real time*
(rtf 0.77–0.88), so a 3-second answer costs roughly 2 seconds to transcribe. The first turn after
start-up costs ~8.5 s because the weights load then; a long-lived server pays that once, but the
first student of the day waits for it. Synthesis is ~200 ms and is not the bottleneck.

**What they are not.** Server-side only: Windows host, one utterance, synthesized rather than
human speech, a stub responder, and no device, network or playback in the path. They are not a
spoken-turn latency as a student experiences it, and `adapters` are named in every line
(`stt=FasterWhisperSpeechToText`) precisely so a figure taken against a fake cannot be mistaken
for one of these.

Related figures that exist, and what they are not:

| Figure | Status |
|---|---|
| Recognition accuracy on real student speech | **NOT MEASURED** — 8 of 8 words on *synthesized* speech is not an accuracy figure |
| Audio quality as heard on a handset | **NOT MEASURED** |

## Pre-device acceptance gate — determinations of 2026-10-14

Three items were checked for availability rather than assumed. Each is recorded with the exact
prerequisite that would unblock it, so nobody has to rediscover it.

### PostgreSQL concurrency — BLOCKED

| | |
|---|---|
| `TEST_DATABASE_URL` in the shell | **unset** |
| In `.env` | **absent** (0 occurrences) |
| Anywhere else in the repo | only `.github/workflows/ci.yml:138` |

The sole declaration is CI's, pointing at a GitHub Actions `services:` container created fresh
per run. That credential is not authorised for local use and is recorded as rejected locally. A
server *is* listening on this host's port 5432, but its provenance is unknown and connecting to
it would be both probing unknown credentials and risking a real database — so it was not
attempted.

**Prerequisite to unblock:** an explicitly authorised, disposable PostgreSQL URL supplied by the
owner, e.g. `TEST_DATABASE_URL=postgresql+psycopg2://<user>:<pass>@<host>:5432/<throwaway_db>`.
The fixture then uses it with a 15 s `lock_timeout` and needs no code change.

**A gap worth fixing while this is open.** CI's PostgreSQL step runs `pytest -q tests/api`
(`ci.yml:141`). The business-claim concurrency tests live in `tests/unit/test_business_claim.py`,
so **CI has never run them against PostgreSQL either** — the gap is not merely local. Adding
that file to the PostgreSQL step would close it using infrastructure the owner already
authorises. Not applied here: a CI change cannot be validated from this workstation, so it is
the owner's call.

### Linux TTS — NOT PROVEN

| | |
|---|---|
| Host platform | Windows (`sys.platform != "linux"`) |
| WSL | **not installed** — `wsl -l -v` → "The Windows Subsystem for Linux is not installed." |
| Other Linux runtime | none available |

`tests/unit/test_tts_linux_contract.py:112` skips on exactly this condition, and its skip text
already states the consequence. Installing WSL or `espeak-ng` is not permitted in this
increment, and Windows SAPI5 success is not evidence about Linux.

**Prerequisite to unblock:** a Linux host (or WSL distribution) with the `espeak-ng` OS package
installed and this project's Python environment available, then run
`pytest tests/unit/test_tts_linux_contract.py`. The test asserts a real PCM WAV at a sane sample
rate because the consumer is Android `MediaPlayer`, not merely a file beginning with `RIFF`.

### Physical device — BLOCKED

```
$ adb devices -l
* daemon not running; starting now at tcp:5037
* daemon started successfully
List of devices attached
                                  (empty)
```

A fresh ADB daemon, exit 0, zero devices. No emulator substituted. `LS §12 (p.17) VA-11`
requires a physical device and explicitly forbids a production-quality claim from emulator-only
testing.

## Release gates

Nothing below is satisfied by code review or by JVM tests.

| Gate | State |
|---|---|
| G1 — physical-device acceptance (VA-11, all nine scenarios) | **BLOCKED** — no device |
| G2 — a spoken Career Coach conversation on a handset | **BLOCKED** |
| G3 — a spoken Interview Coach conversation on a handset, with the interview advancing correctly | **BLOCKED** |
| G4 — a speech provider approved for production, proven on the deployment OS | **OWNER DECISION REQUIRED**; Linux TTS **NOT PROVEN** |
| G5 — measured latency budget for a spoken turn | **NEEDS TEST** — nothing measured |
| G6 — session/confirmation/memory state safe at the deployed worker count | **OWNER DECISION REQUIRED** |
| G7 — the normative specification placed under version control by the owner | **OWNER DECISION REQUIRED** |
| G8 — `PROJECT_HANDOFF.md` V1 scope confirmed or revised | **OWNER DECISION REQUIRED** |

## Test evidence for this revision

Recorded from JUnit XML and pytest's own exit code, not from `BUILD SUCCESSFUL` and not from a
trailing shell status. Background-task notifications have reported "exit code 0" for a run in
which pytest exited 1 -- that was the trailing `echo`, and it is why every row below cites
pytest's own code.

| Run | Result |
|---|---|
| Backend full suite, **2026-10-14** | **4 237 passed, 3 skipped, 0 failed** in 1:23:17 — pytest exit **0** |
| Focused voice/concurrency/speech/WS suites | 388 passed, exit 0, 6:04 |
| `tests/unit/test_voice_turn_metrics.py` | 18 passed, exit 0 |
| `ruff check app/ tests/` | clean, exit 0 |
| `alembic upgrade head` (disposable SQLite) | exit 0 |
| `app.db` | md5 `496b29df1acb1d5c9ec4d86da2a0430c`, unchanged, mtime Sep 28 |
| Android `testDevDebugUnitTest` (2026-10-13, unchanged since) | 1 429 tests, 0 failures, 0 errors, 0 skipped, 162 suites |
| Android `testProdDebugUnitTest` | 1 429 tests, 0 failures, 0 errors, 0 skipped |
| Android source `@Test` count | **1 429** — exact match with both variants |
| Android `lintDevDebug` | 0 errors, 32 warnings, 1 hint; Gradle exit 0 in 5m 13s |

No Android file has changed since that run, so it is cited rather than repeated. The earlier
backend run of 2026-10-13 ended **1 failed / 4 217 passed, exit 1**; that failure was D6 and its
closure is recorded below.

The three skips are unchanged and each states its own reason: Linux TTS (no host driver),
PostgreSQL-specific partial indexes, and the backup/restore round-trip (needs `TEST_DATABASE_URL`).

### D6 — a timing-dependent concurrency test *(found 2026-10-13, closed 2026-10-14)*

The one failure in the 2026-10-13 full run was
`tests/unit/test_business_claim.py::TestRegenerate::test_concurrent_requests_generate_once_and_a_later_one_generates_again`
— *"three overlapping requests generated 2 times"*. Unrelated to the Voice Agent, but it bears
directly on an acceptance row: `LS §17` requires that "duplicate/replayed/raced voice commands
cannot corrupt state", and `LS §11 VA-7` lists "concurrent commands" among its key tests.

**Root cause: a test-harness synchronisation defect, not a product defect.** Measured, not
assumed:

- 1 failure in 15 isolated runs (~7 %), same symptom each time.
- A probe showed that when the two follower threads are held past the owner's settlement,
  **3 of 3 runs generate three times** — the documented `on_settled=REGENERATE` policy for a
  request that arrives after a settled claim.

`run_business_once` sets `waited = True` only for a request that *observed* the claim in flight;
such a request is then handed the owner's result even once the claim has settled. The old test
released the owner the instant it entered `perform`, which is before the followers had read the
row at all, so a follower could arrive post-settlement and correctly regenerate. Both outcomes
were right for the request actually made; the test could not tell them apart.

**The fix** replaces the start barrier and the two sibling `time.sleep(0.4)` waits with
`WaiterWatch`, which witnesses the production act itself: `run_business_once` reaches `time.sleep`
from exactly one place, the branch taken after reading the claim as `claimed` and not stale. The
owner is released only once the required number of followers have provably joined, and each test
asserts that count, so a run that failed to exercise coalescing fails loudly instead of passing
for the wrong reason. The post-settlement policy is now a separate, deterministic test.

| Evidence | Result |
|---|---|
| `tests/unit/test_business_claim.py` | 26 passed, exit 0 |
| The 4 concurrency tests, 25 isolated runs | **25 passed, 0 failed** |
| All concurrency + claim-guard suites (11 files) | **164 passed**, exit 0, 4:28 |
| Mutation `M-A`: `waited = True` → `False` | **CAUGHT** |
| Mutation `M-B`: settled claim ignores `waited` | **CAUGHT** |
| Restored-file drift | **none** — `app/services/idempotency.py` SHA-256 identical before and after |

Both mutants are caught by the two coalescing tests and correctly leave the REPLAY waiter test
passing, since for `on_settled=REPLAY` the result is owed regardless of whether the request
waited.

**PostgreSQL remains NOT PROVEN.** The fixture already supports it — set `TEST_DATABASE_URL` and
it uses PostgreSQL with a 15 s `lock_timeout` — but every result above was produced on a
file-backed SQLite engine. SQLite serialises writers with one database-level lock; PostgreSQL
arbitrates per row, and the post-rollback re-read depends on READ COMMITTED taking a fresh
snapshot per statement. The production code is written for that model (`_insert` rolls back
explicitly and says why) but has not been executed against it. A server is listening on this
host's port 5432, no `TEST_DATABASE_URL` is set, and credentials are never probed for — the same
reason `tests/scripts/test_backup_restore_roundtrip.py` skips. The distinction is documented in
the `engine` fixture's own docstring so it travels with the test.

---

## Premium voice upgrade — determinations of 2026-10-17

Appended, not edited: every row above is preserved. Full detail in
[voice-agent-premium-upgrade-research.md](voice-agent-premium-upgrade-research.md).

| Item | Status | Evidence / prerequisite |
|---|---|---|
| Voice state visualiser (12 states, reduced-motion aware) | **PASS** (JVM) | `VoiceOrbHonestyTest` 6/6; full Android suite 1440 tests, 0 failures, exit 0 |
| Spoken-text rendering (URL/markdown safe, meaning preserved) | **PASS** | `tests/unit/test_speech_text.py` 19 tests; 70 passed with turn-runtime, exit 0 |
| Premium female TTS (Kokoro `af_heart` / `af_bella`) | **BLOCKED** | Apache-2.0 code + weights. Needs authorization to `pip install kokoro` and download ~330 MB weights before any audition |
| Recorded voice audition on the five sample scripts | **NOT RUN** | Depends on the gate above. Publisher grades are not an audition |
| Casual conversational persona (`LS` conflict) | **BLOCKED** | `_speak_coaching` renders grounded claims and adds nothing; a phrasing pass would convert verified claims into model-authored text. Owner decision, three options recorded |
| Silero VAD adoption | **BLOCKED** | Landing page shows MIT *and* a conflicting CC BY-NC 4.0 badge; licence must be resolved from the repo's licence files |
| Pipecat / LiveKit adoption | **NOT APPLICABLE** | Both are frameworks that would replace the FastAPI/WebSocket layer; LiveKit additionally needs WebRTC infrastructure |
| Streaming TTS chunking | **NOT APPLICABLE** | No approved streaming adapter exists; `faster-whisper` is turn-based |
| Telugu speech | **NOT SUPPORTED** | Absent from Kokoro's published language list; must not be advertised |
| Device voice acceptance after this change | **BLOCKED** | `adb devices -l` empty |

---

## Unified conversation history - determinations of 2026-10-17

Appended, not edited. Every row above is preserved.

| Item | Status | Evidence / prerequisite |
|---|---|---|
| Durable transcript store (conversations, conversation_turns) | **PASS** | Migration d4a1c08f52e7; upgrade and downgrade -1 both exit 0 on a disposable DB |
| Incremental save (per completed turn, before TTS) | **PASS** | `_persist_turn` called before any audio frame; 19 service tests |
| Idempotent replay on reconnect | **PASS** | UNIQUE (conversation_id, turn_id, role); replay returns None |
| Survives a server restart | **PASS** | Engine disposed, new engine on same file, cold identity map; turns read back |
| Gateway actually writes a transcript | **PASS** | 5 integration tests through a real authenticated WebSocket |
| History REST endpoints (list / detail / delete) | **PASS** | 13 route tests: filter, pagination bound, 422 on unknown kind, 404 on foreign id |
| Owner isolation | **PASS** | Profile derived from the token; foreign ids return 404, never 403 |
| No raw audio persisted | **PASS** | No audio column exists; decision cannot be taken accidentally |
| Interrupted sessions reported honestly | **PASS** | `ended_status` defaults to interrupted; never rewritten to completed |
| Existing voice path preserved | **PASS** | 516 passed, exit 0 (481 baseline + new) |
| Android unified history UI | **NOT RUN** | Backend only this increment; no Compose screen added |
| Adaptive interview length | **FAIL (unchanged)** | `interview_service._total_questions()` and `stages.SIMULATION_QUESTION_COUNT` still fix the count |
| Richer per-answer evaluation format | **NOT RUN** | `interview_evaluations` exists; the 11-part feedback shape was not implemented |
| Resume an unfinished interview from history | **NOT RUN** | Store supports it (session_key); no resume journey built |
| Device verification of history | **NOT RUN** | Requires a handset and the Android UI |

## Voice agent repair -- determinations of 2026-10-18

Three reported defects, three independent root causes. None of them was where the report
pointed.

### 1. "Does not respond to normal conversation" -- not a bug, an absent code path

`build_coach_responder` runs the grounding orchestrator and renders the result through
`_speak_coaching`, which emits **only** grounded claims: four evidence sections, plus unknowns,
failures and a clarifying question. Its own comment states the rule -- "a summary of evidence is
a new claim about it".

"Hey, how are you?" produces no claim about anybody's records, so nothing was rendered and the
student heard the fixed fallback:

> "I checked, and I do not have anything recorded to tell you about that yet."

There was no intent gate rejecting chat, no career-only filter, no restrictive system prompt --
and no conversational path at all. The architecture had exactly one kind of reply.

**Fix.** A turn with no claims *and* no failures now goes to `app/voice/conversation.py`, which
prompts the configured provider with a persona and bounded context. The grounding rule is
preserved rather than broken: chat replies carry `grounded=False`, so model-authored text stays
distinguishable from evidence everywhere downstream. A turn with claims never reaches the model
-- asserted by `test_voice_chat_routing.py`, which pins both directions.

**Context source.** The agent's `SessionMemory` could not serve this: it records turn *metadata*
(intent, tools used, whether answered) and holds no transcript text. Context therefore comes
from the persisted conversation store the gateway already writes every turn into, looked up by
(profile_id, session_key) so a session key alone cannot reach another student's transcript.
Bounded to `MAX_CONTEXT_TURNS = 8`, matching `MAX_SUMMARY_TURNS` so the two notions of "recent"
do not disagree.

### 2. "Voice is not female" -- the voice was installed and never selected

Measured on the development host:

| voice | median F0 | verdict |
|---|---|---|
| `TTS_MS_EN-US_DAVID_11.0` (the system default, used until now) | **92.3 Hz** | male |
| `TTS_MS_EN-US_ZIRA_11.0` (installed the whole time) | **191.7 Hz** | female |
| provider output after the fix | **191.7 Hz** | matches Zira exactly |

`synthesize_speech` never called `setProperty("voice", ...)`, so SAPI5 used the host default,
which is David. A female voice was already present. **No installation and no download was
needed for this**, which is why it was evaluated before the Kokoro proposal.

Expressed as `tts_voice_gender` (default `"female"`) rather than a voice name, so the setting
still means something on a Linux host running espeak-ng, whose voice ids are unrelated. An
unmatched preference keeps the host default rather than failing synthesis.

**The honest limit:** this fixes the *development* host only. SAPI5 is Windows. On Linux the
driver is espeak-ng, whose quality is poor and whose contract remains unproven --
`tests/unit/test_tts_linux_contract.py` still skips with "Linux TTS is NOT PROVEN". A premium
female voice in production still needs Kokoro or equivalent, which remains gated (§6 of
`voice-agent-premium-upgrade-research.md`).

### 3. "Too quiet / wrong speaker" -- an incomplete communication-audio contract

The playback `AudioTrack` is built with `USAGE_VOICE_COMMUNICATION`. Two consequences follow,
and nothing in the app had accounted for either:

1. the platform prefers the **earpiece** for communication audio, and
2. the level follows `STREAM_VOICE_CALL`, not `STREAM_MUSIC` -- so the volume keys the student
   reaches for move the wrong stream.

`AndroidAudioRouteMonitor` observes the route and, by its own documented choice, never sets one:
"it does not force audio to a device, because overriding the user's headset choice is a product
decision". Correct for an observer, but it left nothing in the app able to ask for the
loudspeaker. Searched and confirmed absent before the fix: no `setMode`, no
`setCommunicationDevice`, no `setSpeakerphoneOn` anywhere in the repository.

**Not a gain problem.** Measured from the real provider output: RMS **-21.9 dBFS**, peak
**-4.6 dBFS**, not clipped. The synthesised audio is at a healthy level, so no gain
normalisation was added -- raising gain would have distorted correctly-levelled audio to
compensate for a routing fault.

**Why the usage was not simply changed to `USAGE_MEDIA`.** That is the one-line fix and it is
wrong here. The session holds the microphone and speaker simultaneously, and the communication
usage is what enlists the platform's acoustic echo canceller. On media usage the assistant's
own voice returns through the microphone, which the turn detector reads as the student
interrupting -- trading a routing bug for a conversation that talks over itself.

**Fix.** A new `AudioOutputRouter` port with `AndroidAudioOutputRouter`: enter
`MODE_IN_COMMUNICATION`, select a device (`setCommunicationDevice` on API 31+, the scoped
legacy `isSpeakerphoneOn` below that), and restore mode and routing on teardown. Entering
communication mode is also what gives the volume keys back, since the platform maps them to the
voice-call stream while it is active -- so no code touches a volume level.

An attached headset or Bluetooth device wins over the loudspeaker; the speaker is the default
only when nothing else is connected. A routing refusal does **not** fail the session: another
app owning the communication device is ordinary, and a conversation on the earpiece beats no
conversation. It surfaces as `VoiceEvent.OutputRoutingFailed` rather than being swallowed.

### What remains unverified

**No physical device was attached** (`adb devices` returned an empty list), so every hardware
claim is BLOCKED, specifically:

- that audio actually emerges from the loudspeaker;
- that it is comfortably audible at normal volume;
- that the volume keys move it;
- that wired/Bluetooth routing works and survives a mid-playback switch;
- that the mode is restored (observable only as "other apps sound normal afterwards");
- whether Zira *sounds* natural and pleasant, as opposed to measuring as female.

`AndroidAudioOutputRouter` itself has no unit test and cannot have one: `AudioManager` and
`AudioDeviceInfo` do not exist in a JVM unit test, which is the reason the port exists. The
tests cover the *session's* use of the port. A green suite is not a working loudspeaker.
