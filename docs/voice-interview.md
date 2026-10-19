# Voice interview turn (VA-9)

**Status: one server-side route that composes the existing interview engine. No speech
provider, no realtime recognition, no Android change. The spoken end-to-end path remains
blocked and is not claimed.**

## 1. What was already there

Verified against the code, not inherited from the VA-8 report:

| Thing VA-9 needs | Already existed |
|---|---|
Voice interview mode | `Interview.mode` accepts `"voice"`; `InterviewEvaluation` has `speaking_pace` and `filler_word_rate` |
Spoken answer submission | `POST /interviews/{id}/answer/audio` — M4A upload, magic-byte checked, transcribed through `AIProvider.transcribe` |
Question playback | `GET /interviews/{id}/questions/{qid}/audio` — synthesized on demand, never persisted |
Android voice screen | `VoiceInterviewScreen` + `VoiceInterviewViewModel` — record, upload, advance |
**Replay detection** | `_is_replay_of` — a resend is answered with the stored answer, a different answer is a conflict |
**Duplicate prevention** | `_insert_answer_once` — the unique index on `interview_question_id` decides, not a preceding read |
**Evaluation idempotency** | `_insert_evaluation_once`, `_grade_ungraded_row`, `_persist_ungraded` |
Gated concurrency tests | `tests/api/test_answer_result_replay.py` with `h.post_together(gate="INSERT INTO interview_answers")` |

So VA-9's §5 invariants were largely **already implemented and tested**. The work was to verify
them, extend them to the new path, and add the one thing missing.

## 2. What VA-9 added, and why only this

`POST /interviews/{interview_id}/voice-turn` → `{interview_question_id, transcript}`.

It composes the two calls a voice client otherwise makes back to back — `POST /answer` then
`GET /next-question` — so the window between them cannot leave a client holding a stored answer
it believes failed. That composition is the **only** new logic. No scoring, no grading, no
question generation, no progression rule, no second interview state machine: all of those stay
in `interview_service`, and a test asserts `voice_interview.py` contains no `overall_score`, no
evaluation construction, no `db.add`, no `db.commit` and no `db.query`.

**Why the transcript goes through the text path.** `submit_answer` compares submitted text
against the stored answer, so a resend is recognised byte for byte. The audio path cannot: the
recording is not persisted, so its replay identity is only "this question's stored answer also
came from audio" — which its own docstring calls the honest limit of what existing state can
prove. A realtime voice turn already has a transcript when it reaches the server, so the text
path gives the **stronger** guarantee instead of inheriting the weaker one.

**Why there is no new turn token.** `interview_question_id` *is* the turn token. A late result
carries the id of the question it belongs to, so it is submitted against that question, where
the engine's own guard answers it — as a replay if the same answer, as a refusal if different.
A separate token would be a second mechanism that could disagree with the first.

**Why no `Idempotency-Key`.** The engine is already the deduplicator, and §5 says to use the
existing mechanism. An operation claim on top would be a second mechanism over the same write.

**Why the cap is 5 000 characters.** Taken from `SubmitInterviewAnswerRequest.answer_text`
rather than chosen again. A voice path accepting a longer answer than the typed path would mean
the two modes disagree about what a valid answer is, and the grader would read something the
text route would have refused.

## 3. The turn states

```
ANSWER_ACCEPTED            stored for the first time, next question attached
ANSWER_REPLAYED            same answer already stored — idempotent success, advanced nothing twice
ANSWER_REFUSED             this question holds a *different* answer; first one is final; no next question
INTERVIEW_COMPLETE         stored, and no further questions
NEXT_QUESTION_UNAVAILABLE  stored, but the next question could not be generated — do not resubmit
```

`answer_is_stored` is the flag that decides whether resubmitting is safe, and it is deliberately
**not** `state == ANSWER_ACCEPTED`: four of the five states are safe, and two of them would read
as failure to a naive client. `VoiceTurn.__post_init__` refuses a refused-answer payload that
carries a next question, and refuses an accepted or replayed one that does not say what is
stored.

