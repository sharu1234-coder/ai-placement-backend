"""The realtime wire contract (VA-2), tested without a socket.

Keeping the protocol free of FastAPI is what makes this possible, and the exhaustive negative
cases below are the reason it is worth it: every one of them is a frame a real client could
send, and none of them needs a server to produce.
"""

import json

import pytest

from app.voice import realtime_protocol as protocol


class TestClientMessages:
    @pytest.mark.parametrize(
        "message",
        [
            {"type": "commit_turn"},
            {"type": "interrupt"},
            {"type": "interrupt", "generation_id": "g-1"},
            {"type": "resume", "token": "t-1"},
            {"type": "cancel_operation", "operation_id": "op-1"},
            {"type": "heartbeat"},
        ],
    )
    def test_every_documented_client_message_decodes(self, message):
        decoded = protocol.decode_client_message(json.dumps(message))
        assert decoded.type == message["type"]

    @pytest.mark.parametrize(
        ("raw", "because"),
        [
            ("not json", "a non-JSON frame"),
            ("[]", "a JSON array rather than an object"),
            ('"a string"', "a bare JSON string"),
            ("{}", "no type at all"),
            ('{"type": 7}', "a non-string type"),
            ('{"type": "make_payment"}', "a type outside the contract"),
            ('{"type": "resume"}', "resume without a token"),
            ('{"type": "resume", "token": 5}', "a non-string resume token"),
            ('{"type": "cancel_operation"}', "cancel without an operation id"),
        ],
    )
    def test_malformed_frames_are_rejected(self, raw, because):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode_client_message(raw)

    def test_an_unknown_type_is_never_guessed_at(self):
        # "commit" is one character from a real type. A gateway that fuzzy-matched would act on
        # a message it did not understand, which is worse than refusing it.
        with pytest.raises(protocol.ProtocolError):
            protocol.decode_client_message('{"type": "commit"}')


class TestServerMessages:
    def test_only_types_the_client_can_parse_are_emitted(self):
        assert protocol.SUPPORTED_SERVER_MESSAGE_TYPES <= protocol.SERVER_MESSAGE_TYPES

    def test_the_forbidden_set_is_derived_rather_than_written_twice(self):
        """Two hand-maintained lists drift, and the direction a mistake travels is adding a type
        to the supported set while forgetting to remove it from the forbidden one -- which would
        make the guard pass for a type the gateway is now sending."""
        assert protocol.FORBIDDEN_SERVER_MESSAGE_TYPES == (
            protocol.SERVER_MESSAGE_TYPES - protocol.SUPPORTED_SERVER_MESSAGE_TYPES
        )
        assert not protocol.FORBIDDEN_SERVER_MESSAGE_TYPES & protocol.SUPPORTED_SERVER_MESSAGE_TYPES

    def test_exactly_three_types_stay_out_of_the_gateways_reach(self):
        """Each for a reason that is a property of the implementation, not a preference.

        If any of the three becomes emittable, this test is where the justification has to be
        rewritten -- which is the point of pinning the set rather than only its size.
        """
        assert protocol.FORBIDDEN_SERVER_MESSAGE_TYPES == frozenset(
            {"partial_transcript", "tool_call_started", "operation_outcome"}
        )

    def test_session_accepted_carries_the_identifiers(self):
        body = json.loads(protocol.session_accepted("s-1", "r-1"))
        assert body == {
            "type": "session_accepted",
            "session_id": "s-1",
            "resume_token": "r-1",
            "resumed": False,
        }

    def test_session_accepted_reports_a_resume_the_server_actually_honoured(self):
        # The flag is the server's own answer, not the client's inference, so both states
        # are pinned here: a client that trusts `resumed` must never read true for a
        # session the server did not continue.
        body = json.loads(protocol.session_accepted("s-1", "r-1", resumed=True))
        assert body == {
            "type": "session_accepted",
            "session_id": "s-1",
            "resume_token": "r-1",
            "resumed": True,
        }

    def test_a_null_resume_token_is_omitted_rather_than_sent_as_null(self):
        # The client reads it as an optional string; an explicit null is a third state nobody
        # modelled, and "absent" is what it actually means.
        body = json.loads(protocol.session_accepted("s-1", None))
        assert "resume_token" not in body

    def test_session_expired_and_error_shapes(self):
        assert json.loads(protocol.session_expired()) == {"type": "session_expired"}
        assert json.loads(protocol.error("bad")) == {"type": "error", "message": "bad"}

    def test_emitting_a_type_the_client_cannot_parse_is_a_programming_error(self):
        with pytest.raises(ValueError):
            protocol._server_message("transcript_maybe")


