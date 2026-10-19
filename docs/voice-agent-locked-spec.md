# Voice Agent — locked specification, VA-0 … VA-12

**Status of this document:** version-controlled record of the VA-0 … VA-12 roadmap, created to
close finding **D5**. It is the companion to [`voice-agent-acceptance-matrix.md`](voice-agent-acceptance-matrix.md),
which carries the per-requirement status. This file says *what was required*; the matrix says
*where each requirement stands*.

---

## 0. Provenance — corrected 2026-10-14

D5 was opened in [`va12-final-audit.md`](va12-final-audit.md) §11: the acceptance criteria
existed only in prompt text, so a later auditor could not reproduce the reconciliation.

### Owner decision (given, not requested)

The document hierarchy is settled and is not reopened here:

| Role | Document |
|---|---|
| **Normative** roadmap source, VA-0 … VA-12 | *AI Placement OS — Voice Agent Locked Spec and Development Plan* |
| **Supporting** reference | *AI Placement OS — Voice Agent Final Master Specification* |

### Correction to this document's first revision

The first revision of this file stated that the normative documents "are not present in either
repository" and concluded that no row could cite a section. The first half was true and the
conclusion was wrong: **all three documents are on this machine, and they have now been read.**
They live outside both repositories, which is why `git ls-files` found nothing and why the
earlier check — which looked only in the repos — reported them missing.

Located, inspected, hashed, and — as of 2026-10-14 — **copied into this repository** so that
every citation below resolves to a path a reader already has. Each copy's SHA-256 was recomputed
after copying and matches its original byte for byte; the originals were left in place.

| Document | Repository path | SHA-256 | Pages |
|---|---|---|---|
| Locked Spec and Development Plan (**LS**) | [`docs/source/01-locked-spec-and-development-plan.NORMATIVE.pdf`](source/01-locked-spec-and-development-plan.NORMATIVE.pdf) | `d96f0711aaad8f90b491cbbe9d913a7d66cd7714d39080456d5148a0c9d5bd29` | 24 |
| Final Master Specification (**FMS**) | [`docs/source/02-final-master-specification.SUPPORTING.pdf`](source/02-final-master-specification.SUPPORTING.pdf) | `cdb9d609a61be7c2cc2188c1050ba64abc470c10543c6e3fb0632229353f19d5` | 24 |
| Master Specification — **SUPERSEDED**, do not cite as a requirement | [`docs/source/03-master-specification.SUPERSEDED.pdf`](source/03-master-specification.SUPERSEDED.pdf) | `b4af3f3bd44d33a0851f87c5faca9cf0b121240528695f9a63dfbad1838aa591` | 18 |

Originals, unmoved and unmodified: `~/OneDrive/Desktop/Downloads/AI_Placement_OS_Voice_Agent_*.pdf`.
See [`docs/source/README.md`](source/README.md) for the hierarchy and re-verification command.

They are text-based PDFs whose pages are CID-encoded under subset fonts, so the text had to be
decoded through each font's embedded `ToUnicode` CMap. No dependency was installed for this and
none is required; the extraction is stdlib `zlib` plus the CMaps, and it resolved every glyph in
all three files (zero unmapped characters), so the quotations below are the documents' own words
rather than a lossy rendering. Page numbers were then established by walking the `/Pages`
`/Kids` tree for true page order, so a citation like `LS §12 (p.17)` names the page a viewer's
counter shows. Verified section-to-page anchors in LS:

| Section | Page |
|---|---|
| §10, stages VA-0 … VA-3 | 15 |
| §11, stages VA-4 … VA-7 | 16 |
| §12, stages VA-8 … VA-12 | 17 |
| §13.2 Critical invariants, §13.3 Performance metrics | 18 |
| §14 Security and privacy hardening | 19 |
| §15.3 Production provider decision gate | 20 |
| §16.2 Change discipline | 21 |
| §17 Final acceptance checklist | 22 |

### What is still unclosed

The copies now in `docs/source/` are **untracked working-tree files, not version history.**
`git status` lists them as `??`; there is no commit, no diff and no review trail, and committing
them is the owner's call under this increment's stop condition. So D5 is narrowed, not closed:
citations are reproducible from the repository, while the documents themselves still have no
history. Nothing here converts that into an owner decision — it is a statement of the current
repository state.

### Provenance codes used below

| Code | Meaning |
|---|---|
| `LS §n` | Locked Spec and Development Plan, section *n*, read directly from the PDF above. |
| `FMS` | Final Master Specification, supporting reference. |
| `OWNER DECISION` | A decision the owner has given in session. Labelled as such, never dressed as a document citation. |
| `AUTHORIZATION` | Stated in an increment authorization in session, and **not** found in LS or FMS. Reproducible only from the session transcript. |
| `CODE` | Read out of the implementation and its tests. |
| `DOC:<file>§<n>` | A version-controlled document in one of the two repos. |
| `DERIVED` | Inferred by an audit from code plus docs, and labelled as inference there. |

