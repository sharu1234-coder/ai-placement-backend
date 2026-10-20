"""The realtime wire contract, as the Android client actually speaks it.

This module is the server half of
`ai-placement-android/app/src/main/java/com/aiplacementos/app/voice/realtime/WebSocketRealtimeTransport.kt`.
It was written against that file rather than designed independently: a second protocol that
merely passes its own tests is how two halves of a system drift until the first real device
finds out. `tests/unit/test_realtime_protocol_parity.py` reads the Kotlin source and fails if
the two ever disagree.

No FastAPI, Starlette or database import belongs here. Keeping the contract free of the
transport that carries it is what lets it be tested exhaustively without a socket, and mirrors
the client, where `RealtimeTransport.kt` names no HTTP client either.

Framing
-------
* Control messages are JSON **text** frames.
* Client audio is a **binary** frame of raw PCM16 little-endian -- no header. The client sends
  one every 20 ms, and an envelope would inflate the most frequent message type for nothing.
* Server audio is a binary frame of ``[version:1][id_length:1][generation_id][PCM16LE]``. The
  generation id travels with the audio so a chunk arriving after an interruption can be
  identified and dropped rather than played against whatever generation is current.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

# --------------------------------------------------------------------------- message types

#: Everything the client may send. Anything else is a protocol violation.
CLIENT_MESSAGE_TYPES: Final[frozenset[str]] = frozenset(
    # "end_session" is how a student says "I am finished". It exists because nothing else
    # in this set can say it, and without it a clean ending was indistinguishable from a
    # dropped socket -- see `_PUMP_REASON_STATUS` in app/api/voice_session.py.
    #
    # Additive and backward compatible: a client that never sends it simply never produces a
    # `completed` conversation, which is the safe direction to be wrong in.
    {"commit_turn", "interrupt", "resume", "cancel_operation", "heartbeat", "end_session"}
)

#: Everything the client can parse. Sending a type absent from this set is a server bug: the
#: client turns an unknown type into a non-retryable ProtocolViolation and fails the session,
#: so "the client will just ignore it" is never true here.
SERVER_MESSAGE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session_accepted",
        "partial_transcript",
        "final_transcript",
        "assistant_message",
        "turn_failed",
        "speech_started",
        "speech_stopped",
        "thinking",
        "tool_call_started",
        "generation_started",
        "generation_done",
        "generation_cancelled",
        "operation_outcome",
        "session_expired",
        "error",
    }
)

#: What the gateway is permitted to emit, now that a turn runtime actually produces results.
#:
#: Three types stay out, each for a reason that is a property of the implementation rather than
#: a matter of taste:
#:
#: * ``partial_transcript`` -- the only approved recogniser is turn-based faster-whisper, so a
#:   partial could only be manufactured by splitting a final one. That is a claim about what the
#:   microphone heard mid-sentence that nothing observed.
#: * ``tool_call_started`` -- the coach's read fan-out runs to completion inside one synchronous
#:   responder call, so a frame sent afterwards would describe as *starting* something that has
#:   already finished. The names are carried on ``Answer.tools_used`` for a future streaming
#:   responder; the frame waits for one.
#: * ``operation_outcome`` -- this path executes no confirmable ACTION tool. The interview answer
#:   write is the interview engine's own authoritative path, not a tracked operation the client
#:   can cancel, and an outcome frame would hand the client an operation id that resolves
#:   nothing. Keeping it out is what makes "the gateway emits no unsubstantiated action message"
#:   a checkable fact rather than a promise.
SUPPORTED_SERVER_MESSAGE_TYPES: Final[frozenset[str]] = frozenset(
    {
        "session_accepted",
        "session_expired",
        "error",
        "thinking",
        "final_transcript",
        "assistant_message",
        "turn_failed",
        "speech_started",
        "speech_stopped",
        "generation_started",
        "generation_done",
        "generation_cancelled",
    }
)

#: The types the gateway must never emit, stated positively so a test can assert on it. Derived
#: rather than written twice -- adding a type to the supported set above automatically removes it
#: from here, which is the direction a mistake actually travels.
FORBIDDEN_SERVER_MESSAGE_TYPES: Final[frozenset[str]] = (
    SERVER_MESSAGE_TYPES - SUPPORTED_SERVER_MESSAGE_TYPES
)

BINARY_FRAME_VERSION: Final[int] = 1
_HEADER_BYTES: Final[int] = 2
_MAX_GENERATION_ID_BYTES: Final[int] = 0xFF

# --------------------------------------------------------------------------- audio limits

#: Mirrors VoiceAudioConfig.DEFAULT_SAMPLE_RATE_HZ on the client. Only a default: the client
#: states its real format on the handshake and that is what gets validated.
DEFAULT_SAMPLE_RATE_HZ: Final[int] = 16_000

#: Mirrors VoiceAudioConfig.MIN_SAMPLE_RATE_HZ / MAX_SAMPLE_RATE_HZ on the client.
MIN_SAMPLE_RATE_HZ: Final[int] = 8_000
MAX_SAMPLE_RATE_HZ: Final[int] = 48_000
MAX_CHANNELS: Final[int] = 2
BYTES_PER_SAMPLE: Final[int] = 2


@dataclass(frozen=True)
class ClientMessage:
    """A validated client control message."""

    type: str
    payload: dict[str, Any]

    def get(self, key: str) -> Any:
        return self.payload.get(key)


class ProtocolError(ValueError):
    """The peer sent something outside the contract.

    Separate from a transport failure because the responses differ: a protocol error means
    reconnecting will reproduce it, so the connection is closed rather than retried.
    """


def decode_client_message(raw: str) -> ClientMessage:
    """Parse a client text frame.

    Raises :class:`ProtocolError` for anything malformed. Deliberately strict -- a gateway that
    guesses at a half-understood message is a gateway that acts on one.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError("frame is not valid JSON") from exc

    if not isinstance(parsed, dict):
        raise ProtocolError("frame is not a JSON object")

    message_type = parsed.get("type")
    if not isinstance(message_type, str):
        raise ProtocolError("frame has no string 'type'")
    if message_type not in CLIENT_MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type: {message_type}")

    if message_type == "resume" and not isinstance(parsed.get("token"), str):
        raise ProtocolError("resume without a token")
    if message_type == "cancel_operation" and not isinstance(parsed.get("operation_id"), str):
        raise ProtocolError("cancel_operation without an operation_id")

    return ClientMessage(type=message_type, payload=parsed)


