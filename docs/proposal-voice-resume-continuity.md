# Proposal — fail-closed voice resume continuity

**Status: proposal only. No code written.** The defect in §1 is confirmed in the current tree.

---

## 1. The confirmed defect

In `app/api/voice_session.py` the order of operations is:

1. the socket is accepted and a **new** conversation row is opened, keyed `voice-<new
   session_id>` (`_open_conversation`, reached while the `_Connection` is constructed);
2. only afterwards does the client send `{"type": "resume", "token": ...}`;
3. the handler redeems the token, verifies the user, sets `record.resumed_from =
   grant.session_id`, rotates the token and replies `session_accepted`.

Step 3 records **lineage** and nothing else. `connection.conversation_id` still points at the
row created in step 1, so every subsequent turn is appended to a brand-new conversation while
the client has been told recovery succeeded. The student sees an unbroken session; their
history gets two rows, the second starting mid-conversation with no coaching context.

Two things are already correct and must stay correct:

- **No turn state is carried over.** The handler's comment is explicit that a turn in flight on
  the old connection is not restarted, "it may already have written an interview answer, and
  silently re-running an operation whose outcome is uncertain is the one thing a reconnect must
  never do." This proposal does not change that.
- **Ownership is checked.** `redeem_resume_token(..., user_id=connection.user_id)` scopes
  redemption to the authenticated user, and an unusable token is treated as non-fatal rather
  than as a reason to kill a healthy new session.

So this is a continuity defect, not a security one. The fix must not turn it into a security
one.

## 2. Root cause

The conversation is opened before the resume intent is known. Resume is expressed as a message
*after* the handshake, so by the time the server learns the client wanted to continue
something, it has already committed to something new.

## 3. Remedy: learn the intent before opening anything

**Carry the resume token at connect time**, as a query parameter alongside the existing
`interview_id` / `interview_question_id`, and resolve it *before* `_open_conversation`:

- **Valid token** → do not open a conversation. Rebind `connection.conversation_id` to the
  original row, after re-verifying that its `profile_id` matches the authenticated profile.
- **Absent token** → open a new conversation, exactly as today.
- **Present but invalid, expired, already redeemed, or owned by another profile** → open a new
  conversation **and say so**.

Keeping the `resume` message as a deprecated no-op path, or removing it, is a compatibility
decision (§8).

### Fail-closed is about what is reported, not about refusing

The rule:

> Never report a resumed session unless the original conversation row was actually rebound.

`session_accepted` gains `resumed: bool` and the `conversation_id` it is writing to. Today the
client infers success from the frame arriving at all, which is why a failed resume is
indistinguishable from a successful one. With the flag, a client that asked to resume and reads
`resumed: false` shows a clearly labelled fresh conversation.

Refusing the connection outright was considered and rejected: a student whose signal dropped
would be locked out of the feature by a stale token, turning a recovered connection back into
an outage. Failing closed on the *claim* costs nothing; failing closed on the *connection*
costs the session.

## 4. Token ownership, single use, expiry, concurrency

- **Binding.** The grant binds `(user_id, session_id, conversation_id)`. `conversation_id` is
  the addition — it is what makes rebinding possible without trusting anything the client sent.
- **Ownership, twice.** The existing `user_id` check at redemption, plus a re-check that the
  target conversation's `profile_id` is the authenticated profile before any turn is appended.
  Defence in depth: the second check is what makes cross-user transcript contamination
  impossible even if a grant were ever mis-issued.
- **Single use.** Redemption consumes the grant and issues a rotated one. Unchanged.
- **Expiry.** Short, and shorter than the conversation idle timeout, so a token cannot outlive
  the thing it refers to.
- **Concurrency.** Two reconnects racing one grant: redemption is already single-use, so at
  most one wins and the loser gets `resumed: false` and a fresh conversation. The stronger
  guarantee — that no two live sockets ever write to one conversation — needs a single-writer
  claim on `conversation_id`.
- **Indistinguishability.** An unknown token and another user's token must stay
  indistinguishable in the response, as they are today.

## 5. Conversation identity and history pagination

Rebinding the existing row is deliberately chosen over creating a linked successor row.

