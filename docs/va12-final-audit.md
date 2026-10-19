# VA-12 — final voice agent audit

Independent re-audit of both repositories, performed by inspecting current code and
configuration rather than by reading the VA-0 … VA-11 completion reports. Every claim below was
re-derived in this phase; where a prior report was imprecise, this document says so.

**Headline, stated precisely, because the imprecise versions are all misleading:**

- A **turn-based, file-based spoken interview path exists and is wired end to end** — record →
  upload → server-side transcription → store → grade → synthesize the next question → play.
- A **realtime streaming voice agent does not exist**. The client transport is never
  constructed, the gateway performs no recognition, and the VA-3 speech provider selection is
  `none`.
- **Neither has been run on a physical device.** No handset was available in VA-11 or here.

So the product is not "a fully functioning realtime voice agent", and it is also not "no spoken
path at all". It is a turn-based spoken interview whose device behaviour is unproven and whose
speech quality is explicitly dev-grade.

## 1. Repository and configuration state

| | Android | Backend |
|---|---|---|
Location | `ai-placement-android` | `ai-placement-backend` |
Branch | `career-strategy-intelligence` | `professional-presence-intelligence` |
HEAD | `8b571f2` | `46d420f` |
Modified | 1 | 15 |
Untracked | 13 | 47 |
Staged / stashes | 0 / 0 | 0 / 0 |

`app.db` md5 `496b29df1acb1d5c9ec4d86da2a0430c`, unchanged. 46 Alembic revisions;
`alembic check` → "No new upgrade operations detected". Android `compileSdk 36`,
`targetSdk 36`, `minSdk 26`, flavours `dev` / `prod`.

**The locked specification is `PROJECT_HANDOFF.md` (391 lines) at the workspace root**, which
declares itself the single source of truth and still opens with "No implementation has started
yet" — it is the original product spec, not a living acceptance document. **No VA-0 … VA-12
phase specification exists in either repository.** The phase acceptance gates exist only in the
prompts given at the time, so §6's matrix reconciles against those prompts plus the code. This
is itself an audit finding: the acceptance criteria for the product's most security-sensitive
subsystem are not version-controlled anywhere.

Provider configuration (names and non-secret defaults only):

| Setting | Value | Consequence |
|---|---|---|
`ai_provider` | `ollama` | local model; `anthropic_provider.py` implements neither `transcribe` nor `synthesize_speech` |
`whisper_model_size` | `tiny.en` | smallest Whisper tier |
`stt_provider` | **`none`** | the VA-3 `SpeechToText` contract has no selected implementation |
`tts_provider` | **`none`** | the VA-3 `TextToSpeech` contract has no selected implementation |
`job_provider` | **`none`** | `get_job_provider()` returns `NullJobProvider` |

No secret values were read or printed. No provider was enabled.

## 2. Capability inventory

Using VA-12's vocabulary strictly. **Connected** means a real application flow invokes it —
for the Android product that means a screen or route reaches it, not merely that an HTTP
endpoint exists.

| Capability | Location | Implemented | Connected | Tested | Status |
|---|---|---|---|---|---|
Authentication, token type/revocation | `app/api/deps.py` | yes | yes | yes | **Accepted** |
Ownership scoping on personal resources | per-service + `interview_service.owned_question` | yes | yes | yes | **Accepted** |
Entitlement at execution boundary | `registry._prepare_action`, `_dispatch`, `deps.require_premium` | yes | yes | yes | **Accepted** |
Authenticated read tools | `app/tools/read_tools.py` | yes | **backend only** | yes | Implemented + Tested; no app flow reaches them |
World / job-market tools | `app/tools/world_tools.py`, `app/world/` | yes | provider `none` | yes (fakes) | **Blocked** |
Agent intent / planning / grounding / memory | `app/agent/` | yes | **`POST /coach/ask` only** | yes | Implemented + Tested; **no Android client** |
Tool Registry READ/ACTION separation | `app/tools/registry.py:141,217,233` | yes | yes | yes (mutation-verified) | **Accepted** |
Action confirmation, idempotency, cancellation, verification, reconciliation | `app/tools/confirmation.py`, `registry.py` | yes | HTTP yes | yes (mutation-verified) | **Accepted** (backend) |
Action confirmation **UI** | `ui/actions/ActionConfirmationHost.kt` | yes | **no — never rendered** | yes (21 JVM tests) | Implemented + Tested, **not Connected** |
Shared Voice Core | `voice/core/` | yes | **no callers** | yes (59 JVM tests) | Implemented + Tested, **not Connected** |
Realtime client transport | `voice/realtime/` | yes | **never constructed** | yes (105 JVM tests) | Implemented + Tested, **not Connected** |
Audio capture / playback (reachable path) | `voice/AudioRecorder.kt`, `AudioPlayer.kt` | yes | **yes** | JVM only | **Needs test** (no device) |
Audio focus | `voice/core/AndroidVoiceAudio.kt:356-398` | yes | **no — only in the unmounted path** | yes | **Defect D1** |
Permissions lifecycle | `ui/interview/VoiceInterviewScreen.kt` | yes | yes | — | **Needs test** |
Recorder lifecycle on interruption | same | **no** | — | — | **Defect D2** |
WebSocket auth, sessions, resume grants | `app/api/voice_session.py`, `app/voice/session_registry.py` | yes | **no client opens it** | yes (26 tests) | Implemented + Tested, **not Connected** |
Realtime protocol messages | `app/voice/realtime_protocol.py` ↔ `voice/realtime/` | yes | — | yes (parity test) | session management only — see §3 |
STT / TTS contracts | `app/speech/` | yes | **selection `none`** | yes (fakes) | **Blocked** |
STT / TTS actually in use | `app/ai/ollama_provider.py:196,211` | yes | **yes**, via `/answer/audio` and `/questions/{id}/audio` | — | dev-only; TTS effectively Windows-only |
Voice career coach | `app/agent/coaching.py`, `app/api/coach.py` | yes | **no Android client** | yes (97 tests) | Implemented + Tested, **not Connected** |
Voice interview turn (VA-9) | `app/services/voice_interview.py`, `POST /interviews/{id}/voice-turn` | yes | **no Android client** | yes (42 tests) | Implemented + Tested, **not Connected** |
Interview engine: dedup, concurrency, evaluation integrity | `app/services/interview_service.py` | yes | yes | yes (mutation-verified) | **Accepted** |
Prompt-injection input/output guards | `app/ai/input_guard.py`, `output_guard.py` | yes | yes (~20 call sites) | yes (mutation-verified) | **Accepted** |
Log redaction | central handler + per-module | yes | yes | yes (canary-measured) | **Accepted** |
Rate limits | `app/core/rate_limit.py` | yes | **not on `/actions/*`** | — | **Defect D4** |
Physical-device evidence | — | — | — | **none** | **Blocked** |