def _server_message(message_type: str, **fields: Any) -> str:
    if message_type not in SERVER_MESSAGE_TYPES:  # pragma: no cover - guards a coding mistake
        raise ValueError(f"{message_type} is not a type the client can parse")
    body: dict[str, Any] = {"type": message_type}
    body.update({key: value for key, value in fields.items() if value is not None})
    return json.dumps(body, separators=(",", ":"))


#: The header a client presents a resume grant in. A header rather than a query parameter
#: because a URL reaches server logs, proxy logs and crash reports -- the same reason
#: WebSocketRealtimeTransport keeps the access token out of the query string -- and because it
#: has to be readable *before* the socket is accepted, which a protocol message cannot be.
RESUME_HEADER: Final = "x-voice-resume"


def session_accepted(
    session_id: str, resume_token: str | None = None, *, resumed: bool = False
) -> str:
    """Accept a session, saying honestly whether it continued an earlier one.

    `resumed` is additive and defaults to false, so a client that does not read it behaves as
    before. It exists because the alternative -- letting the client infer continuity from the
    presence of a token it just sent -- is what allowed a reconnect to report success while
    starting a new conversation.
    """
    return _server_message(
        "session_accepted", session_id=session_id, resume_token=resume_token, resumed=resumed
    )


def session_expired() -> str:
    return _server_message("session_expired")


def error(message: str) -> str:
    """A structured error.

    The client maps this to a **non-retryable** failure, so it is reserved for conditions a
    reconnect cannot fix. Anything transient must close the socket with a retryable code
    instead of coming through here.
    """
    return _server_message("error", message=message)


# ------------------------------------------------------- the conversational turn
#
# These were declared in `SERVER_MESSAGE_TYPES` from VA-2 and built on the client from VA-2, and
# until now nothing on the server could produce them -- VA-12 found the gateway's only send sites
# were `session_accepted` and `session_expired`. The field names below are not a new design:
# they are what `WebSocketRealtimeTransport.decode` already requires, and
# `tests/unit/test_realtime_protocol_parity.py` is what keeps the two from drifting.
#
# `partial_transcript` deliberately has **no builder**. The only approved recogniser is
# turn-based faster-whisper, so a partial would have to be manufactured by splitting a final
# transcript -- which the master prompt forbids and which would be a lie about what the device
# heard. When a streaming recogniser is approved, this is the place to add it.


def final_transcript(turn_id: str, text: str) -> str:
    """The transcript of one committed turn, after recognition actually ran."""
    return _server_message("final_transcript", turn_id=turn_id, text=text)


def assistant_message(turn_id: str, text: str, *, spoken: bool) -> str:
    """The authoritative textual answer for a turn.

    **Why this type had to be added.** Before it, the agreed vocabulary had no way to send the
    assistant's words: `final_transcript` is the *student's* speech, and the reply existed only
    as PCM in the audio frames. That made the specification's TTS rule unimplementable -- "a
    failed TTS operation may still have a valid authoritative textual answer; return that answer
    with an explicit audio-unavailable condition" -- because there was nowhere to put the answer.
    The alternatives were worse: `error` is mapped to a non-retryable `ProtocolViolation` by the
    client and would kill the session over a missing voice, and overloading
    `operation_outcome.detail` would smuggle content through a status field.

    `spoken` says whether audio for this turn follows. False is the honest signal for "read
    this, there is no voice" -- never silence presented as a spoken reply.
    """
    return _server_message("assistant_message", turn_id=turn_id, text=text, spoken=spoken)