Evaluation availability is separate and tri-valued — `not_ready` / `ready` / `unknown` — read
from the interview row itself, because completion is the engine's decision and a client counting
questions would hold a second, disagreeing copy of it. "Not yet" and "we tried and could not"
do not read the same.

**The answer is never lost.** It is durable the moment `submit_answer` returns; question
generation is an AI call that can fail afterwards. So a failure there is
`NEXT_QUESTION_UNAVAILABLE` *with* the stored answer, never a failed turn — a client retrying
the whole turn on a generic error would be resubmitting an answer the server already holds.

## 4. Reused services, for review

| Reused | Used for |
|---|---|
`interview_service.owned_question` | both ownership checks, in one place (extracted in VA-9 from `synthesize_question_audio`, which now calls it) |
`interview_service.answer_text_for` | telling a first submission from a resend; a read, creates nothing |
`interview_service.submit_answer` | the authoritative store, including `_is_replay_of` and `_insert_answer_once` |
`interview_service.get_next_question` | question generation and progression |
`interview_service.read_interview` | completion state for evaluation availability |
`get_ai_provider`, `limiter`, `get_current_user`, `get_or_create_profile`, `ai_unavailable` | unchanged route-layer dependencies |

## 5. Security

Identity from the existing bearer-token boundary; ownership through `owned_question`, whose
`InterviewNotFoundError` is identical for "does not exist", "not yours" and "belongs to another
interview", so an id cannot be confirmed by probing. `extra="forbid"` on the request, so an
invented `score`, `completed`, `profile_id` or `answer_text` is a 422. A malicious transcript is
**stored verbatim** — it is the student's own answer and the grader must see what was actually
said — while changing no interview state. No transcript, token or credential is logged; the log
line carries profile, interview, question and outcome only.

No raw-audio storage was introduced, no transcript reuse, and no new retention behaviour.

## 6. Tested, and explicitly not tested

**Tested (42 tests), deterministic, no model and no speech provider:** the full turn; a complete
interview driven only by voice turns; evaluation through the existing endpoint; duplicate turns;
different-answer refusal; stale callbacks; reconnect replay (**simulated by resending, not an
actual dropped connection**); two gated concurrent turns, identical and differing; a failed
next-question keeping the answer; empty and oversized transcripts; cross-user and cross-interview
access; altered identifiers; invented fields; malicious transcript; log hygiene; and the
structural assertion that no scoring or SQL was duplicated.

**Not tested, and not claimed:**

| Gate | Status | What would close it |
|---|---|---|
Spoken end to end (STT → transport → engine → TTS) | **BLOCKED** | an approved STT and TTS provider; VA-2 performs no recognition |
Real speech provider | **BLOCKED** | provider approval; every test here uses the existing `FakeAIProvider` |
Realtime socket interview | **NEEDS TEST** | Android client work plus gateway message types that VA-9 deliberately did not add |
Physical-device interview | **NEEDS TEST** | a device run |
Actual interruption and recovery | **NEEDS TEST** | a real dropped connection and an app lifecycle run; VA-9 simulates replay only |
Android background/foreground | **NEEDS TEST** | no Android file was changed in VA-9 |

## 7. Known limitations

- **The realtime path is untouched.** The VA-2 gateway still sends only `session_accepted` and
  `session_expired`; it performs no recognition. The protocol module *declares* richer message
  types as the agreed vocabulary, which is not the same as emitting them — a guard now asserts
  the gateway's actual send set rather than searching the protocol file for strings.
- **`/answer/audio` still uses the legacy `AIProvider.transcribe`**, not the VA-3 `SpeechToText`
  contract. Migrating it was deliberately not done: it is a working, tested contract and there
  is no approved provider to validate the migration against.
- Question playback remains on the existing `/audio` route and the legacy
  `AIProvider.synthesize_speech`.
- No Android change, so the file-based voice screen, navigation and the unmounted VA-7
  confirmation surface are all exactly as they were.
- Carried forward: process-local VA-2 registry / agent memory / VA-7 confirmations; world
  provider unconfigured; Linux eSpeak unproven; `_STATUS_RANK` unchanged.
