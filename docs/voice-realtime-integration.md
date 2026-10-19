# Realtime voice: the connected backend path

What this document records: the backend half of a realtime spoken conversation, now actually
joined up. VA-2 built the transport, VA-6/VA-8 built the coach, VA-9 built the authoritative
spoken interview turn, and VA-12's audit found that none of them was connected to the others —
the agreed message vocabulary was fully specified, the Android client was fully built to receive
it, and the server's only send sites were `session_accepted` and `session_expired`.

This increment is the join. It is **backend only**: the Android realtime client package is still
unmounted in `main/`, so no device-to-server conversation has been demonstrated.

## The call chain

```
WebSocket /voice/session?mode=coach|interview
  │
  ├─ authenticate (bearer header)  ──────────────► app/api/deps.authenticate_access_token
  ├─ App Check                     ──────────────► app/core/app_check.verify_app_check_token
  ├─ validate_audio_format(rate, channels)  ─────► app/voice/realtime_protocol
  ├─ VoiceMode(mode)               ──────────────► closed enum, refused with 1008 if unknown
  ├─ build responder (BEFORE accept)──────────────► app/voice/responders
  │     coach      → Orchestrator.coach(AgentRequest, ToolContext(user, db), SessionMemory)
  │     interview  → interview_service.owned_question(...) then voice_interview.submit_spoken_turn
  ├─ registry.open(...)            ──────────────► app/voice/session_registry (process-local)
  └─ accept → session_accepted + resume token
        │
        ▼ _pump (sole owner of all mutable turn state)
        ├─ binary frame → validate_audio_frame → TurnChannel.append
        ├─ commit_turn  → TurnChannel.commit() ──► CommittedTurn (bytes detached)
        │                 └─ asyncio.create_task(_run_turn)
        │                       └─ TurnRuntime.run(pcm, format, respond, cancellation, on_transcript)
        │                             ├─ asyncio.to_thread(stt.transcribe)   ← app/speech/selection
        │                             ├─ on_transcript → final_transcript    (emitted here, early)
        │                             ├─ await respond(text)                 ← the responder above
        │                             └─ asyncio.to_thread(tts.synthesize)   ← app/speech/selection
        │                       └─ _emit → assistant_message, generation_*, speech_*, audio frames
        ├─ interrupt    → TurnChannel.interrupt(generation_id) → generation_cancelled
        ├─ resume       → registry.redeem_resume_token → session_accepted (rotated)
        └─ heartbeat / cancel_operation → counted, never answered
```

## What is emitted, and what is refused

`SUPPORTED_SERVER_MESSAGE_TYPES` — twelve types, each backed by a real result:

| Type | Sent when |
|---|---|
| `session_accepted` | handshake, and on a redeemed resume |
| `thinking` | a turn was accepted and work has begun |
| `final_transcript` | recognition produced text, emitted as soon as it exists |
| `assistant_message` | the responder returned an answer; `spoken` says whether audio follows |
| `turn_failed` | a typed stage failure or a refused commit — **the session stays healthy** |
| `generation_started` / `generation_done` | audio streaming began / completed |
| `speech_started` / `speech_stopped` | the assistant's audio frames start / end |
| `generation_cancelled` | an interrupt matched the live generation |
| `session_expired` | idle timeout |
| `error` | reserved; non-retryable on the client, so a close code is used instead |

`FORBIDDEN_SERVER_MESSAGE_TYPES` — three types that remain in the vocabulary and are never sent.
Each is a property of the implementation, not a preference:

* **`partial_transcript`** — the only approved recogniser is turn-based faster-whisper. A partial
  could only be made by splitting a final transcript, which is a claim about what the microphone
  heard mid-sentence that nothing observed.
* **`tool_call_started`** — the coach's read fan-out completes inside one synchronous
  `Orchestrator.coach()` call, so a frame sent afterwards would announce work already finished.
  The names travel on `Answer.tools_used` instead.
* **`operation_outcome`** — this path executes no confirmable ACTION tool. The interview answer
  write is the interview engine's own authoritative path, not a tracked operation the client can
  cancel, so an outcome frame would hand over an operation id that resolves nothing.

The set is derived (`SERVER_MESSAGE_TYPES - SUPPORTED_...`), so adding a type to the allowlist
removes it from the forbidden set automatically — which is the direction a mistake travels.