`list_conversations` orders by activity with `id` as a tiebreak "so a page boundary is stable
when two conversations share a timestamp". Successor rows would mean one logical conversation
appearing as several entries, each needing merging at read time and at every page boundary.
Rebinding leaves pagination untouched: one conversation, one row, turns appended in order.

`session_key` stays as minted for the original session; `resumed_from` keeps recording lineage
for diagnostics. Turn de-duplication already exists — `append_turn` returns `None` for a
`turn_id` it has already stored, which is what keeps a replayed frame from duplicating a turn
after reconnect.

## 6. Interrupted in-flight turns

Unchanged in behaviour, made explicit in contract:

- A turn in flight when the socket died is **not** restarted. Its outcome is unknown and stays
  unknown; an uncertain operation is never reported as success.
- The **interview engine remains the single source of truth** for question progression and
  grading. After a resume the client must re-read the authoritative current question rather
  than resuming from the question it held, because the server state may have advanced while the
  socket was gone. This is the one place where a client-held value would be genuinely stale —
  unlike the per-socket `interview_question_id`, which is scoped to one question by
  construction.
- No answer is re-submitted, nothing is re-graded, and the interview is never advanced twice.
  The existing `operation_claims` machinery already enforces one mutation per key; resume adds
  no second mechanism.

## 7. Single-worker constraint

`session_registry` is in-memory, which the module docstring already records: "A multi-worker
deployment would not resume across workers, and a restart loses every session." This proposal
inherits that limit and does not pretend otherwise.

What multi-worker would require, stated so it is a decision and not a surprise: grants in
shared storage rather than process memory, and the single-writer claim on `conversation_id`
backed by the database rather than by a process-local set. Choosing a production topology is
not this proposal's call.

## 8. Compatibility

The change adds a query parameter and two fields to `session_accepted`. Both are additive.

A client that still sends the `resume` *message* must not silently get today's broken
behaviour. Two options for the owner:

- **A.** Keep the message, and have it return a typed "resume must be presented at connect
  time" notice without rebinding. Honest, and no old client is misled.
- **B.** Remove the message type in the same change as the client update, since both repositories
  are versioned together and the feature is in constrained beta.

Either way the protocol definition, the Android client, the compatibility tests and this
document change together.

## 9. Regression test matrix

| # | Scenario | Required outcome |
|---|---|---|
| 1 | Reconnect with a valid token | Same `conversation_id`; turns appended to the original transcript; `resumed: true` |
| 2 | Reconnect with no token | New conversation; `resumed: false` |
| 3 | Token replayed after redemption | New conversation; `resumed: false`; no rebinding |
| 4 | Expired token | New conversation; `resumed: false` |
| 5 | Another user's token | New conversation; `resumed: false`; indistinguishable from unknown; **no cross-profile read** |
| 6 | Token whose conversation belongs to another profile | Refused at the profile re-check; nothing appended |
| 7 | Two simultaneous reconnects, one grant | Exactly one rebinds; the other reports `resumed: false`; no two sockets writing one conversation |
| 8 | Old connection's events arrive after the new one exists | Ignored by generation; nothing appended, nothing sent |
| 9 | Reconnect during STT | No duplicate user turn; no re-recognition of the old audio |
| 10 | Reconnect during responder execution | No duplicate assistant turn; no second tool invocation |
| 11 | Reconnect during TTS or playback | No duplicate audio generation; prior generation cancelled |
| 12 | Reconnect mid-interview turn | Interview advances exactly once; no re-grade; client re-reads the authoritative question |
| 13 | Replayed `turn_id` after reconnect | `append_turn` returns `None`; one stored turn |
| 14 | History after an interruption | One conversation, turns in order, stable across page boundaries |
| 15 | Resume failure | Resources released; no leaked worker, lease or socket |
| 16 | Protocol compatibility | A client sending the old `resume` message is told, not silently mis-served |

Tests 5, 6 and 7 are the ones that must exist before this ships: they are the difference between
a continuity fix and a cross-user data incident.

## 10. Owner decisions required

1. **Compatibility option A or B** in §8.
2. **Whether `resumed: false` should surface in the UI** as a labelled new conversation, and
   with what wording. The backend will report it honestly either way; what the student is told
   is a product decision.
3. **Whether to add the database-backed single-writer claim now** or accept the single-worker
   limitation for the constrained beta and record it as a pre-multi-worker blocker.