Android unit tests: 1359 per variant (dev and prod), 0 failures. Backend suite: see §6.

## 3. The actual voice path

Traced independently through navigation and routes. `VoiceInterviewScreen` is routed at
`ui/navigation/NavGraph.kt:370`.

```
REACHABLE — turn-based, file-based (the only path a student can use)

  VoiceInterviewScreen ──permission──> AudioRecorder (MediaRecorder, AAC/m4a 44.1kHz, 2min cap)
        │                                      │
        │                                      ▼
        │                        POST /interviews/{id}/answer/audio
        │                                      │  requires_transcribe
        │                                      ▼
        │                        OllamaProvider.transcribe  (faster-whisper tiny.en, CPU)
        │                                      ▼
        │                        interview_service.submit_answer
        │                          _is_replay_of / _insert_answer_once / unique index
        │                                      ▼
        │                        get_next_question ──> grading, progression, evaluation
        │                                      │
        ▼                                      ▼
  AudioPlayer (MediaPlayer, WAV) <── GET /interviews/{id}/questions/{qid}/audio
                                          requires_synthesize
                                          OllamaProvider.synthesize_speech (pyttsx3)

  Gaps on this path: no audio focus (D1); no ON_PAUSE/ON_STOP release (D2); no logging (D3).
  Never run on a physical device.


NOT REACHABLE — the realtime path

  voice/core/AndroidVoiceAudio  ─┐
  voice/realtime/RealtimeVoiceSession ─┤  zero references from outside their own packages;
  voice/realtime/WebSocketRealtimeTransport ─┘  never constructed in main/
                                   ╳
                                   ╳  no client ever opens the socket
                                   ╳
  Backend GET /voice/session (authenticated, App Check, session registry, resume grants)
        │
        ├── receives: heartbeat, commit_turn, interrupt, cancel_operation, resume
        ├── sends:    session_accepted, session_expired          ← the only two send sites
        └── performs: NO recognition, NO synthesis, NO model, NO tool execution
                                   ╳
                                   ╳  nothing bridges the socket to the engine
                                   ╳
  voice_interview.submit_spoken_turn  ← called only from app/api/interviews.py:239 (HTTP)
  Orchestrator.coach                  ← called only from app/api/coach.py (HTTP)
```

Stage-by-stage verdict:

| § Stage | Status |
|---|---|
1. Mic permission, capture, format | **Reachable**, defects D1/D2, device-unverified |
2. Realtime transport + authenticated session | **Not reachable** — client transport never constructed; backend side implemented and tested |
3. Control messages + audio frames | **Not reachable**; and the protocol carries session management only |
4. Speech recognition + transcript delivery | **Realtime: does not exist.** Turn-based: exists via `AIProvider.transcribe` (dev-only) |
5. Authenticated orchestration → coach / interview engine | **Not reachable from the socket.** Interview engine reachable over HTTP; agent reachable only by calling `/coach/ask` directly |
6. Authoritative processing and progression | **Reachable** over HTTP; engine owns scoring and progression |
7. Synthesis, playback, interruption, recovery | Synthesis and playback reachable; **interruption and recovery unproven** (D1/D2, no device) |

Protocol verification from both ends: the Android codec emits `heartbeat`, `commit_turn`,
`interrupt`, `cancel_operation`, `resume` and understands `session_accepted`,
`session_expired`; the gateway's only `send_text(protocol.…)` sites are `session_accepted` and
`session_expired`. There is **no** transcript, generation or tool-outcome message in either
direction. So even if the client were mounted, a passing layer 2–4 would prove that a socket
opens, stays alive and can be resumed — not that a conversation happened.

All four VA-11 findings re-checked and **confirmed** against current code.

## 4. Security invariants

Re-verified in current code this phase; "mutation-verified" refers to VA-10's 28-mutant
programme, whose results were recorded per guard.