### Two types were added to the vocabulary

`assistant_message` and `turn_failed`, on **both** halves (`realtime_protocol.py`,
`RealtimeTransport.kt`, `WebSocketRealtimeTransport.kt`), because the agreed vocabulary made two
required behaviours unimplementable:

1. There was no channel for the **assistant's text**. `final_transcript` is the student's speech;
   the reply existed only as PCM. So "a failed TTS operation may still have a valid authoritative
   textual answer — return that answer with an explicit audio-unavailable condition" had nowhere
   to put the answer.
2. There was no **non-fatal per-turn failure**. The client maps `error` to a non-retryable
   `ProtocolViolation` and ends the session, so a recogniser that heard nothing would have cost
   the whole conversation.

`tests/unit/test_realtime_protocol_parity.py` reads the Kotlin source and fails if the two halves
disagree, including a check that every frame the gateway can emit carries the fields the client's
decoder refuses a frame for.

## Ownership and lock boundaries

* **`_pump` owns every mutable field** on `_Connection` and `TurnChannel`. `append`, `commit`,
  `settle` and `interrupt` are called only from there, in one coroutine, so the event loop
  serialises them and no lock is needed.
* **One lock, `_send_lock`**, around every outbound frame. Two coroutines can genuinely reach the
  socket at once — the receive loop answering a resume, and a worker streaming audio — and
  interleaved frames would corrupt the stream rather than merely reorder it.
* **A worker mutates nothing.** It reads `channel.is_live(turn_id)` before each emit and calls
  `settle` on exit. A late `settle` for an interrupted turn is ignored, so it cannot retire the
  turn that replaced it.
* **Bytes are detached, not copied.** `commit()` rebinds the buffer, so a frame arriving between
  "copy" and "clear" cannot land in both turns or neither.

## Bounds

| Bound | Value | Breaching it costs |
|---|---|---|
| One inbound frame | 64 KiB | the connection (1009) |
| One control message | 8 KiB | the connection (1009) |
| One turn, bytes | 6 MiB | the turn (`turn_too_large`) |
| One turn, duration | 180 s | the turn (`turn_too_long`) |
| Whole session audio | 25 MiB | the connection (1009) |
| Concurrent turn workers | 2 (one live, one draining) | the commit (`turn_in_flight`) |
| Idle with no message | 120 s | `session_expired`, then a normal close |
| Spoken answer length | 1 200 characters, trimmed out loud | — |
| Transcript | 5 000 characters | the turn (`transcript_too_long`) |

The per-turn caps refuse a turn rather than the connection: a student who rambles should lose a
take, not a conversation. The session-wide cap is the harsher bound that closes the socket.

## Stage timings

`LS §13.3 (p.18)` requires six performance metrics and none had ever been measured. Every turn
now emits one line from `app/voice/turn_metrics.py`:

```
voice turn metrics turn=t-554c10c74091 gen=g-5fa7cfaa74d1 outcome=ok total_ms=2233
  audio_ms=2627 stt_ms=2014 respond_ms=0 tts_ms=219 tts_first_audio_ms=219
  transcript_chars=44 speech_bytes=69916 rtf=0.767
  stt=FasterWhisperSpeechToText tts=Pyttsx3TextToSpeech
```

**It cannot carry what the student said.** `TurnMetrics` has no field that holds free text, and
`__post_init__` enforces it: `outcome` and `cancelled_at` come from closed vocabularies,
`tools_used` entries must match the catalogue's `family.verb` shape, provider fields must look
like class names. Content appears only as lengths -- `transcript_chars`, `speech_bytes`. The same
"make it unconstructible" tactic as `SpokenTurn.__post_init__`, for the same reason: a rule the
type enforces is not a rule anyone has to remember at 2 a.m.

Durations come from one `_now()` indirection over `time.monotonic`, so a clock correction cannot
produce a negative duration and the start cannot sit on a different clock from the elapsed-time
helper -- which is exactly the bug the first implementation had.

`stt=`/`tts=` name the adapter class that actually ran. That is the only honest way to tell a
real-inference figure from one taken against a test fake, and it is why these lines are usable as
evidence at all.

### What is deliberately absent

