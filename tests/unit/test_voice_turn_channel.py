"""Per-session turn state, tested without a socket.

Every case here is one a real client produces: a double tap on the commit button, a barge-in
naming a generation that has already finished, a student who talks for four minutes. None of
them needs a WebSocket to reproduce, which is the reason this state lives in its own module.

**Nothing here involves speech.** No recogniser, no synthesizer, no provider. These are
contract tests for the bookkeeping that decides *which audio* a turn gets and *whether its
result is still wanted* -- the two questions that, answered wrongly, make a conversation reply
to the wrong thing.
"""

import pytest

from app.speech.contracts import AudioEncoding, AudioFormat
from app.voice.turn_channel import (
    MAX_TURN_SECONDS,
    TurnChannel,
    TurnRefused,
    TurnRejection,
)

MONO_16K = AudioFormat(encoding=AudioEncoding.PCM16, sample_rate_hz=16_000, channel_count=1)
MONO_8K = AudioFormat(encoding=AudioEncoding.PCM16, sample_rate_hz=8_000, channel_count=1)

#: A 20 ms frame of 16 kHz mono PCM16, which is what the client actually sends.
FRAME = b"\x01\x02" * 320


@pytest.fixture()
def channel():
    return TurnChannel(MONO_16K)


class TestAccumulation:
    def test_frames_accumulate_into_the_turn_being_spoken(self, channel):
        channel.append(FRAME)
        channel.append(FRAME)
        assert channel.buffered_bytes == 2 * len(FRAME)

    def test_a_committed_turn_carries_exactly_what_was_buffered(self, channel):
        channel.append(FRAME)
        channel.append(b"\xaa\xbb")
        turn = channel.commit()
        assert turn.pcm == FRAME + b"\xaa\xbb"

    def test_committing_empties_the_session_buffer(self, channel):
        channel.append(FRAME)
        channel.commit()
        assert channel.buffered_bytes == 0

    def test_audio_arriving_after_a_commit_cannot_reach_the_committed_turn(self, channel):
        """The detachment has to be a handover, not a copy.

        If the buffer were cleared rather than replaced, a frame that arrived between the copy
        and the clear would end up in neither turn -- or, worse, in both. This is the "one turn
        cannot consume another turn's audio" guarantee, and it is the whole reason `commit`
        rebinds.
        """
        channel.append(FRAME)
        turn = channel.commit()
        before = turn.pcm

        channel.append(b"\x09\x09" * 100)

        assert turn.pcm == before
        assert channel.buffered_bytes == 200

    def test_each_turn_gets_its_own_identifiers(self, channel):
        channel.append(FRAME)
        first = channel.commit()
        channel.settle(first.turn_id)
        channel.append(FRAME)
        second = channel.commit()

        assert first.turn_id != second.turn_id
        assert first.generation_id != second.generation_id
        assert (first.sequence, second.sequence) == (1, 2)

    def test_a_turn_reports_its_own_duration(self, channel):
        channel.append(b"\x00\x00" * 16_000)  # one second of 16 kHz mono
        assert channel.commit().audio_seconds == pytest.approx(1.0)


class TestBounds:
    def test_a_commit_with_no_audio_is_refused(self, channel):
        with pytest.raises(TurnRefused) as caught:
            channel.commit()
        assert caught.value.rejection is TurnRejection.NO_AUDIO

    def test_a_buffer_past_the_byte_cap_is_refused(self):
        tiny = TurnChannel(MONO_16K, max_turn_audio_bytes=1_000)
        for _ in range(10):
            tiny.append(FRAME)  # 640 bytes each
        with pytest.raises(TurnRefused) as caught:
            tiny.commit()
        assert caught.value.rejection is TurnRejection.TURN_TOO_LARGE

    def test_a_buffer_past_the_duration_cap_is_refused_separately(self):
        """The two caps bound genuinely different things once the format varies.

        At 8 kHz mono, two seconds is 32 000 bytes -- well inside a byte cap of 10 MB -- so only
        a duration check can catch it. Reporting it as "too large" would send a student looking
        for a shorter *answer* when what they need is a shorter *recording*.
        """
        brief = TurnChannel(MONO_8K, max_turn_audio_bytes=10 * 1024 * 1024, max_turn_seconds=1.0)
        brief.append(b"\x00\x00" * 8_000)  # exactly one second
        brief.append(b"\x00\x00" * 8_000)  # past it
        with pytest.raises(TurnRefused) as caught:
            brief.commit()
        assert caught.value.rejection is TurnRejection.TURN_TOO_LONG

    def test_the_excess_is_dropped_rather_than_stored(self):
        """A cap that still paid for the memory would not be a cap."""
        tiny = TurnChannel(MONO_16K, max_turn_audio_bytes=1_000)
        tiny.append(FRAME)
        for _ in range(1_000):
            tiny.append(FRAME)
        assert tiny.buffered_bytes == len(FRAME)

    def test_a_refusal_leaves_the_next_turn_clean(self):
        tiny = TurnChannel(MONO_16K, max_turn_audio_bytes=1_000)
        for _ in range(10):
            tiny.append(FRAME)
        with pytest.raises(TurnRefused):
            tiny.commit()

        tiny.append(FRAME)
        assert tiny.commit().pcm == FRAME

    def test_the_default_duration_cap_is_the_documented_one(self):
        # Guards the constant against a silent edit: three minutes is the figure the gateway's
        # docstring and the acceptance matrix both quote.
        assert MAX_TURN_SECONDS == 180.0