No row invents a page number. Where a requirement's only provenance is `AUTHORIZATION`, that is
recorded plainly.

### The source's own rules, quoted

Three rules from LS govern how every row below must be read:

> **LS §16 (p.21).2 (p.21), Change discipline.** "All existing dirty work is presumed intentional. Never
> destroy or hide it to make the stage easier. No cleanup refactor is allowed just because a file
> looks messy."

> **LS §17 (p.22), Final acceptance checklist.** "A green demo is not the acceptance criterion. The
> Voice Agent is complete only when behavior, security, reliability, freshness, lifecycle, device
> and regression evidence are all present."

> **LS §10 (p.15), VA-0 gate.** "Do not start implementation from memory or from this document alone.
> VA-0 must first inspect the actual current code."


---

## 1. What the Voice Agent is

Two conversational modes over one authenticated WebSocket, reaching existing backend engines:

```
Android                                     FastAPI
───────                                     ───────
VoiceAgentScreen
  └─ NetworkModule.createRealtimeVoiceSession(scope, mode, interview)
       └─ WebSocketRealtimeTransport ──── GET /voice/session (Bearer, query params)
            RealtimeVoiceSession              authenticate → App Check → VoiceMode
              AudioRecordPcmCapture           → responder built BEFORE accept
                → VAD → TurnDetector          → TurnChannel.commit()
                → commit_turn                 → TurnRuntime.run()
                                                 to_thread(STT) → final_transcript
              AudioTrackPcmPlayback ◀───        → respond()  → to_thread(TTS)
                                                 Orchestrator.coach()         (coach)
                                                 voice_interview.submit_spoken_turn (interview)
```

Scope boundaries that are themselves requirements:

- **Read-only on this path.** The voice tool catalogue is `build_default_registry()` with no
  ACTION tools. Writes happen only through the separate VA-7 confirmation endpoints, driven by
  an explicit tap.
- **No second engine.** Interview grading, progression, persistence and ownership stay in
  `app/services/interview_service.py` and `voice_interview.py`. The responder is a caller.
- **Development-only speech.** No production STT/TTS provider is approved. ElevenLabs excluded.

---

## 2. VA-0 — baseline and prompt-injection containment

| Requirement | Provenance |
|---|---|
| Repository/configuration baseline recorded; no deployment image claimed | `DOC:va12-final-audit.md§6` |
| Untrusted input cannot forge prompt delimiters | `CODE: app/ai/input_guard.py` |
| Model output cannot leak scaffolding | `CODE: app/ai/output_guard.py` |

## 3. VA-1 — shared Voice Core (Android)

| Requirement | Provenance |
|---|---|
| One audio abstraction behind ports: permission, focus, capture, playback, routes | `DOC:VOICE_CORE_ARCHITECTURE.md` |
| Typed session states; no boolean soup | `CODE: voice/core/VoiceSessionState.kt` |
| Audio focus requested around capture **and** playback, abandoned on teardown | `CODE: voice/core/VoiceSession.kt:118,177` |
| Focus loss stops capture rather than ducking it | `CODE` + `DOC:VOICE_CORE_ARCHITECTURE.md` |
| Regaining focus never silently restarts recording | `CODE: voice/realtime/RealtimeVoiceSession.kt:594` |

## 4. VA-2 — authenticated realtime transport

| Requirement | Provenance |
|---|---|
| Identity from the handshake bearer token only; never from a message | `LS §10 (p.15) VA-2` ("Keep provider-specific details out of domain logic"), `LS §14 (p.19)`; enforced in `CODE: app/api/voice_session.py` |
| Session lifecycle: accept, resume with a rotating token, expire | `CODE: app/voice/session_registry.py` |
| Bounded frames; bounded turn bytes and seconds | `CODE: app/voice/turn_channel.py` (`MAX_TURN_SECONDS = 180.0`) |
| Versioned protocol vocabulary, one event set, no second vocabulary | `AUTHORIZATION` + `CODE: app/voice/realtime_protocol.py` |
| The gateway emits nothing it cannot substantiate | `AUTHORIZATION` ("never emit a fabricated transcript … or successful turn"); consistent with `LS §10 (p.15) VA-3` key test "no raw audio leakage" |
| Client and server agree on types, fields, binary layout, mode vocabulary and handshake params | `CODE: tests/unit/test_realtime_protocol_parity.py` |
| Inbound audio carries no sequence number, so out-of-order inbound audio is **not detectable** | `DERIVED` — VA-2 wire-format decision, recorded in `DOC:voice-realtime-integration.md` |

