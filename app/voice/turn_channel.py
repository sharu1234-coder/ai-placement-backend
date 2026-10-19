"""Per-session turn state: what audio belongs to which turn, and which turn is live.

The gateway needs an answer to four questions that the transport layer alone cannot give, and
getting any of them wrong is a correctness bug rather than a tidiness one:

1. Which bytes belong to the turn being committed? (Not "all the audio on the socket" -- the
   next turn's frames can arrive while this one is still being recognised.)
2. Is a turn already running? (A second ``commit_turn`` must not start the same work twice, and
   must not start a second worker either.)
3. Is this result still wanted? (A transcript that arrives after an interrupt must not be sent.)
4. Has this turn outgrown its bounds? (Before recognition, not after paying for it.)

Keeping the answers here rather than in ``app/api/voice_session.py`` is what makes them testable
without a socket, and the reason the exhaustive cases in
``tests/unit/test_voice_turn_channel.py`` exist at all.

Ownership and lock boundaries
-----------------------------
One ``TurnChannel`` belongs to one connection and is **owned by that connection's receive
loop**: ``append``, ``commit``, ``settle`` and ``interrupt`` are only ever called from there, in
one coroutine, so the mutations are serialised by the event loop itself and no lock is needed
for them.

The one field read from outside that loop is the liveness of a turn, which the worker task
consults through :meth:`is_live` before it emits anything. That read is a single attribute
compare against an immutable snapshot, and the only writer is the receive loop; on a single
event loop thread there is no torn state to protect. A lock here would be decoration.

What is deliberately *not* modelled
-----------------------------------
Inbound audio frames carry **no sequence number**. That was a VA-2 decision recorded in
``realtime_protocol``: the client sends one 20 ms raw PCM frame every 20 ms and an envelope
would have inflated the most frequent message type on the wire for nothing. So out-of-order
*inbound audio* is not detectable here, and this module does not pretend to detect it -- ordering
comes from TCP and nothing else. What *is* detectable, and is enforced below, is a stale turn or
generation id on a control message, which is where a late client actually causes harm.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional

from app.speech.contracts import AudioFormat, CancellationToken
from app.voice.turn_runtime import MAX_TURN_AUDIO_BYTES, new_generation_id, new_turn_id

logger = logging.getLogger(__name__)

#: How long one turn of speech may be. Three minutes is longer than any interview answer and
#: longer than a rambling coaching question; past it, the student is not talking to us.
#:
#: Expressed in seconds as well as bytes because the two bound genuinely different things once
#: the format varies: at 48 kHz stereo the byte cap bites after about 16 seconds, while at 8 kHz
#: mono three minutes is well under it. Whichever is tighter for the negotiated format wins, and
#: the two report separately so a refusal says which limit was actually hit.
MAX_TURN_SECONDS: Final[float] = 180.0

#: Per-session cap on simultaneously running turns. One, and it is not a tunable: a second
#: concurrent turn in interview mode would race the first for the same question.
MAX_CONCURRENT_TURNS: Final[int] = 1


class TurnRejection(str, Enum):
    """Why a commit did not start a turn. Safe for the wire and for a log line."""

    #: Nothing was buffered. A commit with no audio is a client bug or a dead microphone; either
    #: way there is nothing to recognise, and inventing a transcript for it is the exact failure
    #: the specification forbids.
    NO_AUDIO = "no_audio"
    #: The buffer passed the byte cap. Refused before recognition, so an oversized turn costs a
    #: length check rather than minutes of CPU.
    TURN_TOO_LARGE = "turn_too_large"
    #: The buffer passed the duration cap for the negotiated format.
    TURN_TOO_LONG = "turn_too_long"
    #: A turn is already running on this session.
    TURN_IN_FLIGHT = "turn_in_flight"


class TurnRefused(Exception):
    """Raised by :meth:`TurnChannel.commit`. Carries a typed reason, never prose."""

    def __init__(self, rejection: TurnRejection) -> None:
        super().__init__(rejection.value)
        self.rejection = rejection


@dataclass(frozen=True)
class CommittedTurn:
    """A turn's audio, detached from the session.

    Frozen and holding its own ``bytes``: once this exists, the session buffer has been replaced
    rather than cleared, so no later frame can reach these bytes and no second commit can hand
    them out again. That is what "atomically detach the completed turn's audio buffer" means in
    practice -- the handover is a rebind, not a copy-then-truncate with a window in between.
    """

    turn_id: str
    generation_id: str
    #: Monotonic per session, from 1. Not on the wire -- the client contract has no field for it
    #: -- but it makes "which turn is this" answerable in a log without correlating ids.
    sequence: int
    pcm: bytes
    audio_format: AudioFormat
    cancellation: CancellationToken

    @property
    def audio_seconds(self) -> float:
        return _seconds_for(len(self.pcm), self.audio_format)


class TurnChannel:
    """Accumulates one turn's audio and tracks which turn is live."""

    def __init__(
        self,
        audio_format: AudioFormat,
        *,
        max_turn_audio_bytes: int = MAX_TURN_AUDIO_BYTES,
        max_turn_seconds: float = MAX_TURN_SECONDS,
    ) -> None:
        self._format = audio_format
        self._max_bytes = max_turn_audio_bytes
        self._max_seconds = max_turn_seconds
        self._duration_cap_bytes = _bytes_for(max_turn_seconds, audio_format)
        self._buffer = bytearray()
        #: Set when a frame would have pushed the buffer past a cap. The excess is dropped
        #: rather than stored -- the point of a cap is that exceeding it costs nothing -- and the
        #: flag is what turns the following commit into a typed refusal instead of recognition
        #: of a truncated recording, which would change what the student said.
        self._overflow: Optional[TurnRejection] = None
        self._in_flight: Optional[CommittedTurn] = None
        self._sequence = 0

    # ------------------------------------------------------------------ accumulation

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def in_flight_turn_id(self) -> Optional[str]:
        return self._in_flight.turn_id if self._in_flight else None

    def append(self, frame: bytes) -> None:
        """Add one validated PCM frame to the turn being spoken.

        Never raises. A frame that would breach a cap is dropped and the turn is poisoned, so an
        over-long recording ends one turn rather than the whole session -- a student who rambles
        should lose a take, not a conversation. The session-wide audio budget in the gateway is
        the separate, harsher bound that does close the socket.
        """
        if self._overflow is not None:
            return
        total = len(self._buffer) + len(frame)
        if total > self._max_bytes:
            self._overflow = TurnRejection.TURN_TOO_LARGE
            logger.info("voice turn buffer exceeded %d bytes; dropping the rest", self._max_bytes)
            return
        if total > self._duration_cap_bytes:
            self._overflow = TurnRejection.TURN_TOO_LONG
            logger.info("voice turn buffer exceeded %.0fs; dropping the rest", self._max_seconds)
            return
        self._buffer += frame

    # ------------------------------------------------------------------ commit

    def commit(self) -> CommittedTurn:
        """Detach the buffered audio as a turn ready to run.

        Raises :class:`TurnRefused` for every case where there is nothing honest to run. The
        order of the checks is the order of usefulness to the client when more than one is true
        at once -- and after a double commit, two of them are.

        * An overflowed buffer is reported first and cleared, because its contents are a
          truncated recording that must not be recognised whatever else is going on.
        * Then the in-flight turn. A second ``commit_turn`` arriving with nothing newly spoken is
          both "no audio" and "already working"; the second is what tells the client to wait
          rather than go looking at the microphone.
        * Emptiness last, which is what a genuine commit with a dead microphone reports.
        """
        if self._overflow is not None:
            rejection = self._overflow
            self._reset_buffer()
            raise TurnRefused(rejection)
        if self._in_flight is not None:
            # The buffer is deliberately left intact: the audio the student has spoken since the
            # last commit is theirs, and discarding it because they tapped twice would lose a
            # real take.
            raise TurnRefused(TurnRejection.TURN_IN_FLIGHT)
        if not self._buffer:
            raise TurnRefused(TurnRejection.NO_AUDIO)

        self._sequence += 1
        turn = CommittedTurn(
            turn_id=new_turn_id(),
            generation_id=new_generation_id(),
            sequence=self._sequence,
            # bytes(), so the turn owns an immutable copy and the bytearray below is free to be
            # replaced without the worker seeing it change underneath.
            pcm=bytes(self._buffer),
            audio_format=self._format,
            cancellation=CancellationToken(),
        )
        self._reset_buffer()
        self._in_flight = turn
        return turn

    def _reset_buffer(self) -> None:
        # A fresh object rather than `clear()`: anything still holding the old one keeps what it
        # had, which is the whole point of the handover.
        self._buffer = bytearray()
        self._overflow = None

    # ------------------------------------------------------------------ liveness

    def is_live(self, turn_id: str) -> bool:
        """Whether results for this turn are still wanted.

        Read by the worker before every emit. False after an interrupt, after the turn settles,
        and for any id this channel never issued -- so a stale result cannot be mistaken for a
        current one just because the session is still open.
        """
        return self._in_flight is not None and self._in_flight.turn_id == turn_id

    def settle(self, turn_id: str) -> None:
        """Mark a turn finished. Idempotent, and ignores an id that is not the live one.

        Ignoring a stale id is the important half: an interrupted worker still runs to completion
        and still settles, and if that late call could clear whichever turn happens to be current
        it would silently retire the turn that replaced it.
        """
        if self._in_flight is not None and self._in_flight.turn_id == turn_id:
            self._in_flight = None

    def interrupt(self, generation_id: Optional[str] = None) -> Optional[CommittedTurn]:
        """Cancel the live turn and stop wanting its results.

        ``generation_id`` is the client's statement of *which* generation it is interrupting. A
        value that does not match the live turn is ignored and ``None`` is returned: the client
        is interrupting something that has already finished, and acting on it would cancel the
        turn after it -- barge-in on the wrong generation is how a conversation starts eating its
        own replies. Omitted means "whatever is current", which is what a barge-in with no
        generation yet announced honestly means.

        Cancellation is cooperative, and that bound is real: the token stops the *next* stage and
        suppresses the emit, but it cannot interrupt a recogniser or a TTS driver already blocked
        inside a worker thread. The work may finish; its result is simply never sent.
        """
        turn = self._in_flight
        if turn is None:
            return None
        if generation_id is not None and generation_id != turn.generation_id:
            logger.info(
                "ignoring an interrupt for generation %s; the live one is %s",
                generation_id,
                turn.generation_id,
            )
            return None
        turn.cancellation.cancel()
        self._in_flight = None
        return turn


def _seconds_for(byte_count: int, audio_format: AudioFormat) -> float:
    return byte_count / float(_bytes_per_second(audio_format))


def _bytes_for(seconds: float, audio_format: AudioFormat) -> int:
    return int(seconds * _bytes_per_second(audio_format))


def _bytes_per_second(audio_format: AudioFormat) -> int:
    # PCM16: two bytes per sample per channel. The only inbound encoding on this path, and
    # `validate_audio_frame` has already refused anything that is not a whole sample.
    return audio_format.sample_rate_hz * audio_format.channel_count * 2
