"""Cross-repository parity: the backend protocol against the Android client's actual codec.

A backend test that only checks the backend proves the two halves are each self-consistent,
which is exactly what two diverging implementations also are. So this reads the real Kotlin
source and fails when the contracts disagree.

It is not a substitute for a live client-to-server run -- that needs a device and is recorded
as NEEDS TEST -- but it catches the failure mode that would otherwise be found there: a type
string renamed on one side, a field spelled differently, a changed binary header.

Skipped when the Android repository is not checked out beside this one, which is the normal
case in CI for a backend-only checkout. A skip here means less evidence, not more.
"""

import json
import re
from pathlib import Path

import pytest

from app.voice import realtime_protocol as protocol

_ANDROID_ROOT = Path(__file__).resolve().parents[2].parent / "ai-placement-android"
_CODEC = (
    _ANDROID_ROOT
    / "app/src/main/java/com/aiplacementos/app/voice/realtime/WebSocketRealtimeTransport.kt"
)
_CODEC_TEST = (
    _ANDROID_ROOT
    / "app/src/test/java/com/aiplacementos/app/voice/realtime/WebSocketRealtimeTransportTest.kt"
)

pytestmark = pytest.mark.skipif(
    not _CODEC.is_file(),
    reason=f"Android client codec not found at {_CODEC}; parity cannot be checked",
)


@pytest.fixture(scope="module")
def codec_source() -> str:
    return _CODEC.read_text(encoding="utf-8")


def test_the_client_sends_exactly_the_types_the_server_accepts(codec_source):
    """Every type the Kotlin encoder can produce, and no others."""
    sent = set(re.findall(r'put\("type",\s*"([a-z_]+)"\)', codec_source))
    sent |= set(re.findall(r'obj\("([a-z_]+)"\)', codec_source))

    assert sent, "found no client message types in the Kotlin codec -- the regex is stale"
    assert sent == protocol.CLIENT_MESSAGE_TYPES, (
        "the client sends types the server does not accept, or the server accepts types the "
        f"client never sends. client={sorted(sent)} server={sorted(protocol.CLIENT_MESSAGE_TYPES)}"
    )


def test_the_client_parses_exactly_the_types_the_server_may_emit(codec_source):
    """Every type the Kotlin decoder branches on.

    This set matters more than it looks: the client turns an unrecognised type into a
    *non-retryable* protocol violation and fails the session, so a server emitting one extra
    type does not degrade gracefully -- it ends the conversation.
    """
    decode_body = codec_source.split("fun decode(", 1)[1]
    parsed = set(re.findall(r'^\s{12}"([a-z_]+)"\s*->', decode_body, re.MULTILINE))

    assert parsed, "found no server message types in the Kotlin decoder -- the regex is stale"
    assert parsed == protocol.SERVER_MESSAGE_TYPES, (
        f"client parses {sorted(parsed)}; server declares "
        f"{sorted(protocol.SERVER_MESSAGE_TYPES)}"
    )


def test_everything_the_server_actually_emits_is_parseable_by_the_client():
    assert protocol.SUPPORTED_SERVER_MESSAGE_TYPES <= protocol.SERVER_MESSAGE_TYPES


@pytest.mark.parametrize(
    "field", ["session_id", "resume_token", "message", "turn_id", "text", "code", "generation_id"]
)
def test_the_client_reads_the_field_names_the_server_writes(codec_source, field):
    assert f'root.str("{field}")' in codec_source, (
        f"the server emits '{field}' but the client never reads it"
    )


def test_the_client_reads_the_non_string_fields_too(codec_source):
    """`spoken` is a boolean, so it is not read through `root.str`.

    Checked separately rather than left out, because it is the field that decides whether the UI
    waits for audio -- and the client must default it to false, since an absent `spoken` has to
    mean "no voice is coming" rather than "wait indefinitely".
    """
    assert 'root["spoken"]?.jsonPrimitive?.booleanOrNull ?: false' in codec_source


