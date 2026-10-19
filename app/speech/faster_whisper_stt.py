"""STT over the already-installed faster-whisper.

Chosen for VA-3 because it is the only recogniser this environment can actually run: the
package (1.2.1), its CTranslate2 runtime and the `tiny.en` weights are all present, so this
adapter can be tested against a real model rather than only against a fake. Every other
candidate in the Zero-Cost Blueprint needs NeMo + PyTorch + an NVIDIA GPU, or a Riva/NIM
endpoint and credentials -- see `docs/speech-provider-evaluation.md`.

It is **not** nominated as the production recogniser. `tiny.en` is English-only and the
measured real-time factor is on CPU; the blueprint's candidates may well win on accuracy or
latency. That comparison needs measurement that this environment cannot perform, and the
brief forbids choosing a primary before it exists.

Two things this adapter deliberately does differently from `OllamaProvider.transcribe`, which
stays untouched:

* **No temp file.** faster-whisper accepts a numpy array, so PCM goes straight in. The old
  path wrote bytes to a `.m4a` temp file and let the library decode it, which works but puts a
  filesystem round trip and a filename suffix guess in the hot path of every utterance.
* **Cancellation and a deadline.** `transcribe()` returns a *generator* of segments, so the
  work happens as it is consumed -- which is the point where control genuinely returns to us,
  and therefore the only point where cancelling is honest.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from typing import Any, Optional

from app.speech.audio import decode_container, to_recogniser_input
from app.speech.contracts import (
    AudioEncoding,
    CancellationToken,
    Deadline,
    MalformedProviderOutput,
    SpeechError,
    SpeechFailed,
    SpeechToText,
    SpeechUnavailable,
    SttCapabilities,
    TranscribeRequest,
    Transcript,
)

logger = logging.getLogger(__name__)

#: Rates accepted for raw PCM. 16 kHz is the realtime path's own format and needs no
#: conversion; the rest are the common device capture rates, converted through swresample.
#: A rate outside this set is refused rather than resampled from an unvalidated value.
SUPPORTED_PCM_RATES_HZ = frozenset({8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000})

#: Ceiling on one request. Bounds memory and inference time: at the measured CPU real-time
#: factor, five minutes of audio is already a long-running request, and the interview upload
#: route's own 10 MB cap allows more than that for a compressed stream.
DEFAULT_MAX_AUDIO_SECONDS = 300.0

#: Default wall-clock budget. Generous relative to the measured RTF so an ordinary answer
#: never trips it, while an unbounded one cannot hold a threadpool worker indefinitely.
DEFAULT_BUDGET_SECONDS = 180.0


class FasterWhisperSpeechToText(SpeechToText):
    name = "faster-whisper"

    def __init__(
        self,
        model_size: Optional[str] = None,
        *,
        device: str = "cpu",
        compute_type: str = "int8",
        max_audio_seconds: float = DEFAULT_MAX_AUDIO_SECONDS,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        model: Any = None,
    ) -> None:
        from app.core.config import settings

        self.model_size = model_size or settings.whisper_model_size
        self.device = device
        self.compute_type = compute_type
        self.max_audio_seconds = max_audio_seconds
        self.budget_seconds = budget_seconds
        # An injected model keeps the adapter's own logic testable without loading weights.
        # Not a back door: the contract tests use it precisely so that the error paths are
        # exercised deterministically, and the real-model test passes nothing.
        self._model = model
        self._model_lock = threading.Lock()

    # ------------------------------------------------------------------ capabilities

    @property
    def capabilities(self) -> SttCapabilities:
        return SttCapabilities(
            input_formats=frozenset({AudioEncoding.PCM16, AudioEncoding.WAV, AudioEncoding.M4A}),
            sample_rates_hz=SUPPORTED_PCM_RATES_HZ,
            max_channel_count=2,
            languages=self._languages(),
            max_audio_seconds=self.max_audio_seconds,
            # avg_logprob is a real signal the model reports; see `_confidence` for what it is
            # and is not.
            supplies_confidence=True,
            # Whisper is an offline encoder-decoder over a 30-second window. Streaming it
            # means chunking with overlap and reconciling the seams, which is a different
            # algorithm rather than a flag -- so this is False, and `stream_transcribe` is
            # left unimplemented so the two cannot disagree.
            streaming=False,
            partial_transcripts=False,
        )

    def _languages(self) -> frozenset[str]:
        """English-only for a `.en` model, auto-detect otherwise.

        Derived from the model name rather than configured, because it *is* a property of the
        weights: `tiny.en` has no multilingual decoder, so accepting `language="hi"` would
        produce fluent English-shaped nonsense from Hindi audio -- a wrong answer that looks
        like a right one. An empty set means "the provider detects the language itself", which
        is what the multilingual checkpoints do.
        """
        return frozenset({"en"}) if self.model_size.endswith(".en") else frozenset()

    # ------------------------------------------------------------------ model

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        with self._model_lock:
            if self._model is None:
                try:
                    # Must be set before huggingface_hub is first imported. The
                    # xet-accelerated download path hung indefinitely in this project's dev
                    # testing; the plain HTTPS fallback is reliable. Same reason and same
                    # ordering as app/ai/ollama_provider.py.
                    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
                    from faster_whisper import WhisperModel

                    logger.info(
                        "loading faster-whisper model %r on %s (%s) -- first use only",
                        self.model_size,
                        self.device,
                        self.compute_type,
                    )
                    self._model = WhisperModel(self.model_size, device=self.device, compute_type=self.compute_type)
                except Exception as exc:
                    # A missing package, an absent download, or an unsupported compute type
                    # all land here. Unavailable rather than failed: nothing was transcribed,
                    # and the fix is an operator action.
                    raise SpeechUnavailable(self.name, f"the recogniser could not be loaded: {exc}") from exc
        return self._model

    # ------------------------------------------------------------------ transcription

    def transcribe(self, request: TranscribeRequest) -> Transcript:
        self.check_supported(request)
        deadline = Deadline(request.budget_seconds or self.budget_seconds)
        cancellation = request.cancellation or CancellationToken()
        cancellation.raise_if_cancelled(self.name, "preparing audio")

        samples = self._prepare(request)
        audio_seconds = float(samples.shape[0]) / 16_000
        # Byte and second counts only. Never the transcript, never the audio: this runs on
        # every utterance, and application logs are not a place to accumulate what a student
        # said about their weaknesses.
        logger.info("transcribing %.2fs of audio with %s", audio_seconds, self.model_size)

        model = self._load_model()
        cancellation.raise_if_cancelled(self.name, "loading the model")
        deadline.raise_if_expired(self.name)

        try:
            segments, _info = model.transcribe(samples, language=request.language or None)
        except Exception as exc:
            raise SpeechFailed(self.name, f"the recogniser rejected the audio: {exc}") from exc

        pieces: list[str] = []
        log_probs: list[float] = []
        try:
            for segment in segments:
                # The generator is where the inference actually happens, so these two checks
                # are the only ones with teeth. Checking before the loop would bound nothing.
                cancellation.raise_if_cancelled(self.name, "transcribing")
                deadline.raise_if_expired(self.name)
                text = getattr(segment, "text", None)
                if text is None:
                    raise MalformedProviderOutput(self.name, "a segment carried no text field")
                pieces.append(text.strip())
                log_prob = getattr(segment, "avg_logprob", None)
                if isinstance(log_prob, (int, float)):
                    log_probs.append(float(log_prob))
        except SpeechError:
            # Cancellation, the deadline and a contract violation are all already the right
            # error. Re-wrapping them would hide a cancel behind "transcription failed", and a
            # caller that cancelled needs to see that it was honoured.
            raise
        except Exception as exc:
            raise SpeechFailed(self.name, f"transcription stopped partway: {exc}") from exc

        return Transcript(
            text=" ".join(piece for piece in pieces if piece).strip(),
            # Batch: there is exactly one result and it is the answer. An empty string is a
            # legitimate final transcript -- silence transcribes to nothing, and reporting
            # that as a failure would turn "the student said nothing" into an error.
            is_final=True,
            language=request.language or (list(self._languages()) or [None])[0],
            confidence=self._confidence(log_probs),
            audio_seconds=audio_seconds,
        )

    def _prepare(self, request: TranscribeRequest):
        """Request audio to mono float32 at 16 kHz, by the explicit path for its encoding."""
        if request.audio_format.encoding is AudioEncoding.PCM16:
            return to_recogniser_input(
                request.audio, request.audio_format, max_seconds=self.max_audio_seconds
            )
        # WAV and M4A both go through the container decoder: it reads the real rate from the
        # header instead of trusting the caller's, which for an M4A recorded by a device is
        # whatever that device chose.
        return decode_container(request.audio, max_seconds=self.max_audio_seconds)

    def _confidence(self, log_probs: list[float]) -> Optional[float]:
        """Mean per-token log-probability, exponentiated into 0.0--1.0.

        What this is: the model's own average token probability, which does track transcription
        quality in practice. What it is not: a calibrated probability that the transcript is
        correct. It is returned because a caller can usefully threshold on it, and labelled
        here so nobody mistakes it for the latter. No log-probs means None, not a default --
        an invented confidence is worse than an absent one.
        """
        if not log_probs:
            return None
        mean = sum(log_probs) / len(log_probs)
        return max(0.0, min(1.0, math.exp(mean)))