class TestTheConversationalTurnMessages:
    """The builders for the turn events.

    The field names are not a server-side choice: `WebSocketRealtimeTransport.decode` rejects a
    `final_transcript` without `turn_id`, a `generation_started` without `generation_id` and an
    `operation_outcome` without `operation_id`, turning each into a non-retryable protocol
    violation that fails the whole session. So a missing or renamed field here is not a cosmetic
    bug -- it kills the call. These assert the exact shapes that decoder requires.
    """

    def test_a_final_transcript_carries_its_turn(self):
        body = json.loads(protocol.final_transcript("t-1", "I bisected the change."))
        assert body == {"type": "final_transcript", "turn_id": "t-1", "text": "I bisected the change."}

    def test_an_empty_transcript_is_still_a_transcript(self):
        # A recogniser that heard nothing produces "", and the client reads a missing `text` as
        # empty anyway -- but sending the field keeps "heard nothing" distinct from "field lost".
        body = json.loads(protocol.final_transcript("t-1", ""))
        assert body["type"] == "final_transcript" and body["turn_id"] == "t-1"

    def test_speech_start_and_stop_name_the_turn_they_belong_to(self):
        assert json.loads(protocol.speech_started("t-2")) == {"type": "speech_started", "turn_id": "t-2"}
        assert json.loads(protocol.speech_stopped("t-2")) == {"type": "speech_stopped", "turn_id": "t-2"}

    def test_thinking_promises_nothing(self):
        assert json.loads(protocol.thinking()) == {"type": "thinking"}

    def test_a_tool_call_names_a_catalogue_capability(self):
        body = json.loads(protocol.tool_call_started("op-1", "skills.get_passport"))
        assert body == {
            "type": "tool_call_started",
            "operation_id": "op-1",
            "name": "skills.get_passport",
        }

    def test_the_generation_lifecycle_messages_carry_their_id(self):
        for builder, expected in (
            (protocol.generation_started, "generation_started"),
            (protocol.generation_done, "generation_done"),
            (protocol.generation_cancelled, "generation_cancelled"),
        ):
            body = json.loads(builder("g-1"))
            assert body == {"type": expected, "generation_id": "g-1"}

    def test_an_outcome_states_success_explicitly_even_when_false(self):
        """The client reads an absent `succeeded` as false, which is the right default -- but a
        dropped field would then be indistinguishable from a real refusal."""
        refused = json.loads(protocol.operation_outcome("op-2", succeeded=False, detail="expired"))
        assert refused == {
            "type": "operation_outcome",
            "operation_id": "op-2",
            "succeeded": False,
            "detail": "expired",
        }
        succeeded = json.loads(protocol.operation_outcome("op-3", succeeded=True))
        assert succeeded["succeeded"] is True
        assert "detail" not in succeeded

    def test_an_assistant_message_is_the_only_channel_the_reply_text_has(self):
        """`final_transcript` is the student; this is the assistant. Two speakers, two frames.

        Without this type the reply existed only as PCM, which made "return the answer when
        synthesis fails" unimplementable -- there was nowhere to put it, and `error` is fatal on
        the client.
        """
        body = json.loads(protocol.assistant_message("t-1", "Work on DSA next.", spoken=True))
        assert body == {
            "type": "assistant_message",
            "turn_id": "t-1",
            "text": "Work on DSA next.",
            "spoken": True,
        }

    def test_spoken_false_survives_the_none_filter(self):
        """`_server_message` drops None values, and `False` must not be mistaken for one.

        A dropped `spoken` is read by the client as false, which is the right default -- but
        relying on absence would make "no voice" indistinguishable from a lost field, and the
        same shape elsewhere look like a voice that is coming.
        """
        body = json.loads(protocol.assistant_message("t-1", "A reply you will have to read.", spoken=False))
        assert body["spoken"] is False

    def test_a_turn_failure_names_a_code_and_a_safe_message(self):
        body = json.loads(
            protocol.turn_failed("t-9", "nothing_recognised", "I did not hear anything.")
        )
        assert body == {
            "type": "turn_failed",
            "turn_id": "t-9",
            "code": "nothing_recognised",
            "message": "I did not hear anything.",
        }

    def test_every_turn_failure_code_comes_from_a_closed_enum(self):
        """The codes the gateway can put on the wire, enumerated.

        Two enums rather than one, because they answer different questions: `TurnFailure` is a
        stage that failed inside a turn that ran, `TurnRejection` is a commit that never started
        one. Both are closed, which is what stops a provider's own words becoming a wire code.
        """
        from app.voice.turn_channel import TurnRejection
        from app.voice.turn_runtime import TurnFailure, user_message_for

        codes = {f.value for f in TurnFailure} | {r.value for r in TurnRejection}
        assert "no_audio" in codes and "stt_unavailable" in codes and "turn_in_flight" in codes
        for failure in TurnFailure:
            # Every code has a student-facing sentence; a KeyError here is a code that would
            # reach the gateway with nothing to say.
            assert user_message_for(failure)

    def test_there_is_no_partial_transcript_builder(self):
        """Deliberate, and the reason is worth failing a test over.

        The only approved recogniser is turn-based faster-whisper. A partial transcript could
        therefore only be produced by splitting a final one, which would be a claim about what
        the device heard mid-sentence that nothing actually observed. The type stays in the
        vocabulary for a future streaming recogniser; the builder must not exist until there is
        one.
        """
        assert not hasattr(protocol, "partial_transcript")
        # Still part of the agreed vocabulary, so the client keeps its decoder branch.
        assert "partial_transcript" in protocol.SERVER_MESSAGE_TYPES