def turn_failed(turn_id: str, code: str, message: str) -> str:
    """One turn did not produce an answer. The session is still healthy.

    Also a new type, for the same reason: the only failure channel was `error`, which the client
    treats as fatal. A recogniser that heard nothing is not a reason to drop a conversation, so
    expressing it as `error` would have turned every mumble into a reconnect.

    `code` comes from one of two closed enums and nowhere else: `turn_runtime.TurnFailure` when a
    turn ran and a stage failed, or `turn_channel.TurnRejection` when a commit never started a
    turn at all. `message` comes from the fixed mapping beside each. Neither is ever a provider's
    own words, which can name a model path, a driver or a missing dependency.

    A `turn_failed` that *follows* an `assistant_message` for the same turn id reports a failed
    stage, not a lost answer: the reply above it stands, and the code says which part did not
    happen. Both frames are true at once.
    """
    return _server_message("turn_failed", turn_id=turn_id, code=code, message=message)


def speech_started(turn_id: str) -> str:
    """Assistant speech for `turn_id` is about to be streamed as audio frames."""
    return _server_message("speech_started", turn_id=turn_id)


def speech_stopped(turn_id: str) -> str:
    """No further audio frames belong to `turn_id` -- the natural end, or a cancellation."""
    return _server_message("speech_stopped", turn_id=turn_id)


def thinking() -> str:
    """Work has begun on the committed turn. Carries nothing, promises nothing."""
    return _server_message("thinking")


def tool_call_started(operation_id: str, name: str) -> str:
    """A registered capability is running. `name` is a catalogue name, never free text."""
    return _server_message("tool_call_started", operation_id=operation_id, name=name)


def generation_started(generation_id: str) -> str:
    return _server_message("generation_started", generation_id=generation_id)


def generation_done(generation_id: str) -> str:
    return _server_message("generation_done", generation_id=generation_id)


def generation_cancelled(generation_id: str) -> str:
    return _server_message("generation_cancelled", generation_id=generation_id)


def operation_outcome(operation_id: str, *, succeeded: bool, detail: str | None = None) -> str:
    """The result of a mutation.

    `succeeded` is always sent explicitly, including when false. The client reads an absent
    field as "did not succeed", which is the right default, but relying on absence to mean
    failure would make a dropped field look like a refusal -- and the same shape elsewhere look
    like success. Say it.
    """
    return _server_message(
        "operation_outcome", operation_id=operation_id, succeeded=succeeded, detail=detail
    )


def encode_audio(generation_id: str, pcm: bytes) -> bytes:
    """Build a server audio frame.

    The generation id travels with every chunk so the client can drop audio that belongs to a
    generation it has already interrupted, rather than playing it over the next one.
    """
    identifier = generation_id.encode("utf-8")
    if len(identifier) > _MAX_GENERATION_ID_BYTES:
        raise ValueError("generation id is too long for the frame header")
    return bytes([BINARY_FRAME_VERSION, len(identifier)]) + identifier + pcm


def decode_audio(frame: bytes) -> tuple[str, bytes]:
    """Parse a server-shaped audio frame. Used by the parity test and any future echo path."""
    if len(frame) < _HEADER_BYTES:
        raise ProtocolError("binary frame is too short to hold a header")
    if frame[0] != BINARY_FRAME_VERSION:
        raise ProtocolError(f"unsupported binary frame version {frame[0]}")
    id_length = frame[1]
    pcm_start = _HEADER_BYTES + id_length
    if len(frame) < pcm_start:
        raise ProtocolError(f"binary frame header claims {id_length} id bytes")
    return frame[_HEADER_BYTES:pcm_start].decode("utf-8"), frame[pcm_start:]


def validate_audio_format(sample_rate_hz: int, channel_count: int) -> None:
    """Check the format the client negotiated on the handshake.

    Bounds match the client's own `VoiceAudioConfig`, so a configuration the client would have
    refused locally is refused here too rather than becoming a stream nothing can interpret.
    """
    if not MIN_SAMPLE_RATE_HZ <= sample_rate_hz <= MAX_SAMPLE_RATE_HZ:
        raise ProtocolError(
            f"sample rate {sample_rate_hz} is outside "
            f"{MIN_SAMPLE_RATE_HZ}-{MAX_SAMPLE_RATE_HZ} Hz"
        )
    if not 1 <= channel_count <= MAX_CHANNELS:
        raise ProtocolError(f"channel count {channel_count} is not 1 or {MAX_CHANNELS}")


def validate_audio_frame(frame: bytes, *, channel_count: int) -> None:
    """Check an inbound PCM frame.

    An odd byte count means a sample was cut in half, which is a framing bug rather than quiet
    audio -- and silently accepting it shifts every subsequent sample by one byte, turning the
    rest of the stream into noise that looks like a model problem.
    """
    if not frame:
        raise ProtocolError("audio frame is empty")
    alignment = BYTES_PER_SAMPLE * channel_count
    if len(frame) % alignment:
        raise ProtocolError(
            f"audio frame of {len(frame)} bytes is not a whole number of "
            f"{channel_count}-channel PCM16 samples"
        )