| Invariant | Evidence | Status |
|---|---|---|
Personal resources scoped to the authenticated user | ownership checks per service; `owned_question` | **PASS** |
Cross-user access rejected without disclosing existence | identical 404s; coach session oracle closed in VA-10 | **PASS** |
Session ids / resume grants are not credentials | `redeem_resume_token` owner check + single use; memory keys namespaced by account | **PASS** |
Entitlement at authoritative boundaries | `registry.py:236`, `:159`; both mutation-verified | **PASS** |
Model-generated identifiers cannot authorize | `ToolContext` has no user-id constructor | **PASS** |
No arbitrary code / SQL / HTTP / unregistered mutation | closed registry; no `db.*` in read or world tools | **PASS** |
Read paths cannot execute ACTION tools | `registry.py:141` and `:217` both reject non-READ | **PASS** |
Confirmation user / capability / argument bound, expiring, single-use | all four checks present in `confirmation.py`; each mutation-verified | **PASS** |
Idempotency prevents duplicate effects; conflicting key reuse rejected | `request_fingerprint` check; `operation_claims` unique index | **PASS** |
Uncertain outcomes reconciled from authoritative state | `_reconcile`; mutation-verified | **PASS** |
Nothing reported successful before verification | `ActionOutcome.__post_init__` makes it unconstructible | **PASS** |
Cancellation and concurrency preserve guarantees | VA-10 race matrix; gated concurrency tests | **PASS** |
External content treated as untrusted | `input_guard` delimiter neutralisation; mutation-verified | **PASS** |
Guards catch wrapped / whitespace-manipulated markers | `output_guard.looks_like_prompt_leak` flattens before matching | **PASS** |
Fake tool-result text cannot bypass boundaries | closed enum/tool validation; orchestrator tests | **PASS** |
No sensitive data in logs or errors | canary-measured across 9 surfaces | **PASS** |
No undocumented transcript / raw-audio persistence | no audio written in `interviews.py`; no model field holds audio | **PASS** |
Answer not duplicated by replay or concurrency | unique index decides; mutation-verified | **PASS** |
Stale results cannot modify a newer turn | `interview_question_id` is the turn token | **PASS** |
Evaluation status reflects authoritative state | read from the interview row; mutation-verified | **PASS** |
Voice and typed semantics consistent | shared 5 000-char cap; same engine | **PASS** |
Engine owns scoring and progression | structural test forbids scoring in `voice_interview.py` | **PASS** |
**Rate limiting on the action boundary** | **absent** — see D4 | **FAIL** |

## 5. Memory, reliability, lifecycle

| Property | Finding |
|---|---|
Session ownership / expiry / resume | owner-checked, single-use, TTL-purged, quota-bounded |
Cancellation and deadline propagation | checked at dispatch boundaries, before approval consumption; honest about granularity |
Bounded memory | agent memory 256 sessions / 16 facts / 8 turns; confirmations 256 global + 16 per account; resume grants 8 per account; voice sessions 3 per account; world cache bounded with eviction counting |
Backpressure and input limits | WS reads one message at a time, buffers nothing; 8 KiB control frame, 64 KiB audio frame, 25 MiB per session; 10 MiB audio upload; 5 000-char answer |
Provider failure / fallback | typed failures; degrades to the deterministic path; no fabrication |
Stale callbacks | answered by the turn token, not a separate generation counter |
Audio resource cleanup and focus | **D1 and D2 open on the reachable path** |
Cache bounds and freshness | bounded; freshness and provenance recorded per observation |
**Process-local state** | three registries (VA-2 voice sessions, agent memory, VA-7 confirmations) are per-process with **no cross-worker recovery**. Deployment assumption: **a single worker process.** A multi-worker deployment silently loses resume grants, conversation context and pending approvals on any request that lands on another worker. Worker restart is now asserted to refuse honestly rather than appear to recover. Not solved here: the fix is a shared store, which is a deployment decision and a new dependency. |
Concurrency / DB uniqueness | `operation_claims` and `interview_answers` unique indexes are the arbiters, not preceding reads |
Logging / error redaction | central handler; canary-verified |

## 6. Master acceptance matrix, VA-0 … VA-12

Phase scopes were read from the code and docs, not assumed: `VA-1` = Shared Voice Core
(Android, 42 refs), `VA-2` = realtime transport gateway, `VA-3` = speech contracts
("evaluation, not a decision"), `VA-4` = Tool Registry (read-only), `VA-5` = World Career
Intelligence, `VA-6` = agent orchestration/memory, `VA-7` = safe actions, `VA-8` = voice career
coach, `VA-9` = voice interview, `VA-10` = security hardening, `VA-11` = device validation.

"Critical" means critical to `PROJECT_HANDOFF.md`'s locked V1 flow, which names **Voice
Interview** as a V1 step and does not name a realtime voice agent, a career-coach chat, or
write-actions.