Three server types stay **forbidden** and are derived, not listed, as
`SERVER_MESSAGE_TYPES - SUPPORTED_SERVER_MESSAGE_TYPES`:

| Forbidden type | Why it cannot be substantiated |
|---|---|
| `partial_transcript` | The only approved recogniser (faster-whisper) is turn-based. |
| `tool_call_started` | The coach's read fan-out completes inside one synchronous `Orchestrator.coach()` call, so the frame could only be sent after the work finished. |
| `operation_outcome` | No confirmable ACTION tool exists on this path. |

## 5. VA-3 — speech contracts

| Requirement | Provenance |
|---|---|
| Provider-neutral STT/TTS interfaces; adapt the existing development voice path rather than deleting it; document production-provider requirements **separately** | `LS §10 (p.15) VA-3`, verbatim | 
| Defaults remain `none` | `AUTHORIZATION`; `CODE: app/core/config.py` |
| Production selection of development-only adapters must **fail closed** | `AUTHORIZATION`; `CODE: validate_for_production()` |
| ElevenLabs excluded | `AUTHORIZATION` |
| Unavailable adapter yields typed `stt_unavailable` / `tts_unavailable`, never a fabricated transcript | `LS §10 (p.15) VA-3` key tests ("TTS failure; unsupported format; provider timeout; provider unavailable; fallback path"); `CODE: app/voice/turn_runtime.py:81,88` |
| Empty synthesized audio is a failure, not successful silence | `AUTHORIZATION` |
| A failed TTS with a valid answer returns the answer with an explicit audio-unavailable condition | `AUTHORIZATION`; `CODE: assistant_message(spoken=False)` |
| Windows SAPI5 success is **not** Linux readiness | `AUTHORIZATION`; `CODE: tests/unit/test_tts_linux_contract.py` (skips) |

## 6. VA-4 … VA-6 — tools, world data, orchestration

| Requirement | Provenance |
|---|---|
| Closed read-only tool registry; no arbitrary execution; entitlement enforced | `DOC:tool-registry.md` |
| World data carries provenance and freshness; absence is stated honestly | `DOC:world-intelligence.md` |
| A Null job provider must make the agent say live market data is unavailable — never fabricate jobs | `AUTHORIZATION` |
| Intent, planning, bounded context, grounding, session memory; no write access | `DOC:agent-orchestrator.md` |
| `turn_runtime.py` imports no `app.tools` / `app.agent` / `app.services` | `CODE: tests/unit/test_tool_registry_contracts.py` |

## 7. VA-7 — safe actions

| Requirement | Provenance |
|---|---|
| `plan -> validate -> authorize/confirm -> execute -> verify`; confirmation bound to user, capability and arguments; expiring; single-use | `LS §11 (p.16) VA-7`, verbatim sequence; `DOC:safe-actions.md` |
| **Reuse existing idempotency and concurrency infrastructure**; key per logical operation, retry reuses it | `LS §11 (p.16) VA-7` ("Reuse existing idempotency and concurrency infrastructure"); `DOC:safe-actions.md` |
| Outcomes verified against authoritative state, not assumed from a 200 | `DOC:safe-actions.md` |
| Saved / already-satisfied / failed / expired / unknown kept distinct | `AUTHORIZATION` |
| An uncertain mutation is reconciled, never automatically retried | `AUTHORIZATION` |
| Per-user rate limits on preview and execute | `CODE: app/api/actions.py` (`60/hour`) |
| Explicit user confirmation required; a spoken "yes" or model text must never bypass it | `LS §11 (p.16) VA-7` key test "confirmation bypass attempt"; `AUTHORIZATION` for the spoken-yes wording |
| `jobs.save` takes `listing_id`; `ToolInput` is `extra="forbid"` | `CODE` |
| No new high-risk actions | `AUTHORIZATION` |

## 8. VA-8 / VA-9 — Career Coach and Interview Coach

| Requirement | Provenance |
|---|---|
| Coaching grounded in evidence; no fabrication, no promises | `DOC:voice-career-coach.md` |
| A spoken turn reuses the interview engine; replay- and concurrency-safe | `LS §12 (p.17) VA-9` gate: "no duplicate answer/AI/transcription on replay; interruption and recovery work; evaluation remains honest when ungraded; interview state survives lifecycle events" |
| Deduplication, grading, progression, ownership and unknown-outcome handling preserved | `AUTHORIZATION` |
| A replayed turn must not advance the interview twice | `LS §12 (p.17) VA-9` gate, verbatim |
| Interview ownership verified **before** the socket is accepted | `AUTHORIZATION`; `CODE: responders.build_interview_responder` |
| A cancelled turn with an authoritative answer still reports its text, never its audio | `DERIVED` from the AUTHORIZATION rule above; `CODE: assistant_message(spoken=False)` |
| Server-confirmed question state, not a stale local index | `AUTHORIZATION` (§2 of the closure increment) |

