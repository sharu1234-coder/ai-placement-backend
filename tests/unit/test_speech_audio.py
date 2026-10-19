"""Tier 1: audio conversion.

These matter more than their size suggests. A resampler that is merely *plausible* produces
audio a human finds intelligible and a recogniser finds ambiguous, and the resulting accuracy
loss looks like a model problem rather than a conversion problem -- which is exactly how the
VA-2 note came to record that the Android resampler must not feed a recogniser.

So the checks here are on measurable signal properties (length, amplitude, which frequency
survived) rather than on byte equality against a golden file.
"""

import io
import math
import wave

import numpy as np
import pytest

from app.speech import audio as audio_mod
from app.speech.contracts import AudioEncoding, AudioFormat, MalformedProviderOutput, UnsupportedAudioFormat

MONO_16K = AudioFormat(AudioEncoding.PCM16, 16_000, 1)
STEREO_48K = AudioFormat(AudioEncoding.PCM16, 48_000, 2)


def tone_pcm16(hz: float, seconds: float, rate: int, channels: int = 1, amplitude: float = 0.5) -> bytes:
    t = np.arange(int(seconds * rate), dtype=np.float32) / rate
    wave_data = (np.sin(2 * math.pi * hz * t) * amplitude).astype(np.float32)
    if channels > 1:
        wave_data = np.repeat(wave_data[:, None], channels, axis=1).reshape(-1)
    return (wave_data * 32767).astype("<i2").tobytes()


def dominant_frequency(samples: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.fft.rfftfreq(samples.shape[0], 1 / rate)[int(np.argmax(spectrum))])


class TestPcmToFloat:
    def test_full_scale_negative_stays_inside_minus_one(self):
        # Dividing by 32767 instead of 32768 puts -32768 at -1.00003. Most consumers clamp it
        # and nobody notices, which is why the bug is common.
        samples = audio_mod.pcm16_to_float32(b"\x00\x80", MONO_16K)
        assert samples.min() >= -1.0

    def test_shape_is_frames_by_channels(self):
        assert audio_mod.pcm16_to_float32(b"\x00\x00" * 8, STEREO_48K).shape == (4, 2)

    def test_a_half_sample_is_refused(self):
        with pytest.raises(MalformedProviderOutput) as caught:
            audio_mod.pcm16_to_float32(b"\x01\x02\x03", MONO_16K)
        assert "half sample" in str(caught.value)

    def test_empty_audio_is_refused(self):
        with pytest.raises(MalformedProviderOutput):
            audio_mod.pcm16_to_float32(b"", MONO_16K)

    def test_a_container_format_is_refused_by_the_pcm_path(self):
        with pytest.raises(UnsupportedAudioFormat):
            audio_mod.pcm16_to_float32(b"RIFF", AudioFormat(AudioEncoding.WAV, 16_000, 1))


class TestRecogniserInput:
    def test_the_realtime_format_passes_through_unresampled(self):
        pcm = tone_pcm16(440, 0.5, 16_000)
        out = audio_mod.to_recogniser_input(pcm, MONO_16K, max_seconds=10)
        assert out.ndim == 1
        assert out.shape[0] == 8_000  # exactly the input, not a resampler's approximation

    def test_downsampling_keeps_the_tone_and_the_duration(self):
        pcm = tone_pcm16(1_000, 1.0, 48_000, channels=2)
        out = audio_mod.to_recogniser_input(pcm, STEREO_48K, max_seconds=10)
        assert out.ndim == 1
        # swresample's output length varies by a few samples with filter delay; a 1% window
        # catches a wrong *rate* while tolerating that.
        assert abs(out.shape[0] - 16_000) < 160
        assert abs(dominant_frequency(out, 16_000) - 1_000) < 20

    def test_a_tone_above_the_new_nyquist_is_filtered_not_aliased(self):
        # The property that makes this suitable for a recogniser and the VA-2 resampler not:
        # 7 kHz cannot exist at 16 kHz with an 8 kHz Nyquist, so it must be attenuated. A
        # naive resampler folds it down to 1 kHz instead, putting energy where none was --
        # inside the band a recogniser's features come from.
        pcm = tone_pcm16(7_000, 0.5, 16_000)
        out = audio_mod.to_recogniser_input(pcm, MONO_16K, max_seconds=10)
        before = float(np.abs(out).max())

        pcm_high = tone_pcm16(15_000, 0.5, 44_100)
        folded = audio_mod.to_recogniser_input(pcm_high, AudioFormat(AudioEncoding.PCM16, 44_100, 1), max_seconds=10)
        # Whatever survives must be quiet; a folded alias comes through at nearly full level.
        assert float(np.abs(folded).max()) < before * 0.5

    def test_the_resampler_tail_is_flushed(self):
        # swresample holds the end of the signal in its filter state. Without a flush the last
        # milliseconds vanish -- which is the end of the final word.
        pcm = tone_pcm16(440, 0.2, 44_100)
        out = audio_mod.to_recogniser_input(pcm, AudioFormat(AudioEncoding.PCM16, 44_100, 1), max_seconds=10)
        assert out.shape[0] > 0.19 * 16_000

    def test_audio_over_the_ceiling_is_refused_before_allocation(self):
        with pytest.raises(UnsupportedAudioFormat) as caught:
            audio_mod.to_recogniser_input(tone_pcm16(440, 2.0, 16_000), MONO_16K, max_seconds=1.0)
        assert "limit" in str(caught.value)

    def test_more_than_stereo_is_refused_rather_than_guessed(self):
        fmt = AudioFormat(AudioEncoding.PCM16, 16_000, 4)
        with pytest.raises(UnsupportedAudioFormat):
            audio_mod.to_recogniser_input(b"\x00\x00" * 4 * 100, fmt, max_seconds=10)