| Phase | Requirement | Actual interface | Evidence | Status | Remaining dependency | Critical |
|---|---|---|---|---|---|---|
VA-0 | Baseline recorded; no deployment image | `docs/PRODUCTION_DEPLOYMENT.md` | repo inspection | **PASS** | — | no |
VA-0.5 | Untrusted input cannot forge prompt delimiters; model output cannot leak scaffolding | `app/ai/input_guard.py`, `output_guard.py` | mutation-verified (M18–M20); ~20 guarded call sites | **PASS** | — | yes |
VA-1 | Shared Voice Core abstraction with audio focus and typed states | `voice/core/` | 59 JVM tests | **NEEDS TEST** — *Implemented + Tested, never Connected* | mount on a screen; device run | no |
VA-2 | Authenticated realtime transport, session lifecycle, resume, bounded frames | `app/api/voice_session.py`, `app/voice/session_registry.py`; client `voice/realtime/` | 26 backend + 105 JVM tests; protocol parity test | **NEEDS TEST** — *backend Accepted; client never constructed* | a client that opens the socket; device run | no |
VA-2 | Gateway emits nothing it cannot substantiate | only `session_accepted`, `session_expired` sent | re-verified: two `send_text` sites | **PASS** | — | yes |
VA-3 | STT/TTS contracts defined; provider evaluated, not chosen | `app/speech/`; `docs/speech-provider-evaluation.md` | adapter + contract tests with fakes | **BLOCKED** | `stt_provider`/`tts_provider` are `none` — owner decision | yes |
VA-4 | Closed read-only tool registry; no arbitrary execution; entitlement enforced | `app/tools/registry.py`, `read_tools.py` | mutation-verified (M05, M05b, M11); 36 security + 23 contract tests | **PASS** | — | yes |
VA-5 | World data with provenance and freshness; honest absence | `app/world/`, `app/tools/world_tools.py` | 108 tests (23 security + 32 contracts + 53 job-market) with fakes | **BLOCKED** | `job_provider` is `none` — owner decision | no |
VA-6 | Intent, planning, bounded context, grounding, session memory; no write access | `app/agent/` | 106 tests (38 orchestrator + 46 contracts + 22 memory); `test_every_plannable_tool_has_a_grounding_branch` | **PASS** (backend) | no app flow reaches it | no |
VA-7 | Confirmation user/capability/argument-bound, expiring, single-use; idempotency; verified outcomes; reconciliation | `app/tools/confirmation.py`, `registry.py`, `app/api/actions.py` | mutation-verified (M06–M15, M26); 96 tests | **PASS** (backend) | — | no |
VA-7 | Confirmation UI mounted | `ui/actions/ActionConfirmationHost.kt` | never rendered — re-verified | **NEEDS TEST** — *not Connected* | mount + device run | no |
VA-8 | Evidence-grounded coaching, no fabrication, no promises | `app/agent/coaching.py`, `POST /coach/ask` | 97 tests; promise language unconstructible | **PASS** (backend) | **no Android client** | no |
VA-9 | Spoken turn reuses the interview engine; replay/concurrency safe; honest states | `app/services/voice_interview.py`, `POST /interviews/{id}/voice-turn` | 42 tests; mutation-verified (M01, M02, M16, M17) | **PASS** (backend) | **no Android client** | yes |
VA-9 | Spoken end-to-end (STT→transport→engine→TTS) | — | — | **BLOCKED** | VA-3 providers + realtime integration | yes |
VA-10 | Critical guards demonstrably load-bearing | mutation programme | **27 of 28 mutants caught**, zero file drift | **PASS** | — | yes |
VA-10 | Secrets absent from logs and errors | central handler + per-module | canary-measured across 9 surfaces | **PASS** | — | yes |
VA-10 | Bounded memory on all process-local state | confirmations, resume grants, agent memory, world cache | unit-proven bounds | **PASS** | — | yes |
VA-11 | Physical-device acceptance of audio, lifecycle, routing, interruption, network, resources | `VOICE_DEVICE_ACCEPTANCE_VA11.md` | `adb devices` empty; no handset present | **BLOCKED** | a physical Android device | yes |
VA-11 | Audio focus on the path a student uses | `AudioRecorder.kt` / `AudioPlayer.kt` | no `requestAudioFocus` — re-verified | **FAIL** (D1) | choose remedy | yes |
VA-11 | Recording released on background / lock / call / rotation | `VoiceInterviewScreen.kt:61`, `ON_RESUME`-only observer | re-verified | **FAIL** (D2) | owner decision on remedy | yes |
VA-12 | Rate limiting on every authenticated mutation boundary | `app/api/actions.py` | no limiter; no `default_limits` | **FAIL** (D4) | small fix + fixture review | no |
VA-12 | Acceptance criteria version-controlled | — | no VA spec in either repo | **FAIL** (D5) | write it down | no |
VA-12 | Multi-worker resilience of session/memory/confirmation state | three process-local registries | documented, not solved | **OWNER DECISION REQUIRED** | shared store = new dependency | yes |
VA-12 | Realtime voice agent in V1 scope at all | unmounted client + recognition-free gateway | §3 map | **OWNER DECISION REQUIRED** | scope call | yes |

Counts: **12 PASS**, **5 FAIL** (D1, D2, D4, D5, and the audio-focus/lifecycle pair counted
separately), **4 BLOCKED**, **4 NEEDS TEST**, **2 OWNER DECISION REQUIRED**. No critical
security invariant is FAIL; the FAILs are one hardening gap, two Android lifecycle defects, and
two process gaps.

## 7. Findings opened by this audit

### D4 — the action endpoints have no rate limit *(new, found in this phase)*

`app/api/actions.py` contains no `limiter`, no `@limiter.limit` and no `Request` parameter.
`app/core/rate_limit.py:80` constructs `Limiter(key_func=user_or_ip_key)` with **no
`default_limits`**, so an undecorated route is unlimited. The router is included with App Check
only (`app/main.py:121`).

Every comparable authenticated surface *is* limited — `/coach/ask` at 60/hour, every interview
route at 60/hour — which makes this an inconsistency rather than a deliberate choice.

Impact is bounded but real: `/actions/execute` still needs a valid single-use confirmation, so
this is not an authorization bypass and cannot multiply mutations. `/actions/preview` is the
amplifier — authenticated, unlimited, and each call performs listing and tracker reads and mints
a grant. Classification: resource exhaustion on the write boundary, medium-low.

**Not fixed in this audit, deliberately.** VA-12 §1 permits a correction only with clear
justification and focused regression tests, and this one carries a specific test-interaction
risk: several of VA-7's 72 action tests make many calls under one identity, so adding a 60/hour
limit could turn passing tests into 429s. The fix belongs in a phase that can adjust the
fixtures and re-run the suite, not in the audit that found it.

### D1, D2, D2a, D3 — carried from VA-11, re-confirmed