class TestAudioFrames:
    def test_a_frame_round_trips(self):
        pcm = bytes(range(200))
        generation_id, decoded = protocol.decode_audio(protocol.encode_audio("gen-7", pcm))
        assert generation_id == "gen-7"
        assert decoded == pcm

    def test_the_header_layout_is_version_then_length(self):
        frame = protocol.encode_audio("ab", b"\x01\x02")
        assert frame[0] == protocol.BINARY_FRAME_VERSION
        assert frame[1] == 2
        assert frame[2:4] == b"ab"

    def test_a_generation_id_too_long_for_the_header_is_refused(self):
        with pytest.raises(ValueError):
            protocol.encode_audio("x" * 256, b"")

    @pytest.mark.parametrize("frame", [b"", b"\x01", b"\x09\x02ab", b"\x01\x32a"])
    def test_truncated_or_unversioned_frames_are_rejected(self, frame):
        with pytest.raises(protocol.ProtocolError):
            protocol.decode_audio(frame)


class TestAudioFormat:
    @pytest.mark.parametrize("rate", [8_000, 16_000, 44_100, 48_000])
    def test_supported_rates_are_accepted(self, rate):
        protocol.validate_audio_format(rate, 1)

    @pytest.mark.parametrize("rate", [0, -1, 7_999, 48_001, 192_000])
    def test_rates_the_client_itself_would_refuse_are_refused_here(self, rate):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_audio_format(rate, 1)

    @pytest.mark.parametrize("channels", [0, 3, -1])
    def test_impossible_channel_counts_are_refused(self, channels):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_audio_format(16_000, channels)

    def test_a_whole_number_of_samples_is_accepted(self):
        protocol.validate_audio_frame(b"\x00\x01" * 160, channel_count=1)

    def test_an_empty_audio_frame_is_refused(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_audio_frame(b"", channel_count=1)

    def test_a_half_sample_is_refused_because_it_shifts_the_whole_stream(self):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_audio_frame(b"\x00\x01\x02", channel_count=1)

    def test_stereo_alignment_counts_both_channels(self):
        protocol.validate_audio_frame(b"\x00\x01\x02\x03", channel_count=2)
        with pytest.raises(protocol.ProtocolError):
            protocol.validate_audio_frame(b"\x00\x01", channel_count=2)