class TestOneTurnAtATime:
    def test_a_second_commit_while_a_turn_runs_is_refused(self, channel):
        channel.append(FRAME)
        channel.commit()
        channel.append(FRAME)

        with pytest.raises(TurnRefused) as caught:
            channel.commit()
        assert caught.value.rejection is TurnRejection.TURN_IN_FLIGHT

    def test_a_refused_second_commit_does_not_cost_the_student_their_audio(self, channel):
        channel.append(FRAME)
        channel.commit()
        channel.append(b"\x07\x07" * 50)

        with pytest.raises(TurnRefused):
            channel.commit()

        assert channel.buffered_bytes == 100

    def test_a_double_tap_reports_the_running_turn_rather_than_the_empty_buffer(self, channel):
        """Both facts are true; only one of them tells the client what to do.

        A second `commit_turn` with nothing newly spoken is simultaneously "no audio" and
        "already working". `no_audio` would send the client looking at the microphone, which is
        fine; the microphone is not the problem.
        """
        channel.append(FRAME)
        channel.commit()

        with pytest.raises(TurnRefused) as caught:
            channel.commit()
        assert caught.value.rejection is TurnRejection.TURN_IN_FLIGHT

    def test_a_settled_turn_frees_the_session_for_the_next_one(self, channel):
        channel.append(FRAME)
        first = channel.commit()
        channel.settle(first.turn_id)

        channel.append(FRAME)
        assert channel.commit().turn_id != first.turn_id


class TestLiveness:
    def test_the_committed_turn_is_live(self, channel):
        channel.append(FRAME)
        turn = channel.commit()
        assert channel.is_live(turn.turn_id)

    def test_an_id_this_channel_never_issued_is_not_live(self, channel):
        channel.append(FRAME)
        channel.commit()
        assert not channel.is_live("t-somebody-elses")

    def test_a_settled_turn_is_no_longer_live(self, channel):
        channel.append(FRAME)
        turn = channel.commit()
        channel.settle(turn.turn_id)
        assert not channel.is_live(turn.turn_id)

    def test_settling_a_stale_id_does_not_retire_the_running_turn(self, channel):
        """A worker whose turn was interrupted still runs to completion and still settles.

        If that late settle could clear whichever turn happens to be current, the turn after an
        interruption would be silently retired and its result dropped.
        """
        channel.append(FRAME)
        first = channel.commit()
        channel.interrupt()
        channel.append(FRAME)
        second = channel.commit()

        channel.settle(first.turn_id)

        assert channel.is_live(second.turn_id)

    def test_settling_twice_is_harmless(self, channel):
        channel.append(FRAME)
        turn = channel.commit()
        channel.settle(turn.turn_id)
        channel.settle(turn.turn_id)
        assert not channel.is_live(turn.turn_id)


class TestInterruption:
    def test_an_interrupt_cancels_the_live_turn(self, channel):
        channel.append(FRAME)
        turn = channel.commit()

        abandoned = channel.interrupt()

        # Returning the turn is how the caller knows an interrupt landed on something: the
        # gateway sends `generation_cancelled` only when it gets a turn back, so an interrupt
        # for nothing produces no frame at all.
        assert abandoned is turn
        assert turn.cancellation.cancelled
        assert not channel.is_live(turn.turn_id)

    def test_a_matching_generation_id_cancels(self, channel):
        channel.append(FRAME)
        turn = channel.commit()
        assert channel.interrupt(turn.generation_id) is turn

    def test_a_stale_generation_id_cancels_nothing(self, channel):
        """Barge-in on the wrong generation is how a conversation eats its own next reply.

        The client announces which generation it is interrupting. If that announcement arrives
        after the generation finished and a new turn started, acting on it would cancel the new
        turn -- the student interrupts reply A and loses reply B.
        """
        channel.append(FRAME)
        turn = channel.commit()

        assert channel.interrupt("g-a-generation-that-already-finished") is None
        assert not turn.cancellation.cancelled
        assert channel.is_live(turn.turn_id)

    def test_an_interrupt_with_nothing_running_is_not_an_error(self, channel):
        assert channel.interrupt() is None
        assert channel.interrupt("g-1") is None

    def test_an_interrupt_frees_the_session_immediately(self, channel):
        """The abandoned worker may be stuck in a recogniser for another minute. Making the
        student wait for it before they can speak again would make barge-in useless."""
        channel.append(FRAME)
        first = channel.commit()
        assert channel.in_flight_turn_id == first.turn_id
        channel.interrupt()

        channel.append(FRAME)
        second = channel.commit()
        assert second.turn_id != first.turn_id

    def test_the_channel_keeps_no_history_of_interrupted_turns(self, channel):
        """A barge-in loop must not accumulate state.

        An earlier version of this module tracked abandoned turn ids in a set so a worker could
        ask whether it had been interrupted. Nothing needed the answer -- `is_live` already says
        it -- and the gateway's abandoned path returned without clearing the entry, so the set
        grew by one id per interruption: unbounded growth a client could drive by committing and
        interrupting in a loop. The state was removed rather than patched.
        """
        for _ in range(100):
            channel.append(FRAME)
            turn = channel.commit()
            channel.interrupt()
            channel.settle(turn.turn_id)

        # Nothing left but the counter, which is a single int by design.
        collections = [v for v in vars(channel).values() if isinstance(v, (set, dict, list))]
        assert collections == [], f"the channel is retaining per-turn state: {collections}"
        assert channel.buffered_bytes == 0
        assert channel.in_flight_turn_id is None
