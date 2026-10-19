"""Explicit, bounded audio conversion.

Why this module exists at all: the VA-2 report recorded that the Android-side resampler is
linear interpolation with a box pre-filter -- fine for playing speech back to a human, **not**
fine for feeding a recogniser, because the aliasing it leaves behind lands in exactly the band
a recogniser's features come from. VA-3 may not install dependencies, so the question was
whether a correct resampler already exists in the environment. It does: `av` (PyAV 18.1.0) is
already installed as a faster-whisper dependency, and it wraps FFmpeg's `swresample`. That is
a real polyphase resampler, already present, with no new dependency and no download.

Three rules hold throughout:

* **Nothing converts implicitly.** Every function takes the source format as an argument. A
  default rate here would be the same bug the contracts module refuses.
* **Everything is bounded.** Conversions allocate memory proportional to their input, and the
  input arrives from a network request, so each entry point takes a ceiling and refuses rather
  than truncates. A truncated answer is a wrong transcript that looks like a correct one.
* **No audio and no transcript is ever logged.** Byte counts and durations only.
"""

from __future__ import annotations

import io
import wave
from typing import Optional

import numpy as np

from app.speech.contracts import (
    AudioEncoding,
    AudioFormat,
    MalformedProviderOutput,
    UnsupportedAudioFormat,
)

#: What a recogniser wants, and what faster-whisper requires when handed an array directly:
#: mono float32 in -1.0..1.0 at 16 kHz.
RECOGNISER_SAMPLE_RATE_HZ = 16_000

_INT16_FULL_SCALE = 32768.0

#: A canonical WAV header is 44 bytes, so anything at or below it is a header with no audio.
#: Same constant and same reason as `_MIN_WAV_BYTES` in app/ai/ollama_provider.py, where a TTS
#: driver that "succeeded" while writing nothing was caught by it.
_MIN_WAV_BYTES = 45


class AudioConversionError(MalformedProviderOutput):
    """Input that cannot be interpreted as the audio it claims to be."""

    def __init__(self, reason: str) -> None:
        super().__init__("audio", reason)


def _require_pcm16(fmt: AudioFormat) -> None:
    if fmt.encoding is not AudioEncoding.PCM16:
        raise UnsupportedAudioFormat("audio", fmt, "expected raw PCM16")


def pcm16_duration_seconds(byte_count: int, fmt: AudioFormat) -> float:
    """Duration of raw PCM. Separate from `AudioFormat.duration_seconds` only as a named
    entry point for callers that have a byte count but no buffer yet."""
    _require_pcm16(fmt)
    return fmt.duration_seconds(byte_count)


def pcm16_to_float32(audio: bytes, fmt: AudioFormat) -> np.ndarray:
    """Interleaved PCM16 bytes to a float32 array of shape (frames, channels).

    Scaling is by 32768, not 32767: int16 runs from -32768 to 32767, so dividing by 32767
    lets a full-scale negative sample exceed -1.0. Most consumers clamp it and nobody notices,
    which is why it survives in a lot of audio code.
    """
    _require_pcm16(fmt)
    if not audio:
        raise AudioConversionError("no audio bytes were supplied")
    if len(audio) % fmt.bytes_per_frame:
        raise AudioConversionError(
            f"{len(audio)} bytes is not a whole number of {fmt.channel_count}-channel PCM16 samples; "
            "a half sample shifts every sample after it"
        )
    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / _INT16_FULL_SCALE
    return samples.reshape(-1, fmt.channel_count)


def to_recogniser_input(
    audio: bytes,
    fmt: AudioFormat,
    *,
    max_seconds: float,
    target_rate_hz: int = RECOGNISER_SAMPLE_RATE_HZ,
) -> np.ndarray:
    """Raw PCM16 in any supported rate/channel count to mono float32 at `target_rate_hz`.

    The one conversion path a recogniser adapter should use. It refuses audio longer than
    `max_seconds` *before* allocating anything, because the ceiling exists to bound memory and
    a check after the allocation would not.

    Resampling goes through FFmpeg's swresample via PyAV. Downmixing many channels to one is
    left to it as well rather than averaging here: channel layouts are not all equal-weight,
    and FFmpeg knows the matrices.
    """
    _require_pcm16(fmt)
    seconds = pcm16_duration_seconds(len(audio), fmt)
    if seconds > max_seconds:
        raise UnsupportedAudioFormat("audio", fmt, f"{seconds:.1f}s exceeds the {max_seconds:.0f}s limit")

    if fmt.sample_rate_hz == target_rate_hz and fmt.channel_count == 1:
        # Nothing to do. Worth the branch: this is the realtime path's own format, so the
        # common case costs one conversion rather than a resampler round trip.
        return pcm16_to_float32(audio, fmt).reshape(-1)

    return _resample(audio, fmt, target_rate_hz)


def _resample(audio: bytes, fmt: AudioFormat, target_rate_hz: int) -> np.ndarray:
    import av
    from av.audio.resampler import AudioResampler

    frames = len(audio) // fmt.bytes_per_frame
    if frames == 0:
        raise AudioConversionError("no whole PCM16 samples were supplied")

    layout = _layout_for(fmt.channel_count)
    source = av.AudioFrame(format="s16", layout=layout, samples=frames)
    source.sample_rate = fmt.sample_rate_hz
    # One plane: s16 is a packed (interleaved) format in FFmpeg, so all channels live in
    # plane 0. Writing per-channel planes here would silently produce garbage for stereo.
    source.planes[0].update(audio)

    resampler = AudioResampler(format="flt", layout="mono", rate=target_rate_hz)
    chunks = [frame.to_ndarray().reshape(-1) for frame in resampler.resample(source)]
    # swresample buffers: a filter has state, and the tail of the signal is still inside it
    # until flushed. Without this the last few milliseconds of every utterance are lost --
    # which is the end of the final word.
    chunks.extend(frame.to_ndarray().reshape(-1) for frame in resampler.resample(None))
    if not chunks:
        raise AudioConversionError("the resampler produced no output")
    return np.concatenate(chunks).astype(np.float32, copy=False)