@pytest.fixture(scope="module")
def required_fields(codec_source) -> dict:
    """What the client *refuses a frame for*, read out of its own violation messages.

    The decoder turns a missing required field into a non-retryable `ProtocolViolation` that
    fails the whole session. So this is not a style check: a builder that omitted one of these
    would end a conversation, and it would do it only on a real device.
    """
    found: dict = {}
    for message_type, field in re.findall(
        r'violation\("([a-z_]+) without ([a-z_]+)"\)', codec_source
    ):
        found.setdefault(message_type, set()).add(field)
    assert found, "found no violation messages in the Kotlin decoder -- the regex is stale"
    return found


def test_every_frame_the_server_emits_carries_the_fields_the_client_demands(required_fields):
    """Builds one real frame of every emitted type and checks it against the client's own rules.

    The sample arguments below are the only hand-written part; the *requirements* come from the
    Kotlin source, so adding a required field on the client without adding it here fails the
    test rather than the call.
    """
    samples = {
        "session_accepted": protocol.session_accepted("s-1", "r-1"),
        "session_expired": protocol.session_expired(),
        "error": protocol.error("unrecoverable"),
        "thinking": protocol.thinking(),
        "final_transcript": protocol.final_transcript("t-1", "what I said"),
        "assistant_message": protocol.assistant_message("t-1", "what it said", spoken=True),
        "turn_failed": protocol.turn_failed("t-1", "nothing_recognised", "I did not hear anything."),
        "speech_started": protocol.speech_started("t-1"),
        "speech_stopped": protocol.speech_stopped("t-1"),
        "generation_started": protocol.generation_started("g-1"),
        "generation_done": protocol.generation_done("g-1"),
        "generation_cancelled": protocol.generation_cancelled("g-1"),
    }
    assert set(samples) == protocol.SUPPORTED_SERVER_MESSAGE_TYPES, (
        "a type the gateway may emit has no sample here, so its shape is unchecked"
    )

    for message_type, frame in samples.items():
        body = json.loads(frame)
        assert body["type"] == message_type
        for field in required_fields.get(message_type, set()):
            assert field in body, f"{message_type} omits '{field}', which the client refuses"


def test_the_client_can_still_parse_the_types_the_gateway_refuses_to_send(required_fields):
    """The three forbidden types stay in the vocabulary, and that is deliberate.

    `partial_transcript` waits for a streaming recogniser, `tool_call_started` for a responder
    that reports a tool before it finishes, and `operation_outcome` for a confirmable action on
    this path. Removing their decoder branches would make adding any of them a breaking client
    change; leaving them means the server's restraint is the only thing stopping them, which is
    what `FORBIDDEN_SERVER_MESSAGE_TYPES` is for.
    """
    assert protocol.FORBIDDEN_SERVER_MESSAGE_TYPES == {
        "partial_transcript",
        "tool_call_started",
        "operation_outcome",
    }
    assert protocol.FORBIDDEN_SERVER_MESSAGE_TYPES < protocol.SERVER_MESSAGE_TYPES


def test_the_client_negotiates_the_handshake_parameters_the_gateway_reads(codec_source):
    """The query string, checked against the route signature rather than against a comment.

    The gateway decides the mode and verifies interview ownership *during the upgrade*, so these
    cannot be messages sent after the socket opens -- a mismatch here is a session that is
    refused with a non-retryable 1008 on a real device and nowhere else.
    """
    import inspect

    from app.api import voice_session as endpoint

    sent = set(re.findall(r'addQueryParameter\("([a-z_]+)"', codec_source))
    accepted = set(inspect.signature(endpoint.voice_session).parameters)

    assert sent, "found no query parameters in the Kotlin transport -- the regex is stale"
    assert sent <= accepted, (
        f"the client sends handshake parameters the gateway does not read: {sorted(sent - accepted)}"
    )
    # The three that carry meaning rather than defaults. `mode` in particular: without it every
    # session would silently open against the coach, including an interview.
    for required in ("mode", "interview_id", "interview_question_id"):
        assert required in sent, f"the client never sends '{required}'"