| Not reported | Why |
|---|---|
| Per-tool invocation and completion times | The coach runs its whole read fan-out inside one synchronous `Orchestrator.coach()` call, so the runtime sees one `respond` boundary. *Which* capabilities ran is recorded; when each started is not available. Same limitation that makes `tool_call_started` a forbidden frame. |
| Partial-transcript or streaming latency | The only approved recogniser is turn-based. There is no first-partial moment to measure. |
| True interruption latency | `cancelled_at` names the checkpoint that *observed* cancellation. Cancelling cannot interrupt a recogniser already running in a worker thread; its result is discarded. Naming the field for the checkpoint keeps that honest. |
| Anything about a device | Every number is server-side, between the committed turn and the synthesized reply. It excludes capture, upload, playback and the network, so it is **not** a spoken-turn latency as a student experiences it. |

### Measured on one Windows host, 2026-10-14

Three turns on one runtime, real faster-whisper inference on a real 2.628 s synthesized utterance:

| Turn | total_ms | stt_ms | tts_ms | rtf |
|---|---|---|---|---|
| 1 — cold, loads the weights | 8 484 | 8 266 | 218 | 3.147 |
| 2 — warm | 2 515 | 2 312 | 202 | 0.880 |
| 3 — warm | 2 233 | 2 014 | 219 | 0.767 |

Steady-state recognition is faster than real time; the first turn after start-up costs ~8.5 s
because the weights load then. A single cold measurement taken alone reported rtf 4.9, which
would have badly misrepresented the system -- which is why the cold and warm turns are reported
separately rather than averaged.

## Honest limitations

* **Process-local state.** The session registry and every turn channel live in one worker's
  memory. This is the documented single-worker beta constraint. It is **not** horizontally
  scalable and **not** restart-resilient: after a restart a resume token fails, honestly, rather
  than reporting a recovery that did not happen.
* **Cooperative cancellation only.** An interrupt stops the next stage and suppresses the emit. It
  cannot interrupt a recogniser or TTS driver already blocked in a worker thread, so the work may
  finish — its result is simply never sent. The `asyncio` task is deliberately not cancelled,
  because cancelling the `to_thread` await abandons the thread without stopping it.
* **A cancelled turn with an authoritative answer still reports its text.** No audio is sent for a
  cancelled generation, ever. But once the responder has run, interview state may be durably
  changed and the question pointer moved; suppressing that would leave the client a question
  behind the server. So the answer goes out as `assistant_message` with `spoken: false`.
* **No turn survives a disconnect.** An in-flight turn is abandoned. In interview mode its answer
  may or may not have reached the database; the student reconciles by hearing which question comes
  next, and a re-answer is safe because the engine recognises a byte-identical resend as a replay.
* **Inbound audio has no sequence number.** A VA-2 decision: the client sends a raw 20 ms PCM
  frame every 20 ms, and an envelope would have inflated the most frequent message type for
  nothing. Out-of-order *inbound audio* is therefore not detectable; ordering comes from TCP. What
  is enforced is a stale turn or generation id on a control message, which is where a late client
  actually causes harm.
* **No speech provider is approved.** `STT_PROVIDER` and `TTS_PROVIDER` default to `none`, so on a
  stock server the first committed turn returns `stt_unavailable`. The development adapters
  (faster-whisper `tiny.en`, pyttsx3) must be selected through an explicit development-only
  configuration, and `validate_for_production()` fails closed if production selects either.
  faster-whisper `tiny.en` is English-focused and is not presented as multilingual.
* **No device has been tested.** No latency, accuracy or audio-quality figure in this system has
  been measured. Android screen integration and real-device acceptance remain outstanding.

## Mode selection

`mode` is a query parameter validated against the `VoiceMode` enum once, at handshake, before the
socket is accepted. Letting a turn's *content* choose would make the student's own words decide
whether their answer is graded.

Interview mode additionally requires `interview_id` and `interview_question_id`, and ownership is
verified through `interview_service.owned_question` **before accept** — so a session can never be
established against someone else's interview and discover it one turn later. An unknown interview,
an unknown question and someone else's are all refused identically, so a socket cannot confirm an
id that an HTTP probe could not.

The responder then tracks which question each turn answers, advancing only to the `next_question`
the engine itself returned. The client never names a question id on the socket.