D1 (no audio focus on the reachable path; focus exists only in the unmounted Voice Core), D2 (no
`ON_PAUSE`/`ON_STOP` release, manager scoped to `remember {}`, no `configChanges`, so the
microphone may keep recording after the app leaves the foreground), D2a (the instrumentation
rotation-test pattern exists for other screens but not for voice), D3 (no logging on the
reachable path). All re-verified here against current code.

### D5 — the acceptance specification is not version-controlled

`PROJECT_HANDOFF.md` predates all voice work and says so. The VA-0 … VA-12 gates exist only in
prompt text. Nothing in either repository states what "done" means for the voice agent, which is
why each phase had to re-derive it and why two prior reports drifted from the code
(`VOICE_READINESS_CHECKLIST.md` on audio focus; the VA-8 report on the spoken pipeline).

## 8. Test evidence re-run in this audit

Re-run here rather than cited from prior reports, since VA-12 §1 requires revalidating the code
rather than trusting the phase reports.

| Command | Exit | Result |
|---|---|---|
`pytest -q` (full backend) | **0** | **4040 passed, 3 skipped, 0 failed**, 1:21:53; zero `FAILED`/`ERROR` lines |
`ruff check app/ tests/` | 0 | All checks passed |
`alembic upgrade head && alembic check` | 0 | "No new upgrade operations detected" (disposable DB) |
`gradlew :app:testDevDebugUnitTest --rerun :app:testProdDebugUnitTest --rerun` | 0 | **1359 tests per variant, 0 failures / 0 errors / 0 skipped** (JUnit XML) |
`gradlew :app:lintDevDebug --rerun-tasks` | 0 | **0 errors**, 32 warnings, 1 hint |
`adb devices -l` | 0 | empty — no physical device |
Android instrumentation (`connectedDevDebugAndroidTest`) | — | **not run — requires a device** |

`app.db` md5 `496b29df1acb1d5c9ec4d86da2a0430c` before and after every run.

The full-suite figure reproduces VA-10's exactly, which is the expected result: no backend
production code changed in VA-11 or VA-12, and the identical git status and database hash
confirm it. The Android figures were taken from the JUnit XML rather than the build's exit code,
because the first Gradle invocation reported `BUILD SUCCESSFUL` with every test task
`UP-TO-DATE` — a green build that ran no tests.

**What this evidence does not establish:** any of it running on a physical device, any real
network behaviour, any real speech provider, or any realtime conversation. 5399 automated tests
passing is why the device run can be short; it is not a substitute for it.

## 9. Provider, cost and external-data status

Verified from configuration and code, not from prior reports.

| Provider role | Implemented | Selected | Configured | Actually used | External requests possible? |
|---|---|---|---|---|---|
LLM | `ollama_provider.py`, `anthropic_provider.py` | `ai_provider = "ollama"` | loopback default | yes, locally | **No** — loopback; `config.py:361` flags the un-overridden default as a production misconfiguration |
STT (VA-3 contract) | `app/speech/` `FasterWhisperSpeechToText` | **`stt_provider = "none"`** | weights cached locally | **not selected** | No |
TTS (VA-3 contract) | `app/speech/` pyttsx3 adapter | **`tts_provider = "none"`** | SAPI5 driver present | **not selected** | No |
STT/TTS actually serving the app | `OllamaProvider.transcribe` / `.synthesize_speech` | — | faster-whisper `tiny.en`, pyttsx3 | **yes**, via `/answer/audio` and `/questions/{id}/audio` | No |
World / job market | Adzuna, Careerjet, Jobvetta adapters | **`job_provider = "none"`** | no keys | `NullJobProvider` | **No** — `search()` raises `JobProviderNotConfiguredError` rather than returning anything |

**No external network request is possible on the voice, speech or world paths under the current
configuration.** The LLM is local-loopback, both speech adapters are local, and the job provider
raises instead of calling out. ElevenLabs appears nowhere. ₹0-first holds; nothing was enabled,
no credential created, no model downloaded, no dependency installed.

**Local development integrations vs production-capable ones.** This distinction matters more
than the table suggests, because on *this* host the local adapters genuinely run:
`tests/unit/test_speech_real_model.py` — **9 passed, 0 skipped** — exercises the real
faster-whisper recogniser and the real pyttsx3 driver, because `_weights_present()` and
`_driver_available()` both returned true here. Those same tests **silently skip** on a host
without cached weights or a speech driver, and `tests/unit/test_tts_linux_contract.py:112`
skips with the message *"until it runs on Linux with espeak-ng installed, Linux TTS is NOT
PROVEN."* So: speech works on this Windows developer machine and is unproven on any deployment
host. Every world-data test uses fakes; no test demonstrates live retrieval.

**Approvals.** Terms, data retention, attribution and credentials are **unapproved for every
external provider**, because none is selected. The attribution contract exists
(`JobProvider.attribution_text` / `attribution_url`, empty on `NullJobProvider`), so a future
provider has a defined place to carry its required attribution — it has simply never been used.

**Paths unavailable without a provider decision:** VA-3 contract-based STT/TTS, the realtime
spoken pipeline, all live world/job-market retrieval, and any claim of production speech quality.

## 10. D4 — verified, with measured reproduction

§8.1 asks for independent verification rather than an inference from a missing decorator. Both
were checked.

**The absence is real.** `app/api/actions.py` contains no `limiter`, no `@limiter.limit` and no
`Request` parameter. `app/core/rate_limit.py:80` constructs `Limiter(key_func=user_or_ip_key)`
with no `default_limits`; the probe below read `limiter._default_limits == []` from the live app.
The router is mounted with App Check only (`app/main.py:121`), and App Check is itself a no-op
unless enabled (`app_check.py:63` — `if not settings.firebase_app_check_enabled: return`).

