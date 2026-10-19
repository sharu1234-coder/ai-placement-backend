"""The authenticated realtime voice endpoint: transport **and** conversation.

VA-2 built this as a pure transport gateway -- it authenticated a socket, negotiated a format,
bounded the audio and tracked resume, and deliberately claimed nothing about what it carried,
because there was no speech or agent layer to claim anything with. VA-12's audit found the cost
of stopping there: the realtime vocabulary was fully specified and the Android client fully
built, and the server's only send sites were ``session_accepted`` and ``session_expired``. Three
halves of a conversation, none of them joined.

This file is the join. A committed turn now goes to :class:`app.voice.turn_runtime.TurnRuntime`,
which recognises the audio with the configured adapter, asks an **authenticated responder** for
an answer, and synthesises it; the results come back out through the builders in
``app.voice.realtime_protocol`` and the audio frame layout the client already decodes.

What has **not** changed, and must not
--------------------------------------
* Identity is the bearer token on the handshake and nothing else. No message on this socket names
  a user, a profile or an interview owner, and nothing here reads one from a transcript.
* Nothing is emitted that is not backed by a result. There is still no ``partial_transcript``
  (the recogniser is turn-based), no ``tool_call_started`` (the read fan-out completes inside one
  responder call, so a "started" frame would describe finished work) and no ``operation_outcome``
  (this path runs no confirmable ACTION tool). ``realtime_protocol`` holds the full reasoning and
  ``FORBIDDEN_SERVER_MESSAGE_TYPES`` makes it checkable.
* This module imports no tool registry, no orchestrator and no interview service. It names a
  mode and asks ``app.voice.responders`` for a responder bound to the authenticated user; the
  capability decision stays in that one file.

Known limitations, stated rather than designed around
-----------------------------------------------------
* **Process-local session state.** The registry and every turn channel live in this worker's
  memory. A multi-worker deployment would not resume across workers, and a restart loses every
  live session. This is the documented single-worker beta constraint; after a restart a resume
  fails honestly rather than reporting a recovery that did not happen.
* **Cooperative cancellation.** An interrupt stops the next stage and suppresses the emit. It
  cannot interrupt a recogniser or a TTS driver already blocked in a worker thread, so the work
  may finish -- its result is simply never sent. The ``asyncio`` task is deliberately *not*
  cancelled, because cancelling the ``to_thread`` await would abandon the thread without
  stopping it and skip the worker's own cleanup.
* **No turn survives a disconnect.** A turn in flight when the socket drops is abandoned. In
  interview mode its answer may or may not have reached the database; the student reconciles by
  hearing which question comes next, and a re-answer is safe because the engine recognises a
  byte-identical resend as a replay rather than storing it twice.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Final, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

from app.ai.provider import AIProvider, get_ai_provider
from app.api.deps import authenticate_access_token, get_db
from app.core.app_check import verify_app_check_token
from app.services import conversation_history
from app.services.profile_service import get_or_create_profile
from app.speech.contracts import AudioEncoding, AudioFormat
from app.voice import realtime_protocol as protocol
from app.voice import responders
from app.voice.session_registry import TooManySessions, registry
from app.voice.turn_channel import CommittedTurn, TurnChannel, TurnRefused, TurnRejection
from app.voice.turn_runtime import (
    SpokenTurn,
    TurnFailure,
    TurnRuntime,
    VoiceMode,
    new_turn_id,
    user_message_for,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice", tags=["voice"])

#: One second of 16 kHz mono PCM16 is 32 000 bytes; the client sends 20 ms frames. The cap is
#: generous enough for a slow flush and small enough that one frame cannot be a memory attack.
MAX_BINARY_FRAME_BYTES: Final[int] = 64 * 1024

#: Control messages are a handful of short fields. Anything near this is not one of ours.
MAX_TEXT_FRAME_BYTES: Final[int] = 8 * 1024

#: Roughly 13 minutes of 16 kHz mono audio. A bound on one session's total cost, so a client
#: that streams forever is disconnected rather than being billed for silently. Separate from,
#: and harsher than, the per-turn cap in `TurnChannel`: breaching a turn's bound costs a turn,
#: breaching this one costs the connection.
MAX_SESSION_AUDIO_BYTES: Final[int] = 25 * 1024 * 1024

#: No message of any kind -- not even a heartbeat -- for this long means the peer is gone in a
#: way TCP has not noticed yet.
IDLE_TIMEOUT_SECONDS: Final[float] = 120.0

#: How many turn workers may exist at once on one session: the live one, plus at most one
#: abandoned worker still draining a blocking driver it cannot be pulled out of. Without this
#: bound a client could interrupt-and-commit in a loop and accumulate threads, which is growth
#: the client controls -- the same shape of problem as the resume-grant quota in the registry.
MAX_TURN_WORKERS: Final[int] = 2

#: Outbound audio chunk size: about a second of 16 kHz mono. Even, so a chunk never splits a
#: PCM16 sample and shifts everything after it by a byte.
SPEECH_CHUNK_BYTES: Final[int] = 32 * 1024

# Close codes. 1008 and 4401/4403 are the three the Android client treats as non-retryable
# (RealtimeError.NON_RETRYABLE_CLOSE_CODES), so anything a reconnect cannot fix uses one of
# them -- otherwise the client retries a condition that will reproduce exactly.
_CLOSE_NORMAL: Final[int] = 1000
_CLOSE_POLICY: Final[int] = 1008
_CLOSE_TOO_BIG: Final[int] = 1009
_CLOSE_UNAUTHORISED: Final[int] = 4401


def build_turn_runtime() -> TurnRuntime:
    """The runtime one session will use.

    A named seam rather than an inline constructor so a test can substitute fake speech adapters
    without touching configuration or the approved defaults. Production behaviour is the default:
    `TurnRuntime` resolves its providers through `app.speech.selection`, which raises while
    `STT_PROVIDER`/`TTS_PROVIDER` are `none` -- so an unprovisioned server reports
    `stt_unavailable` on the first committed turn instead of pretending to hear something.
    """
    return TurnRuntime()


def _bearer_token(websocket: WebSocket) -> str | None:
    """Read the access token from the handshake headers.

    Headers, never the query string: a URL reaches server logs, proxy logs and crash reports,
    and a long-lived JWT that lands in one of those outlives any session it was minted for.
    The Android client sets this header through OkHttp for exactly this reason.
    """
    header = websocket.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


@dataclass
class _Connection:
    """Everything one live socket owns.

    **Ownership.** Every mutable field here belongs to the receive loop (`_pump`) and is written
    only from there. A turn worker reads `channel.is_live(...)` and sends through `send_text` /
    `send_bytes`; it mutates nothing else. That is the whole lock story, and `_send_lock` is the
    only lock: two coroutines -- the receive loop handling a resume, and a worker streaming audio
    -- can genuinely reach the socket at the same time, and interleaved frames would corrupt the
    stream rather than merely reorder it.
    """

    websocket: WebSocket
    session_id: str
    user_id: int
    channels: int
    language: str
    channel: TurnChannel
    runtime: TurnRuntime
    responder: Any
    #: The one Session that serves this whole socket, reused for transcript persistence
    #: so a turn is recorded on the same connection that produced it.
    db: Any = None
    #: The durable transcript this socket appends to. None only if it could not be
    #: opened, in which case the conversation still works and simply is not recorded --
    #: losing the record is bad, losing the student turn would be worse.
    conversation_id: Optional[int] = None
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    workers: set[asyncio.Task] = field(default_factory=set)
    #: Set once the socket is finished with, so a worker that comes back late from a blocking
    #: driver logs nothing and raises nothing instead of writing to a dead transport.
    closed: bool = False

    async def send_text(self, frame: str) -> None:
        if self.closed:
            return
        async with self._send_lock:
            if self.closed:
                return
            await self._guarded(self.websocket.send_text, frame)

    async def send_bytes(self, frame: bytes) -> None:
        if self.closed:
            return
        async with self._send_lock:
            if self.closed:
                return
            await self._guarded(self.websocket.send_bytes, frame)

    async def _guarded(self, send, frame) -> None:
        """Write, tolerating a peer that left between the check above and this line.

        A worker can come back from a blocking recogniser to find the student has walked out of
        range. Starlette raises `WebSocketDisconnect` or a `RuntimeError` ("cannot call send once
        a close message has been sent") for that, and neither is a bug worth a stack trace -- it
        is the ordinary end of a mobile connection. Marking the connection closed stops every
        later frame from retrying the same dead socket.
        """
        try:
            await send(frame)
        except (WebSocketDisconnect, RuntimeError):
            self.closed = True
            logger.info("voice session %s went away mid-send", self.session_id)


def _open_conversation(
    db: Any, *, user: Any, session_id: str, selected: Any, interview_id: Optional[int]
) -> Optional[int]:
    """Open or resume the durable transcript for this session. Returns its id, or None.

    Never raises into the handshake. A transcript is a record of the conversation, not a
    precondition for having one: if this fails the student still gets their session, and the
    failure is logged by type only.
    """
    try:
        profile = get_or_create_profile(db, user)
        conversation = conversation_history.ensure_conversation(
            db,
            profile_id=profile.id,
            kind=(
                "mock_interview" if selected is VoiceMode.INTERVIEW
                else "career_coach"
            ),
            # The server-minted session id, so a reconnect resumes this transcript rather
            # than opening a parallel one.
            session_key="voice-" + session_id,
            interview_id=interview_id,
        )
        db.commit()
        return conversation.id
    except Exception as exc:  # noqa: BLE001 - see the docstring
        db.rollback()
        logger.warning("could not open a transcript: %s", type(exc).__name__)
        return None


def _persist_turn(connection: "_Connection", turn: Any, result: Any) -> None:
    """Append what was said to the durable transcript.

    Called before anything is spoken, so a synthesis failure afterwards cannot cost the
    student their answer. Idempotent on the protocol own turn id: a reconnect that replays
    this turn adds nothing.
    """
    if connection.conversation_id is None or connection.db is None:
        return
    try:
        conversation = connection.db.get(
            conversation_history.Conversation, connection.conversation_id
        )
        if conversation is None:
            return
        if result.transcript:
            conversation_history.append_turn(
                connection.db,
                conversation=conversation,
                role="user",
                text=result.transcript,
                turn_id=turn.turn_id,
            )
        if result.answer is not None and result.answer.text:
            conversation_history.append_turn(
                connection.db,
                conversation=conversation,
                role="assistant",
                text=result.answer.text,
                turn_id=turn.turn_id,
            )
        connection.db.commit()
    except Exception as exc:  # noqa: BLE001
        # Never log the text. A persistence failure is a lost record, not a lost turn.
        connection.db.rollback()
        logger.warning(
            "could not persist turn %s: %s", turn.turn_id, type(exc).__name__
        )


def _close_conversation(connection: "_Connection", status: str) -> None:
    """Record how the session actually ended.

    An interrupted session is never rewritten to completed: a dropped connection is not a
    finished interview, and the student own record must not claim it was.
    """
    if connection.conversation_id is None or connection.db is None:
        return
    try:
        conversation = connection.db.get(
            conversation_history.Conversation, connection.conversation_id
        )
        if conversation is None:
            return
        conversation_history.mark_ended(
            connection.db, conversation=conversation, status=status
        )
        connection.db.commit()
    except Exception as exc:  # noqa: BLE001
        connection.db.rollback()
        logger.warning(
            "could not close a transcript: %s", type(exc).__name__
        )


@router.websocket("/session")
async def voice_session(
    websocket: WebSocket,
    sample_rate: int = protocol.DEFAULT_SAMPLE_RATE_HZ,
    channels: int = 1,
    locale: str = "en-IN",
    mode: str = VoiceMode.COACH.value,
    interview_id: Optional[int] = None,
    interview_question_id: Optional[int] = None,
    db: Session = Depends(get_db),
    ai_provider: AIProvider = Depends(get_ai_provider),
) -> None:
    """Open an authenticated realtime voice session and converse over it.

    Rejections before :meth:`accept` surface to the client as HTTP 403 on the upgrade, which its
    transport classifies as a non-retryable auth failure. That is the intended shape: a
    connection that was never authorised should not look like a connection that dropped.

    ``mode`` selects which authoritative path answers a turn, and it is a server-side decision
    made once, here, from a closed enum. The alternative -- letting a turn's content choose --
    would make the student's own words the thing that decides whether their answer is graded.
    """
    token = _bearer_token(websocket)
    if token is None:
        await websocket.close(code=_CLOSE_UNAUTHORISED)
        return

    try:
        await verify_app_check_token(x_firebase_appcheck=websocket.headers.get("x-firebase-appcheck"))
    except HTTPException:
        # Reusing the HTTP dependency rather than re-deriving app attestation here: one
        # implementation, so the realtime path cannot drift into accepting what HTTP refuses.
        logger.warning("Rejected a voice session with a failing App Check token")
        await websocket.close(code=_CLOSE_UNAUTHORISED)
        return

    try:
        user = authenticate_access_token(token, db)
    except HTTPException:
        await websocket.close(code=_CLOSE_UNAUTHORISED)
        return

    # The authenticated user is the only identity this connection ever has. No message may
    # name a user or profile, and none of the handlers below reads one -- the model and the
    # client are never the authorization boundary.
    try:
        protocol.validate_audio_format(sample_rate, channels)
    except protocol.ProtocolError as exc:
        await websocket.accept()
        await websocket.close(code=_CLOSE_POLICY, reason=str(exc))
        return

    try:
        selected = VoiceMode(mode)
    except ValueError:
        # An unregistered mode. Refused with a non-retryable code because reconnecting with the
        # same query string reproduces it exactly.
        await websocket.accept()
        await websocket.close(code=_CLOSE_POLICY, reason="unknown voice mode")
        return

    #: Built before `accept`, so a session is never established against an interview the
    #: authenticated student does not own and then told one turn later.
    responder: Any
    if selected is VoiceMode.INTERVIEW:
        if interview_id is None or interview_question_id is None:
            await websocket.accept()
            await websocket.close(
                code=_CLOSE_POLICY, reason="interview mode needs interview_id and interview_question_id"
            )
            return
        try:
            responder = responders.build_interview_responder(
                user=user,
                db=db,
                interview_id=interview_id,
                interview_question_id=interview_question_id,
                ai_provider=ai_provider,
            )
        except responders.InterviewUnavailable:
            # One answer for an unknown interview, an unknown question and someone else's, so a
            # socket cannot be used to confirm an id that an HTTP probe could not.
            await websocket.accept()
            await websocket.close(code=_CLOSE_POLICY, reason="interview not found")
            return
    else:
        responder = None  # built below, once the session id exists to scope its memory

    try:
        record = registry.open(user_id=user.id, sample_rate_hz=sample_rate, channel_count=channels)
    except TooManySessions:
        await websocket.accept()
        await websocket.close(code=_CLOSE_POLICY, reason="too many concurrent voice sessions")
        return

    await websocket.accept()
    resume_token = registry.issue_resume_token(record)
    # Issued now, not on close: a connection that dies does not get a chance to hand one over,
    # and a resume token the client never received is a resume that can never happen.
    await websocket.send_text(protocol.session_accepted(record.session_id, resume_token))

    if responder is None:
        responder = responders.build_coach_responder(
            user=user, db=db, session_key=f"voice-{record.session_id}"
        )

    # `locale` is a BCP-47 tag from the client; the adapters want a language. Only the language
    # subtag is passed on, because the one approved recogniser is faster-whisper `tiny.en` --
    # English-focused, and nothing here may present it as multilingual.
    language = (locale.split("-")[0] or "en").lower()

    connection = _Connection(
        websocket=websocket,
        session_id=record.session_id,
        user_id=user.id,
        channels=channels,
        language=language,
        channel=TurnChannel(
            AudioFormat(
                encoding=AudioEncoding.PCM16,
                sample_rate_hz=sample_rate,
                channel_count=channels,
            )
        ),
        runtime=build_turn_runtime(),
        responder=responder,
        db=db,
        conversation_id=_open_conversation(
            db,
            user=user,
            session_id=record.session_id,
            selected=selected,
            interview_id=interview_id,
        ),
    )

    logger.info(
        "voice session opened session_id=%s user_id=%s mode=%s rate=%s channels=%s locale=%s",
        record.session_id, user.id, selected.value, sample_rate, channels, locale,
    )

    # interrupted unless the pump returns of its own accord. A dropped connection is not
    # a completed interview, and the student own record must not claim it was.
    ended_status = "interrupted"
    try:
        await _pump(connection)
        ended_status = "completed"
    except WebSocketDisconnect:
        pass
    finally:
        _close_conversation(connection, ended_status)
        # Runs for every exit -- clean close, disconnect, limit breach, cancellation. A session
        # that stayed in the registry after its socket died would count against the user's
        # concurrency limit forever and lock them out of voice.
        connection.closed = True
        # Stops the live turn from wanting its result. The worker is left to finish: it may be
        # blocked inside a recogniser that cannot be interrupted, and cancelling the await would
        # abandon the thread rather than stop it.
        connection.channel.interrupt()
        registry.close(record.session_id)
        logger.info("voice session closed session_id=%s", record.session_id)


async def _pump(connection: _Connection) -> None:
    """Receive until the peer goes away or breaks a rule.

    Inbound audio is accumulated into the current turn and nothing else; there is no outbound
    queue, so backpressure on this side stays trivially bounded. Outbound audio is streamed by
    the turn worker, bounded by the synthesised buffer it already holds.
    """
    websocket = connection.websocket
    while True:
        try:
            message = await asyncio.wait_for(websocket.receive(), timeout=IDLE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await connection.send_text(protocol.session_expired())
            await websocket.close(code=_CLOSE_NORMAL, reason="idle")
            return

        if message.get("type") == "websocket.disconnect":
            return

        record = registry.get(connection.session_id)
        if record is None:  # closed underneath us; nothing left to update
            return

        text = message.get("text")
        if text is not None:
            if len(text.encode("utf-8")) > MAX_TEXT_FRAME_BYTES:
                await websocket.close(code=_CLOSE_TOO_BIG, reason="control message too large")
                return
            if not await _handle_text(connection, text, record=record):
                return
            registry.touch(connection.session_id)
            continue

        payload = message.get("bytes")
        if payload is None:
            continue

        if len(payload) > MAX_BINARY_FRAME_BYTES:
            await websocket.close(code=_CLOSE_TOO_BIG, reason="audio frame too large")
            return
        try:
            protocol.validate_audio_frame(payload, channel_count=connection.channels)
        except protocol.ProtocolError as exc:
            await websocket.close(code=_CLOSE_POLICY, reason=str(exc))
            return

        record.audio_bytes += len(payload)
        record.audio_frames += 1
        if record.audio_bytes > MAX_SESSION_AUDIO_BYTES:
            await websocket.close(code=_CLOSE_TOO_BIG, reason="session audio limit reached")
            return

        # Frames that arrive while a turn is running belong to the *next* turn, not the one being
        # recognised -- its bytes were detached when it was committed, so nothing can be appended
        # to it. That is the "one turn cannot consume another turn's audio" guarantee.
        connection.channel.append(payload)
        registry.touch(connection.session_id)


async def _handle_text(connection: _Connection, raw: str, *, record) -> bool:
    """Dispatch one control message. Returns False when the connection should end."""
    try:
        message = protocol.decode_client_message(raw)
    except protocol.ProtocolError as exc:
        # 1008, not a transient code: a message this gateway cannot parse will be just as
        # unparseable after a reconnect, and a retry loop on a client bug is a battery drain.
        logger.warning("voice session %s sent an invalid message: %s", connection.session_id, exc)
        await connection.websocket.close(code=_CLOSE_POLICY, reason="protocol violation")
        return False

    if message.type == "heartbeat":
        # No reply, deliberately. The client fails a session on any message type it cannot
        # parse, and it has no "heartbeat_ack". Liveness here is one-directional.
        return True

    if message.type == "commit_turn":
        record.turns_committed += 1
        await _commit(connection)
        return True

    if message.type == "interrupt":
        record.interrupts += 1
        abandoned = connection.channel.interrupt(message.get("generation_id"))
        if abandoned is not None:
            # Sent from here rather than from the worker, so the client stops playback on the
            # round trip rather than after a recogniser finishes. Late audio chunks are still
            # safe: each carries its generation id, which is what lets the client drop them.
            await connection.send_text(protocol.generation_cancelled(abandoned.generation_id))
        return True

    if message.type == "cancel_operation":
        # Counted, never resolved. This path executes no confirmable ACTION tool, so there is no
        # tracked operation to report on -- and an `operation_outcome` invented here would be
        # precisely the false success the specification forbids. Unknown stays unknown.
        record.cancels_requested += 1
        return True

    if message.type == "resume":
        grant = registry.redeem_resume_token(message.get("token"), user_id=connection.user_id)
        if grant is None:
            # Not fatal: the client is already on a healthy new session, and killing it over a
            # stale token would turn a recovered connection back into an outage. An invalid
            # token for *another* user is indistinguishable from an unknown one by design.
            logger.info("voice session %s offered an unusable resume token", connection.session_id)
            return True
        record.resumed_from = grant.session_id
        rotated = registry.issue_resume_token(record)
        # Deliberately no turn state is carried over. A turn that was in flight on the old
        # connection is not restarted here: it may already have written an interview answer, and
        # silently re-running an operation whose outcome is uncertain is the one thing a reconnect
        # must never do.
        await connection.send_text(protocol.session_accepted(connection.session_id, rotated))
        logger.info("voice session %s resumed from %s", connection.session_id, grant.session_id)
        return True

    return True  # pragma: no cover - decode_client_message rejects every other type


async def _commit(connection: _Connection) -> None:
    """Start the turn the client just committed, or say exactly why not."""
    _reap(connection)
    if len(connection.workers) >= MAX_TURN_WORKERS:
        # Checked before `commit()` so the buffer is left intact: the audio the student has
        # spoken is theirs, and discarding it because the server is busy would lose a real take.
        await _refuse(connection, TurnRejection.TURN_IN_FLIGHT)
        return

    try:
        turn = connection.channel.commit()
    except TurnRefused as exc:
        await _refuse(connection, exc.rejection)
        return

    await connection.send_text(protocol.thinking())
    worker = asyncio.create_task(_run_turn(connection, turn))
    connection.workers.add(worker)
    # Discarded on completion rather than awaited, so the receive loop keeps reading and an
    # `interrupt` can still arrive while the turn is running. Without this the only way to stop
    # a turn would be to drop the socket.
    worker.add_done_callback(connection.workers.discard)


def _reap(connection: _Connection) -> None:
    """Drop finished workers. `add_done_callback` already does this; this covers the window
    between a task finishing and the loop running its callbacks."""
    for worker in [w for w in connection.workers if w.done()]:
        connection.workers.discard(worker)


async def _refuse(connection: _Connection, rejection: TurnRejection) -> None:
    """Report a commit that started no turn.

    The frame carries a fresh id rather than the live turn's: marking the running turn as failed
    because a *second* commit was refused would be a false statement about the turn that is
    legitimately working.
    """
    logger.info("voice session %s refused a commit: %s", connection.session_id, rejection.value)
    await connection.send_text(
        protocol.turn_failed(new_turn_id(), rejection.value, _REFUSAL_MESSAGES[rejection])
    )


#: What a student can be told for each refusal. A closed mapping, for the same reason
#: `turn_runtime` has one: a message built by interpolating an exception is how an internal
#: detail reaches a screen.
_REFUSAL_MESSAGES: Final[dict[TurnRejection, str]] = {
    TurnRejection.NO_AUDIO: "I did not receive any audio for that turn.",
    TurnRejection.TURN_TOO_LARGE: "That was too long to process. Try a shorter answer.",
    TurnRejection.TURN_TOO_LONG: "That was too long to process. Try a shorter answer.",
    TurnRejection.TURN_IN_FLIGHT: "I am still working on your last turn.",
}


async def _run_turn(connection: _Connection, turn: CommittedTurn) -> None:
    """Run one committed turn and emit only what actually came back.

    Runs as its own task. Every exit path settles the turn, so a crashed worker cannot leave the
    session permanently unable to commit another one.
    """
    channel = connection.channel

    async def on_transcript(text: str) -> None:
        """Put the student's words on their screen the moment recognition produces them.

        Recognition is the slow stage -- local CPU inference, seconds on a laptop -- so holding
        the transcript until the whole turn finished would show it alongside the answer it
        preceded. Guarded by liveness for the same reason every other emit is.
        """
        if channel.is_live(turn.turn_id):
            await connection.send_text(protocol.final_transcript(turn.turn_id, text))

    try:
        try:
            result = await connection.runtime.run(
                pcm=turn.pcm,
                audio_format=turn.audio_format,
                respond=connection.responder,
                turn_id=turn.turn_id,
                cancellation=turn.cancellation,
                language=connection.language,
                on_transcript=on_transcript,
            )
        except Exception:  # noqa: BLE001 - a worker that died silently would wedge the session
            logger.exception("voice turn %s failed unexpectedly", turn.turn_id)
            if channel.is_live(turn.turn_id):
                await connection.send_text(
                    protocol.turn_failed(
                        turn.turn_id,
                        TurnFailure.RESPONSE_FAILED.value,
                        user_message_for(TurnFailure.RESPONSE_FAILED),
                    )
                )
            return

        # Durable before a single audio frame goes out. The transcript and the answer are
        # the record; a synthesis failure after this point must not cost the student their
        # turn. Abandoned turns are stored too -- the answer is authoritative either way.
        _persist_turn(connection, turn, result)

        if not channel.is_live(turn.turn_id):
            # Interrupted, or the socket closed, while this was running. The interrupt handler
            # has already sent `generation_cancelled`, and **no audio is sent**: playing a reply
            # the student has moved on from is the stale-audio case the specification singles
            # out.
            #
            # The *text* is a different matter when an answer exists. By then the responder has
            # run, which in interview mode means an answer is durably stored and the question
            # pointer has moved. Suppressing that would leave the client's view of the interview
            # behind the server's -- the student would speak next against a question nobody read
            # out. So an authoritative answer is reported as text with `spoken` false, which is
            # exactly what it is: a real answer that was never voiced.
            logger.info("voice turn %s completed after it was abandoned", turn.turn_id)
            if result.answer is not None:
                await connection.send_text(
                    protocol.assistant_message(turn.turn_id, result.answer.text, spoken=False)
                )
            return

        await _emit(connection, turn, result)
    except Exception:  # noqa: BLE001 - a task dying unretrieved would wedge the session silently
        logger.exception("voice turn %s could not be delivered", turn.turn_id)
    finally:
        # One `finally` for the whole body, so **every** exit settles -- including the abandoned
        # path, which an earlier version returned from without settling. That left the channel
        # holding one dead turn id per barge-in: unbounded growth a client could drive by
        # interrupting in a loop, which is precisely the shape of bound this module claims to
        # provide. Settling a stale id is a no-op, so this is always safe to call.
        channel.settle(turn.turn_id)


async def _emit(connection: _Connection, turn: CommittedTurn, result: SpokenTurn) -> None:
    """Send the turn's outcome, in the order the client's state machine expects.

    Every frame below is conditional on a value actually existing. There is no branch that sends
    an answer the responder did not return or audio the synthesizer did not generate.

    `final_transcript` is not sent from here: it went out from `on_transcript` the moment
    recognition produced it, which is seconds earlier on a local recogniser.
    """
    channel = connection.channel

    if result.answer is None:
        failure = result.failure  # guaranteed by SpokenTurn.__post_init__
        await connection.send_text(
            protocol.turn_failed(turn.turn_id, failure.value, user_message_for(failure))
        )
        return

    # The authoritative text, always, and it says whether a voice follows. This is the only
    # channel the answer has: `final_transcript` is the student's words, and before
    # `assistant_message` existed a reply with no audio had nowhere to go.
    await connection.send_text(
        protocol.assistant_message(turn.turn_id, result.answer.text, spoken=result.speakable)
    )

    if result.speakable:
        await connection.send_text(protocol.generation_started(turn.generation_id))
        await connection.send_text(protocol.speech_started(turn.turn_id))
        audio = result.speech.audio
        for start in range(0, len(audio), SPEECH_CHUNK_BYTES):
            if not channel.is_live(turn.turn_id):
                # Barge-in part way through playback. Stopping here leaves the client with a
                # truncated reply, which is what interrupting means; `generation_cancelled` has
                # already gone out from the interrupt handler.
                logger.info("voice turn %s was interrupted mid-playback", turn.turn_id)
                return
            await connection.send_bytes(
                protocol.encode_audio(turn.generation_id, audio[start : start + SPEECH_CHUNK_BYTES])
            )
        await connection.send_text(protocol.speech_stopped(turn.turn_id))
        await connection.send_text(protocol.generation_done(turn.generation_id))
        return

    if result.failure is TurnFailure.CANCELLED:
        # The answer exists and was sent, because it is authoritative and may already have
        # changed interview state. Only the speaking of it did not happen.
        await connection.send_text(protocol.generation_cancelled(turn.generation_id))
        return

    if result.failure is not None:
        # A synthesis failure after a real answer. Both frames are true at once and neither
        # contradicts the other: the reply stands, and the speech stage is what failed. The
        # message mapping already reads that way -- "I have a reply but cannot speak it".
        await connection.send_text(
            protocol.turn_failed(
                turn.turn_id, result.failure.value, user_message_for(result.failure)
            )
        )