## 9. VA-10 — hardening

| Requirement | Provenance |
|---|---|
| Mutation testing must prove critical **authorization/confirmation/idempotency/reconciliation** guards are load-bearing | `LS §12 (p.17) VA-10`, verbatim; `DOC:security-hardening.md` |
| Secrets absent from logs and errors | `DOC:security-hardening.md` |
| Bounded memory on all process-local state | `DOC:security-hardening.md` |
| Rate limiting on every authenticated mutation boundary | `AUTHORIZATION` (D4) |
| Process-local session state is a documented **single-worker beta constraint**; a missing grant after restart must fail honestly, never masquerade as recovery | `AUTHORIZATION` |

## 10. VA-11 — physical-device acceptance

| Requirement | Provenance |
|---|---|
| Microphone permission and real capture | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Playback and audio focus against real interruptions | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Rotation / background / foreground | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Wired and Bluetooth routing where available | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Interruption and cancellation | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Network loss and recovery | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Repeated turns and resource cleanup | `DOC:VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| Actual spoken Career Coach and Interview Coach conversations | `AUTHORIZATION`; `LS §12 (p.17) VA-11` names the device scenarios but not the two conversations explicitly |
| **No production-quality claim from emulator-only testing**; all critical device scenarios pass **or have an explicit documented, accepted limitation** | `LS §12 (p.17) VA-11` gate, verbatim. The "accepted limitation" branch is a product decision, not an engineering one |

## 11. VA-12 — integration closure

| Requirement | Provenance |
|---|---|
| The three halves are connected, not merely each tested | `AUTHORIZATION` ("Do not merely add message builders or tests around an unconnected pipeline"); `LS §17 (p.22)`: "A green demo is not the acceptance criterion" |
| Interview mode reachable with valid, authoritative ids | `AUTHORIZATION` |
| Save-job confirmation reachable from a real, user-owned listing | `AUTHORIZATION` |
| Development speech deliberately configurable, production fail-closed | `AUTHORIZATION` |
| Acceptance criteria version-controlled (**this document**) | `DOC:va12-final-audit.md§11` (D5). The normative PDFs themselves are still outside version control — see §0 |
| Multi-worker resilience of session/memory/confirmation state | **OWNER DECISION REQUIRED** — `DOC:va12-final-audit.md§6` |
| Whether a realtime voice agent is in V1 scope at all | **OWNER DECISION REQUIRED** — see §12 |

---

## 12. Recorded conflicts — not reconciled here

D5 forbids silently reconciling documents that disagree. Three disagreements stand:

1. **`PROJECT_HANDOFF.md` vs what exists.** It names *Voice Interview* as a V1 step and says
   nothing about a realtime voice agent, a career coach, or write-actions — three of which were
   built. It also still says "No implementation has started yet." It is at the workspace root,
   which is **not a git repository**, and is tracked by neither repo. Correction recorded
   separately in [`project-handoff-status-correction.md`](project-handoff-status-correction.md);
   the original file is left intact apart from one pointer banner.

2. **The normative sources are readable but unversioned** (§0). They were located on this
   machine and inspected on 2026-10-14, so the `LS §n` rows above are real citations and an
   auditor with those files can check them against the hashes in §0. What has *not* changed is
   that the files sit outside both repositories: no history, no diff, no review trail. A row
   still marked `AUTHORIZATION` is one whose wording is **not** in LS or FMS and is therefore
   reproducible only from the session transcript.

3. **`VOICE_READINESS_CHECKLIST.md` drifted** on audio focus, and the VA-8 report drifted on the
   spoken pipeline (`DOC:va12-final-audit.md§11`). Both drifts were caught only because a later
   phase re-read the code. Neither has been rewritten here; they are flagged so a reader does
   not treat them as current.

**Settled, and not reopened:** which document is authoritative for the VA roadmap, and where
the gates live. The owner has given the hierarchy (§0) and these two files are the gates.

**What the owner must still decide**, carried forward from `va12-final-audit.md` §11: whether
the V1 flow in `PROJECT_HANDOFF.md` still stands; whether process-local session state is
acceptable for a single-worker beta; whether to place the three normative PDFs under version
control; and — per `LS §12 (p.17) VA-11` — whether the physical-device gap is to be carried as an
"explicit documented, accepted limitation" or blocks acceptance outright. That branch exists in
the source and only the owner can take it.