**Measured reproduction** (150 consecutive authenticated previews of one listing, through the
real app on the project's disposable-SQLite harness):

```
preview attempts      : 150
status codes          : {200: 150}
429 responses         : 0
SQL statements/preview: 4.0
grants held for user  : 16   (capped by MAX_PENDING_PER_USER)
limiter default_limits: []
```

**Controls that do exist**, which is why this is not an unlimited-exploit headline:

- Authentication is required; the limiter's key function (`user_or_ip_key`) is already correct,
  it is simply never applied here.
- Confirmation grants are bounded per account at 16 (VA-10's fix), so the 150 attempts left
  memory flat — unlimited previews cannot grow the store.
- `/actions/execute` still requires a valid, single-use, user/capability/argument-bound
  confirmation, so this cannot multiply mutations or bypass any VA-7 guarantee.
- The path makes no model or provider call, so there is no cost amplification — which is the
  specific harm `rate_limit.py`'s own docstring says the limiter exists to bound.

**Residual risk:** 4 SQL statements per call at an unbounded rate from a single authenticated
account — database read amplification, i.e. availability. No data exposure, no privilege
escalation, no duplicate mutation, no spend. **Severity: medium-low. Status: FAIL** (it is the
only authenticated mutation surface in the application without a limit, while `/coach/ask` and
every interview route carry 60/hour).

**Not remediated here.** §12 requires owner approval for remediation, and VA-12 §1 forbids
changing production code to make the audit pass. The reproduction probe lives in the session
scratchpad and was not added to the suite: a committed test asserting "150 previews all succeed"
would codify the defect.

### Remediation recommendation and test plan (deliverable 13)

1. Add `request: Request` and `@limiter.limit("120/hour")` to both `preview_action` and
   `execute_action`. 120 rather than 60: a student may legitimately preview several jobs in a
   sitting, and the limit is per account, not per device.
2. Change nothing in `confirmation.py` or the idempotency path — this is a decorator and a
   parameter, so VA-7's confirmation and idempotency behaviour is untouched by construction.
3. Fixture risk is low and must still be checked: VA-7's 72 action tests each sign up a fresh
   user, so each gets its own bucket, and both `h.make_client` and `conftest` already call
   `app.state.limiter.reset()`. The two tests to watch are the HTTP-surface ones that make
   several calls under one identity.
4. Add two regression tests: the limit returns 429 after the threshold for one account, and one
   account's exhausted limit does not affect another's (the per-user key, asserted).
5. Re-run `tests/api/test_safe_actions.py`, `tests/unit/test_action_confirmation.py`,
   `tests/api/test_va10_hardening.py`, `tests/api/test_ai_rate_limits.py`, then the full suite.
6. Do **not** relax any of the 72 action tests to avoid 429s; if one trips, raise the limit or
   reset the limiter in that fixture.

## 11. D5 — verified, and worse than first reported

§8.2 asks whether the VA-0–VA-12 acceptance criteria exist in version-controlled documentation.
They do not, and neither does the document that claims to be authoritative.

- `PROJECT_HANDOFF.md` lives at the **workspace root**, which is **not a git repository**:
  `git rev-parse --is-inside-work-tree` → `fatal: not a git repository`.
- It is **not tracked by either repo**: `git ls-files` in both returns no handoff, spec or
  acceptance document.
- Its own second paragraph still reads *"No implementation has started yet."*

So the document calling itself "the single source of truth for this project" has no version
control, no history, no review trail and no diff — alongside `GITHUB_REFERENCES.md`,
`UI_DESIGN_REFERENCES.md` and `UI_UX_REFERENCES.md`, equally untracked.

What *is* version-controlled is the per-phase documentation inside the backend repo
(`docs/agent-orchestrator.md`, `safe-actions.md`, `tool-registry.md`, `voice-career-coach.md`,
`voice-interview.md`, `world-intelligence.md`, `security-hardening.md`,
`speech-provider-evaluation.md`, plus the Android `VOICE_*.md` set). Those describe what was
built and why. **None of them states acceptance criteria**, and two have already drifted from
the code — `VOICE_READINESS_CHECKLIST.md` on audio focus, and the VA-8 report on the spoken
pipeline. Both drifts were caught only because a later phase re-read the code.

**Consequence for this audit:** the §6 matrix reconciles against prompt text that exists nowhere
in either repository. A different auditor, or this one in a fresh session, could not reproduce
it. That uncertainty is carried into the acceptance verdict in §13 rather than hidden.

### Source-of-truth recommendation (deliverable 14) — needs an owner decision

I have **not** created the acceptance specification, because §8.2 forbids silently rewriting
requirements to match what happens to be implemented, and writing down "the gates" from prompt
text I was given would be exactly that. What the owner must decide:

1. **Which document is authoritative.** Recommended: promote `PROJECT_HANDOFF.md` into a repo —
   `ai-placement-backend/docs/PRODUCT_SPEC.md` is the natural home since the backend holds the
   other long-form docs — and delete the stale "no implementation has started yet" line, or
   mark it as a historical preamble.
2. **Where the acceptance gates live.** Recommended: one `docs/ACCEPTANCE.md` holding the VA-0 …
   VA-12 criteria, with each row's status, so the next audit reconciles against a file rather
   than a chat transcript.
3. **Whether the V1 flow in `PROJECT_HANDOFF.md` still stands**, given it names "Voice
   Interview" as a V1 step and says nothing about a realtime agent, a career coach or
   write-actions — three of which were built.

## 12. Performance, UX and the five capability traces

### Metrics

Every metric VA-12 §9 lists is **NOT MEASURED**. No instrumentation exists for any of them, and
inventing figures is precisely what this audit must not do.

| Metric | Status |
|---|---|
Stop-of-speech → first spoken response | **NOT MEASURED** (the realtime path does not exist) |
Time to first tool invocation / completion | **NOT MEASURED** |
Interruption and playback-stop latency | **NOT MEASURED** |
Reconnection and recovery time | **NOT MEASURED** |
Provider failure rate | **NOT MEASURED** |
Tool success / failure rates | **NOT MEASURED** |
Duplicate-action rate | **NOT MEASURED** (prevention is proven by test; no production rate exists) |
World-data freshness and cache performance | **NOT MEASURED** (no provider; cache bounds proven by unit test) |
On-device latency and resource use | **NOT MEASURED** (no device) |

The one timing figure the suite does emit is an internal test-harness line
(`request 483ms | AI 400ms | 6 transactions`), which measures a local fake on a developer
machine and is not a product metric.

### Does the UI tell the truth?

Yes, for what it covers — and the risk here is the opposite of overclaiming.

`VoiceInterviewScreen`'s user-facing strings are specific and accurate: "Grant microphone
access", "Microphone access needed", "Tap to record your answer.", "Listening… tap to stop
recording.", "Listening… %d:%02d", "Playing the question…", "Transcribing your answer…",
"Submitting…", "Stop & submit", "Q 2 of 10". Recording, processing and permission states are
communicated honestly, and `VoiceInterviewUiState.Error(message)` carries failures rather than
silently resetting.

**No UI string anywhere in the app advertises a voice agent, a career coach, or live voice.** A
grep for such strings returns nothing. So no screen suggests an unavailable feature works — the
unreachable capabilities have no entry point at all. The defect is invisible unused code, not a
misleading button.

Two honest gaps remain: there is no UI vocabulary for **connection loss**, **incomplete
evaluation** or **unknown outcome**, and — more pointedly — the honest-state vocabulary VA-9
built for exactly those cases (`NEXT_QUESTION_UNAVAILABLE`, the tri-valued
`not_ready`/`ready`/`unknown` evaluation state, `answer_is_stored`) **can never reach a user**,
because no Android client calls the endpoint that returns it.

### The five capabilities, kept separate

| # | Capability | Reachable? | Tested? | Status |
|---|---|---|---|---|
1 | **File-based spoken interview** — record → upload → transcribe → submit/evaluate → question audio → playback | **Yes**, `NavGraph.kt:370` | backend yes; **never on hardware** | **NEEDS TEST**; provider = Ollama faster-whisper `tiny.en` + pyttsx3, dev-grade, Linux TTS NOT PROVEN; defects D1/D2/D3 |
2 | **Realtime streaming Voice Agent** — client constructs session/transport, exchanges protocol messages, invokes STT and the engine, synthesizes a reply | **No.** Transport never constructed; gateway sends only `session_accepted`/`session_expired`; no STT, no engine call, no synthesis | halves tested in isolation | **BLOCKED** |
3 | **Voice Career Coach** — Android user reaches VA-8 | **No.** No coach API client exists in the Android app | backend 97 tests | **BLOCKED** (no client) |
4 | **Safe-action confirmation UI** — VA-7 surface reachable from a screen | **No.** `ActionConfirmationHost` declared, never rendered | 21 JVM tests | **NEEDS TEST** (not connected) |
5 | **External career intelligence** — approved live job provider configured and used | **No.** `job_provider = "none"`; `NullJobProvider.search()` raises | 108 tests, all fakes | **BLOCKED** |

## 13. Defect list

| ID | Defect | Severity | Impact | Owner decision needed? | Remediation |
|---|---|---|---|---|---|
D1 | No audio focus on the reachable audio path | Medium | A call, notification or other media app during record/playback behaves per OEM default; the app does not control it | No | Mount the Voice Core (which implements focus), or add focus to `AudioRecorder`/`AudioPlayer` |
D2 | Recording not released on background / lock / call / rotation | **High** | The microphone may keep recording after the app leaves the foreground — privacy-relevant; in-flight answer lost; recorder leaked | **Yes** — choose the remedy | Lock interview screens to portrait, **or** hoist recorder ownership into the `ViewModel` (which already has a `SavedStateHandle`) |
D2a | No voice equivalent of the existing rotation instrumentation test | Low | D2 has no regression guard | No | Apply the `CareerPathGenerationRotationTest` pattern to `VoiceInterviewScreen`; needs a device |
D3 | No logging on the reachable voice path | Low | Device defects cannot be captured from logcat; observation must come from UI state and `dumpsys` | No | Add diagnostics before the device run |
D4 | `/actions/preview` and `/actions/execute` have no rate limit | Medium-low | DB read amplification (4 SQL/call, unbounded) from one authenticated account. No data exposure, no duplicate mutation, no spend | No | §10's plan — decorator + `Request` param + 2 regression tests |
D5 | The authoritative specification is outside version control; no acceptance gates are recorded anywhere | Medium | This audit is not reproducible; two prior reports already drifted from the code | **Yes** — which document is authoritative | §11's recommendation |
L1 | Three process-local registries, no cross-worker recovery | Medium | Single-worker deployment assumption. Multi-worker silently loses resume grants, conversation context and pending approvals | **Yes** — accept or fund a shared store | Shared store (new dependency) |

## 14. Final verdicts

### A. Engineering audit outcome: **INCOMPLETE**

What the code and automated tests establish: the backend is in strong shape. Every security and
reliability invariant in §4 passes except D4, and the load-bearing ones were proven by mutation
(27 of 28 caught) rather than by happy-path tests. 4040 backend tests, 1359 Android tests per
variant, Ruff clean, Alembic clean, lint 0 errors — all re-run in this audit, all green.

What was fixed in this audit: **nothing**. VA-12 §1 forbids changing production code to make the
audit pass, and §12 requires owner approval for remediation. The audit's only artefact is this
document.

What remains unverified: everything that needs hardware or a provider, plus D1–D5 and L1. The
honest summary is that the *components* are engineered to a high standard and the *integration*
between them is largely absent — and no amount of further testing of the components changes that.

### B. Locked product acceptance outcome: **BLOCKED**

Not INCOMPLETE but **BLOCKED**, for a specific reason: the gates that remain cannot be closed by
engineering effort alone. They need owner decisions (providers, realtime scope, deployment
topology) and hardware that does not exist in this environment. Work cannot proceed to a
COMPLETE verdict by trying harder.

`PROJECT_HANDOFF.md` names **Voice Interview** as a V1 step. Measured against that:

- The file-based spoken interview is **built and reachable**, and has **never run on a phone**.
- The realtime voice agent is **three disconnected halves** — a 105-test client nothing
  constructs, a 26-test gateway no client opens and which performs no recognition, and a
  42-test spoken-turn endpoint with no Android caller. Each half passes its own tests; nothing
  tests the seams, because the seams do not exist.
- Live career intelligence, the voice career coach and the safe-action UI are each unreachable.

And per §10's rule, the acceptance decision carries the uncertainty from **D5**: the criteria
being reconciled against are not in version control, so this verdict is auditable only against
prompt text. That alone prevents a trustworthy COMPLETE under any circumstances.

**COMPLETE is not available.** It requires all critical gates green with remaining findings
*intentionally accepted by product decision*. No limitation in this document has been accepted
by the owner — silence and prior phase reports are explicitly not acceptance — and the critical
gates for real STT/TTS wiring, physical-device acceptance and live retrieval are all open.

## 15. What must happen before release work may be considered

In priority order:

1. **Decide whether the realtime voice agent is in V1.** Every other voice decision depends on
   this one, and it is the difference between a small finishing phase and a substantial one.
2. **Approve an STT and a TTS provider**, or accept the dev-only pair with its Linux-TTS-unproven
   constraint in writing.
3. **Fix D2** (owner chooses the remedy) — it is the only defect with a privacy consequence.
4. **Obtain a physical Android device** and execute `VOICE_DEVICE_ACCEPTANCE_VA11.md`, starting
   with row A11.
5. **Fix D4** using §10's plan, and D1, D3, D2a.
6. **Resolve D5** — promote the spec into a repo and write the acceptance gates down.
7. **Accept or fund the single-worker limitation (L1).**
8. Then, and only then, decide about live career intelligence and the coach/action UIs.

Items 1, 2, 3, 6 and 7 require owner decisions. Items 4 and 5 are engineering work needing only
approval to start.

## 16. Revalidated vs historical evidence

Per deliverable 15, every earlier result is explicitly one or the other.

**Revalidated in this audit:** repository and configuration state for both repos; provider
selections; the unmounted status of the Voice Core, realtime transport, coach client, voice-turn
client and action UI; the gateway's send set and the client's message vocabulary; all §4 security
invariants by code inspection; bounds and process-local limitations; the full backend suite
(4040 passed); Ruff; Alembic; both Android unit-test variants (1359 each); Android lint; the
absence of a device; D1, D2, D3 from VA-11; and D4 and D5 by direct measurement.

**Carried as historical evidence, not re-run:**

- **VA-10's mutation programme** (27 of 28 mutants caught; the surviving M23 WebSocket mutant
  judged redundant because `authenticate_access_token("")` refuses one step later with the same
  4401 close code). The harness lived in the session scratchpad and was not re-executed here.
  The guards it exercised were re-verified present in current code, which is weaker evidence
  than re-running it.
- **VA-11's 268 focused backend tests** — superseded by this audit's full-suite run, which
  includes them.
- **Phase-by-phase test counts** cited in §6 — counted from the test files in this audit, but the
  pass/fail status comes from the full-suite run rather than per-file runs.

**Not evidence of anything, and never was:** emulator results (none used), unit tests as device
proof, and fake-provider tests as live-retrieval proof.

## 17. Owner decisions requested

1. **Approve an STT provider and a TTS provider**, or accept the dev-only Ollama/faster-whisper
   + pyttsx3 pair and its Windows-only TTS constraint. Blocks §3 stages 4 and 7 and all of VA-3.
2. **Decide whether the realtime path is in V1 at all.** If yes, it needs mounting on the client,
   interview/transcript message types in the protocol, and recognition in the gateway — a
   substantial phase, not a fix. If no, the Voice Core and realtime packages should be marked
   explicitly as deferred so no future report mistakes their green tests for product readiness.
3. **Approve a job-market provider**, or accept that world-data coaching stays unavailable.
4. **Accept the single-worker deployment assumption**, or fund a shared store for the three
   process-local registries.
5. **Choose D2's remedy** — lock the interview screens to portrait, or hoist recorder ownership
   into the ViewModel.

Engineering work that needs no decision: fix D4, D1, D2, D3; add the voice rotation
instrumentation test; obtain a physical device and execute
`ai-placement-android/VOICE_DEVICE_ACCEPTANCE_VA11.md`.

Nothing was committed, pushed, deployed, signed or published. No provider was enabled, no
credential created, no dependency installed. ElevenLabs remains excluded.