class TestWavRoundTrip:
    def test_a_written_wav_reads_back_with_the_same_metadata(self):
        samples = np.sin(np.arange(16_000, dtype=np.float32) / 16_000 * 2 * math.pi * 440) * 0.5
        wav = audio_mod.write_wav(samples, sample_rate_hz=16_000)
        read, fmt = audio_mod.read_wav(wav)
        # WAV, not PCM16: these bytes carry a header, and a caller told "PCM16" would feed the
        # header to something expecting raw samples.
        assert fmt == AudioFormat(AudioEncoding.WAV, 16_000, 1)
        assert read.shape[0] == 16_000
        assert audio_mod.wav_duration_seconds(wav) == pytest.approx(1.0)

    def test_clipping_is_explicit_rather_than_wrapping_around(self):
        # A bare cast turns +1.5 into a large negative int16 -- a loud click. Clipping is the
        # only honest option when the caller hands over out-of-range samples.
        wav = audio_mod.write_wav(np.array([1.5, -1.5], dtype=np.float32), sample_rate_hz=16_000)
        read, _ = audio_mod.read_wav(wav)
        assert read.max() > 0.9 and read.min() < -0.9

    @pytest.mark.parametrize("payload", [b"", b"RIFF", b"RIFF" + b"\x00" * 40, b"NOTAWAV" + b"\x00" * 60])
    def test_a_header_only_or_non_wav_payload_is_refused(self, payload):
        # The observed TTS failure: a driver that "succeeds" while writing nothing. Returning
        # those bytes to a client is a silent failure on the device.
        with pytest.raises(MalformedProviderOutput):
            audio_mod.read_wav(payload)

    def test_an_8_bit_wav_is_refused_rather_than_misread(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(1)
            handle.setframerate(16_000)
            handle.writeframes(b"\x80" * 1_000)
        with pytest.raises(MalformedProviderOutput) as caught:
            audio_mod.read_wav(buffer.getvalue())
        assert "16-bit" in str(caught.value)

    def test_duration_of_unreadable_bytes_is_none_not_an_exception(self):
        # Used only for labelling a result; failing to label a duration must not fail the
        # synthesis that produced the audio.
        assert audio_mod.wav_duration_seconds(b"nonsense") is None


class TestContainerDecode:
    def test_a_wav_container_decodes_to_mono_16k(self):
        wav = audio_mod.write_wav(
            (np.sin(np.arange(22_050, dtype=np.float32) / 22_050 * 2 * math.pi * 500) * 0.5),
            sample_rate_hz=22_050,
        )
        out = audio_mod.decode_container(wav, max_seconds=10)
        assert abs(out.shape[0] - 16_000) < 200
        assert abs(dominant_frequency(out, 16_000) - 500) < 20

    @pytest.mark.parametrize("payload", [b"", b"not audio at all", b"\x00\x00\x00\x18ftypM4A fake"])
    def test_undecodable_bytes_are_refused(self, payload):
        with pytest.raises(MalformedProviderOutput):
            audio_mod.decode_container(payload, max_seconds=10)

    def test_a_long_container_is_refused(self):
        wav = audio_mod.write_wav(np.zeros(16_000 * 3, dtype=np.float32), sample_rate_hz=16_000)
        with pytest.raises(UnsupportedAudioFormat):
            audio_mod.decode_container(wav, max_seconds=1.0)