def _layout_for(channel_count: int) -> str:
    if channel_count == 1:
        return "mono"
    if channel_count == 2:
        return "stereo"
    raise UnsupportedAudioFormat(
        "audio",
        AudioFormat(AudioEncoding.PCM16, RECOGNISER_SAMPLE_RATE_HZ, channel_count),
        "only mono and stereo layouts are named",
    )


def decode_container(audio: bytes, *, max_seconds: float, target_rate_hz: int = RECOGNISER_SAMPLE_RATE_HZ) -> np.ndarray:
    """Decode a container (M4A/AAC, WAV, anything FFmpeg reads) to mono float32.

    This is the path the interview upload contract needs: Android records
    `MPEG_4` + `AAC`, so the bytes are an MP4 container and their sample rate is whatever the
    device chose. Decoding tells us the rate; assuming it would be a guess.

    The duration ceiling is enforced from the container's own metadata before decoding where
    the metadata exists, and again on the decoded length -- metadata can lie, and a file
    claiming three seconds while holding thirty is a cheap way to make a server do thirty
    seconds of work.
    """
    import av
    from av.audio.resampler import AudioResampler

    try:
        container = av.open(io.BytesIO(audio))
    except Exception as exc:  # PyAV raises several unrelated types for unreadable input
        raise AudioConversionError(f"the audio could not be opened as a media container: {exc}") from exc

    try:
        if not container.streams.audio:
            raise AudioConversionError("the container holds no audio stream")
        stream = container.streams.audio[0]

        declared = container.duration
        if declared is not None and declared / av.time_base > max_seconds:
            raise UnsupportedAudioFormat(
                AudioEncoding.M4A.value,
                AudioFormat(AudioEncoding.PCM16, target_rate_hz, 1),
                f"the container declares {declared / av.time_base:.1f}s, over the {max_seconds:.0f}s limit",
            )

        resampler = AudioResampler(format="flt", layout="mono", rate=target_rate_hz)
        max_samples = int(max_seconds * target_rate_hz)
        chunks: list[np.ndarray] = []
        total = 0
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                block = resampled.to_ndarray().reshape(-1)
                total += block.shape[0]
                if total > max_samples:
                    raise UnsupportedAudioFormat(
                        AudioEncoding.M4A.value,
                        AudioFormat(AudioEncoding.PCM16, target_rate_hz, 1),
                        f"the decoded audio exceeds the {max_seconds:.0f}s limit",
                    )
                chunks.append(block)
        for resampled in resampler.resample(None):
            chunks.append(resampled.to_ndarray().reshape(-1))
    finally:
        container.close()

    if not chunks:
        raise AudioConversionError("the container decoded to no audio")
    return np.concatenate(chunks).astype(np.float32, copy=False)


def read_wav(audio: bytes) -> tuple[np.ndarray, AudioFormat]:
    """Parse a PCM16 WAV with the standard library, returning samples and the container format.

    The returned encoding is `WAV`, not `PCM16`: these bytes are a WAV file, and a caller that
    was told "PCM16" would hand the header to something expecting raw samples. The 16-bit
    sample width is validated rather than reported, because this function only reads that one
    kind.

    Deliberately not PyAV: this is used to check what a TTS provider *produced*, and the
    standard library's `wave` module reads the header fields rather than inferring a best
    guess, so a malformed header fails here instead of being quietly repaired. A provider
    emitting a broken header is something a caller needs told about.
    """
    if len(audio) < _MIN_WAV_BYTES or audio[:4] != b"RIFF":
        raise AudioConversionError(f"not a usable WAV: {len(audio)} bytes, starts with {audio[:4]!r}")
    try:
        with wave.open(io.BytesIO(audio)) as handle:
            if handle.getsampwidth() != 2:
                raise AudioConversionError(f"expected 16-bit PCM, got {handle.getsampwidth() * 8}-bit")
            rate = handle.getframerate()
            channels = handle.getnchannels()
            pcm = handle.readframes(handle.getnframes())
    except AudioConversionError:
        raise
    except Exception as exc:
        raise AudioConversionError(f"the WAV header could not be read: {exc}") from exc
    if not pcm:
        raise AudioConversionError("the WAV holds a header but no samples")
    samples = pcm16_to_float32(pcm, AudioFormat(AudioEncoding.PCM16, rate, channels))
    return samples, AudioFormat(AudioEncoding.WAV, rate, channels)


def write_wav(samples: np.ndarray, *, sample_rate_hz: int, channel_count: int = 1) -> bytes:
    """Wrap float32 samples in a PCM16 WAV container.

    Used by tests and by any adapter whose provider hands back raw PCM while the caller's
    contract is WAV (the interview question-audio endpoint returns `audio/wav`). Clipping is
    explicit: a sample over full scale wraps around into loud noise if it is merely cast.
    """
    if samples.ndim != 1 and channel_count == 1:
        samples = samples.reshape(-1)
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * (_INT16_FULL_SCALE - 1)).astype("<i2").tobytes()
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channel_count)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        handle.writeframes(pcm)
    return buffer.getvalue()


def wav_duration_seconds(audio: bytes) -> Optional[float]:
    """Duration from a WAV header, or None if it cannot be read.

    Returns None rather than raising because this is used for reporting a synthesis result's
    length, and failing to label the duration must not fail the synthesis itself.
    """
    try:
        with wave.open(io.BytesIO(audio)) as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except Exception:
        return None