def test_the_client_and_the_gateway_agree_on_the_mode_vocabulary():
    """Two closed enums, one spelling. A third value on either side is a refused session."""
    kotlin = (
        _ANDROID_ROOT
        / "app/src/main/java/com/aiplacementos/app/voice/realtime/RealtimeTransport.kt"
    )
    if not kotlin.is_file():
        pytest.skip(f"client transport not found at {kotlin}")

    from app.voice.turn_runtime import VoiceMode

    client_modes = set(re.findall(r'^\s{4}[A-Z_]+\("([a-z]+)"\),', kotlin.read_text(encoding="utf-8"), re.MULTILINE))
    assert client_modes == {mode.value for mode in VoiceMode}, (
        f"client offers {sorted(client_modes)}; server accepts {sorted(m.value for m in VoiceMode)}"
    )


def test_the_binary_frame_version_matches(codec_source):
    match = re.search(r"BINARY_VERSION:\s*Byte\s*=\s*(\d+)", codec_source)
    assert match, "could not find BINARY_VERSION in the Kotlin codec"
    assert int(match.group(1)) == protocol.BINARY_FRAME_VERSION


def test_the_server_can_parse_the_exact_frames_the_client_test_asserts():
    """The strongest check available without a device.

    `WebSocketRealtimeTransportTest.kt` pins the client's encoded output as literal strings. If
    the server can parse each of those literals, the two sides agree on real bytes rather than
    on two descriptions of them.
    """
    if not _CODEC_TEST.is_file():
        pytest.skip(f"Android codec test not found at {_CODEC_TEST}")

    literals = re.findall(r'"""(\{"type".*?\})"""', _CODEC_TEST.read_text(encoding="utf-8"))
    assert literals, "found no pinned wire shapes in the client's codec test"

    # The file pins both directions. Each is checked against the half of the contract that
    # actually has to understand it; feeding a server frame to the client decoder would only
    # prove the decoder rejects it.
    outbound = [raw for raw in literals if json.loads(raw)["type"] in protocol.CLIENT_MESSAGE_TYPES]
    inbound = [raw for raw in literals if json.loads(raw)["type"] in protocol.SERVER_MESSAGE_TYPES]
    assert len(outbound) >= 4, f"expected the client's pinned outbound shapes, found {outbound}"

    for raw in outbound:
        assert protocol.decode_client_message(raw).type == json.loads(raw)["type"]

    for raw in inbound:
        # The reverse direction: a frame the client expects to receive must be one this server
        # is allowed to emit, or the two disagree about who says what.
        assert json.loads(raw)["type"] in protocol.SERVER_MESSAGE_TYPES

    assert outbound or inbound, f"no literal matched either direction: {literals}"


def test_the_audio_frame_layout_matches_the_client_encoder(codec_source):
    """`encodeAudio` writes version, id length, id, then PCM. So does the server."""
    encode_body = codec_source.split("fun encodeAudio(", 1)[1].split("private fun obj", 1)[0]
    assert "out[0] = BINARY_VERSION" in encode_body
    assert "out[1] = id.size.toByte()" in encode_body
    assert "id.copyInto(out, 2)" in encode_body
    assert "pcm.copyInto(out, 2 + id.size)" in encode_body

    frame = protocol.encode_audio("gen", b"\x10\x20")
    assert frame == bytes([protocol.BINARY_FRAME_VERSION, 3]) + b"gen" + b"\x10\x20"


def test_the_sample_rate_bounds_match_the_client_config():
    config = (
        _ANDROID_ROOT
        / "app/src/main/java/com/aiplacementos/app/voice/core/VoiceAudioConfig.kt"
    )
    if not config.is_file():
        pytest.skip(f"client audio config not found at {config}")
    source = config.read_text(encoding="utf-8")

    for name, expected in (
        ("DEFAULT_SAMPLE_RATE_HZ", protocol.DEFAULT_SAMPLE_RATE_HZ),
        ("MIN_SAMPLE_RATE_HZ", protocol.MIN_SAMPLE_RATE_HZ),
        ("MAX_SAMPLE_RATE_HZ", protocol.MAX_SAMPLE_RATE_HZ),
    ):
        match = re.search(rf"const val {name}\s*=\s*([\d_]+)", source)
        assert match, f"could not find {name} in VoiceAudioConfig.kt"
        assert int(match.group(1).replace("_", "")) == expected, (
            f"{name} differs: client={match.group(1)} server={expected}"
        )
