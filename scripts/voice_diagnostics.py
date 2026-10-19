"""Report which TTS adapter actually speaks, and with which voice.

Run this instead of reasoning about configuration. The female-voice defect it was written for
looked correct from every static angle at once -- ``TTS_VOICE_GENDER=female`` was set, the
source contained ``Zira``, and a unit test asserted the selection -- while the running Voice
Career Coach spoke in the male host default, because the selection lived in a different
adapter from the one ``TTS_PROVIDER`` resolves.

So this resolves the provider the same way the voice turn does
(``app.speech.selection.get_text_to_speech``), synthesises through it, and reports what came
back. A claim about the voice that is not backed by this script's output is a claim about the
source code.

Usage::

    ./.venv312/Scripts/python.exe -m scripts.voice_diagnostics
    ./.venv312/Scripts/python.exe -m scripts.voice_diagnostics --rate 200 --keep out.wav

## Privacy

Prints the adapter class, the voice outcome, voice ids and names, the audio format and
measured levels. It never prints a bearer token, an API key, a resume, or a user transcript:
the only text it synthesises is the fixed acceptance line below, and no user data is read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: The line from the acceptance script, so the measured numbers correspond to something a
#: person will actually say to the coach rather than to a synthetic phrase.
GREETING = "Hey, how are you?"

#: A longer line with technical terms, which is where a rate that is too fast stops being
#: merely brisk and starts costing intelligibility.
TECHNICAL = (
    "Your strongest area is data structures, but your API design and PostgreSQL indexing "
    "need work before a backend interview."
)


def _measure(audio: bytes) -> dict[str, object]:
    """Format and level, read from the bytes rather than from the declaration."""
    import numpy as np

    from app.speech.audio import read_wav, wav_duration_seconds

    samples, fmt = read_wav(audio)
    # `read_wav` returns float32 already normalised to -1.0..1.0 (it runs the PCM through
    # `pcm16_to_float32`). Dividing by 32768 again cost 90 dB and made healthy audio read as
    # -96 dBFS, i.e. silence -- which would have been reported as the cause of the
    # "too quiet" complaint. Full scale is 1.0 here.
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float64))))) if samples.size else 0.0
    return {
        "encoding": fmt.encoding.value,
        "sample_rate_hz": fmt.sample_rate_hz,
        "channels": fmt.channel_count,
        "bytes": len(audio),
        "duration_seconds": wav_duration_seconds(audio),
        "peak_dbfs": round(20 * np.log10(peak), 2) if peak else None,
        "rms_dbfs": round(20 * np.log10(rms), 2) if rms else None,
        "clipped_samples": int(np.sum(np.abs(samples) >= 0.999)) if samples.size else 0,
    }


def _report(label: str, text: str, keep: Path | None) -> int:
    from app.speech.contracts import SpeechError, SynthesizeRequest
    from app.speech.selection import get_text_to_speech

    print(f"\n--- {label} ---")
    try:
        provider = get_text_to_speech()
    except Exception as exc:  # noqa: BLE001 - reporting tool
        print(f"PROVIDER_RESOLUTION_FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"adapter_class: {type(provider).__name__}")
    print(f"adapter_module: {type(provider).__module__}")
    print(f"adapter_name: {provider.name}")

    try:
        speech = provider.synthesize(SynthesizeRequest(text=text, language="en"))
    except SpeechError as exc:
        print(f"SYNTHESIS_FAILED: {type(exc).__name__}: {exc}")
        choice = getattr(provider, "last_voice_choice", None)
        if choice is not None:
            print(f"voice_selection: {choice.describe()}")
        return 1

    choice = getattr(provider, "last_voice_choice", None)
    if choice is None:
        # The adapter does not report a selection at all -- which is the exact shape of the
        # original defect, so it is called out rather than left blank.
        print("voice_selection: NOT REPORTED BY THIS ADAPTER")
    else:
        print(f"voice_selection: {choice.describe()}")
        print(f"voice_satisfied: {choice.outcome.is_satisfied}")

    print(f"declared_voices: {len(provider.capabilities.voices)}")
    measured = _measure(speech.audio)
    for key, value in measured.items():
        print(f"{key}: {value}")
    print(f"first_audio_latency_seconds: {round(speech.first_audio_latency_seconds or 0, 3)}")

    duration = measured["duration_seconds"]
    if isinstance(duration, float) and duration > 0:
        words = len(text.split())
        print(f"words: {words}  measured_wpm: {round(words / duration * 60)}")

    if keep is not None:
        target = keep.with_name(f"{keep.stem}-{label}{keep.suffix}")
        target.write_bytes(speech.audio)
        print(f"wrote: {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rate",
        type=int,
        default=None,
        help="override TTS_VOICE_RATE for this run (0 keeps the driver default)",
    )
    parser.add_argument(
        "--gender", default=None, help="override TTS_VOICE_GENDER for this run"
    )
    parser.add_argument(
        "--keep",
        type=Path,
        default=None,
        help="write the synthesised WAV files here, for listening",
    )
    args = parser.parse_args(argv)

    from app.core.config import settings

    if args.rate is not None:
        settings.tts_voice_rate = args.rate
    if args.gender is not None:
        settings.tts_voice_gender = args.gender

    print("=== TALVIORA AI voice diagnostics ===")
    print(f"tts_provider: {settings.tts_provider}")
    print(f"tts_voice_gender: {settings.tts_voice_gender}")
    print(f"tts_voice_rate: {settings.tts_voice_rate}")
    print(f"tts_voice_required: {settings.tts_voice_required}")

    status = 0
    for label, text in (("greeting", GREETING), ("technical", TECHNICAL)):
        status |= _report(label, text, args.keep)
    return status


if __name__ == "__main__":
    sys.exit(main())
