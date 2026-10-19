"""TTS over the already-installed pyttsx3, as a development fallback.

Explicitly a fallback, not a candidate. `pyttsx3` drives whatever speech engine the *host*
provides -- SAPI5 on Windows, NSSpeechSynthesizer on macOS, eSpeak on Linux -- so its voice
quality, its output sample rate and whether it works at all are properties of the machine, not
of this code. That is disqualifying for production on its own, before voice quality is even
discussed.

It earns its place in VA-3 for one reason: it is the only synthesiser this environment can run,
so the STT adapter can be tested against real speech instead of a synthetic tone. The
measured loop is in `docs/speech-provider-evaluation.md`.

The Linux question remains open and is *not* answered here. `app/ai/ollama_provider.py`
records that the eSpeak path has never been tested, and `tests/unit/test_tts_linux_contract.py`
holds the test that would resolve it. This adapter inherits that gap unchanged: it reports a
driver that cannot start, or that starts and writes nothing, as a typed capability failure
rather than returning silence as if it were audio.
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from app.speech.audio import read_wav, wav_duration_seconds
from app.speech.contracts import (
    AudioEncoding,
    AudioFormat,
    CancellationToken,
    Deadline,
    MalformedProviderOutput,
    Speech,
    SpeechError,
    SpeechFailed,
    SpeechUnavailable,
    SynthesizeRequest,
    TextToSpeech,
    TtsCapabilities,
)
from app.core.voice_selection import (
    VoiceChoice,
    apply_rate,
    available_voice_ids,
    confirm_selection,
    log_choice,
    select_voice,
)

logger = logging.getLogger(__name__)

#: Measured on this host (Windows SAPI5): the driver writes 22.05 kHz mono 16-bit PCM WAV.
#: Declared, not assumed -- and verified per call, because another host's driver may differ and
#: a caller resampling to a rate the file does not have would shift the whole stream.
OBSERVED_OUTPUT_FORMAT = AudioFormat(AudioEncoding.WAV, 22_050, 1)

#: Longest text per request. A question in the interview flow is a sentence or two; this is
#: well above that and bounds how long one blocking driver call can run.
DEFAULT_MAX_CHARACTERS = 2_000

DEFAULT_BUDGET_SECONDS = 60.0


class Pyttsx3TextToSpeech(TextToSpeech):
    name = "pyttsx3"

    def __init__(
        self,
        *,
        max_characters: int = DEFAULT_MAX_CHARACTERS,
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
        engine_factory: Any = None,
    ) -> None:
        self.max_characters = max_characters
        self.budget_seconds = budget_seconds
        # Injectable so the failure paths -- a driver that will not start, one that writes
        # nothing, one that writes a broken header -- are testable on any host. Each of those
        # has been seen in this project; none of them is reachable from a test that needs a
        # working speech engine.
        self._engine_factory = engine_factory
        # Populated from the driver the first time one starts. `capabilities` is called before
        # every request and must stay cheap, so it cannot start an engine to enumerate -- but
        # declaring the empty set forever was its own bug: the contract reads empty as "one
        # voice", and this host has two, one of them the voice the product asks for.
        self._observed_voices: frozenset[str] = frozenset()
        #: The most recent selection result, for the diagnostics endpoint and for tests.
        #: Deliberately not reset between calls: the last known answer is more useful than
        #: None when something downstream asks why the voice sounds wrong.
        self.last_voice_choice: Optional[VoiceChoice] = None

    @property
    def capabilities(self) -> TtsCapabilities:
        return TtsCapabilities(
            output_format=OBSERVED_OUTPUT_FORMAT,
            # English only as declared capability. The host driver may have other voices
            # installed, but which ones is unknowable from here, and claiming a language the
            # machine cannot speak is the failure this layer exists to prevent.
            languages=frozenset({"en"}),
            max_characters=self.max_characters,
            voices=self._observed_voices,
            # save_to_file writes a complete file and runAndWait blocks until it is done.
            # There is no chunk boundary to yield at, so there is no streaming -- and
            # stream_synthesize is left unimplemented so the flag cannot drift from the code.
            streaming=False,
        )

    def synthesize(self, request: SynthesizeRequest) -> Speech:
        self.check_supported(request)
        deadline = Deadline(request.budget_seconds or self.budget_seconds)
        cancellation = request.cancellation or CancellationToken()
        cancellation.raise_if_cancelled(self.name, "starting synthesis")

        # Character count only. The text is an interview question today, but this adapter must
        # not become the reason user-authored text reaches the logs.
        logger.info("synthesising %d characters with %s", len(request.text), self.name)

        started = time.monotonic()
        audio = self._write_wav(request.text, cancellation, request.voice)
        latency = time.monotonic() - started
        deadline.raise_if_expired(self.name)

        # A driver that loads but writes nothing is a real, observed failure mode, and the
        # bytes would reach a client as a file its player cannot prepare -- a silent failure
        # on the device. read_wav is the check: it parses the header the client will parse.
        try:
            _samples, actual = read_wav(audio)
        except MalformedProviderOutput as exc:
            raise SpeechUnavailable(
                self.name, f"the host speech driver produced no usable WAV ({exc})"
            ) from exc

        if actual != self.capabilities.output_format:
            # Not a failure: the host driver legitimately differs between machines. But the
            # caller is told what it actually got, because a declared rate it then trusts is
            # how a resample goes wrong.
            logger.info(
                "%s produced %s, not the declared %s",
                self.name,
                actual.describe(),
                self.capabilities.output_format.describe(),
            )

        return Speech(
            audio=audio,
            audio_format=actual,
            audio_seconds=wav_duration_seconds(audio),
            # For a batch synthesiser the whole call *is* the time to first audio: nothing is
            # available before it returns. Reported as measured rather than as an estimate.
            first_audio_latency_seconds=latency,
        )

    def _write_wav(
        self, text: str, cancellation: CancellationToken, voice: Optional[str] = None
    ) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            path = handle.name
        try:
            engine = self._start_engine(voice)
            cancellation.raise_if_cancelled(self.name, "driving the speech engine")
            try:
                engine.save_to_file(text, path)
                engine.runAndWait()
                # Only now is the queued voice write real, so only now can it be checked.
                self._confirm_voice(engine)
            except SpeechError:
                raise
            except Exception as exc:
                raise SpeechFailed(self.name, f"the speech driver failed while writing audio: {exc}") from exc
            # runAndWait blocks until the file is written, so a cancel observed here arrived
            # too late to stop the work. It still prevents the audio being returned, which is
            # what the caller asked for -- and the cost of that honesty is one wasted
            # synthesis, which is cheaper than pretending the call was interruptible.
            cancellation.raise_if_cancelled(self.name, "finishing synthesis")
            try:
                return Path(path).read_bytes()
            except OSError as exc:
                raise SpeechFailed(self.name, f"the driver wrote no output file: {exc}") from exc
        finally:
            Path(path).unlink(missing_ok=True)

    @staticmethod
    def _default_engine() -> Any:
        import pyttsx3

        # No driverName: pyttsx3 picks the platform default -- sapi5 on Windows, nsss on
        # macOS, espeak everywhere else. Naming one here would make this module
        # platform-specific, which it is not; the host is what decides.
        return pyttsx3.init()

    def _start_engine(self, voice: Optional[str] = None) -> Any:
        engine = self._create_engine()
        # The actual fix. Before this, the engine went straight to save_to_file, so the
        # driver spoke with the host's system default -- "Microsoft David Desktop" here, a
        # male voice, for a product specified as a female career coach.
        self._observed_voices = available_voice_ids(engine)
        choice = select_voice(engine, voice=voice)
        self.last_voice_choice = choice

        from app.core.config import settings

        # A voice that is missing, unmatched or refused is knowable before synthesis, so a
        # deployment that requires its voice fails here rather than after spending a
        # synthesis it is going to throw away.
        if choice.outcome.is_fallback:
            log_choice(choice, where=self.name)
            self._enforce_voice(choice)

        apply_rate(engine, settings.tts_voice_rate)
        return engine

    def _confirm_voice(self, engine: Any) -> None:
        """Resolve the queued voice write now that `runAndWait` has drained the queue.

        pyttsx3 queues property writes, so the selection made in `_start_engine` is only
        observable here. This is also the only point at which a driver that silently kept a
        different voice can be caught -- `setProperty` returning without raising does not mean
        the voice changed.
        """
        choice = self.last_voice_choice
        if choice is None:
            return
        confirmed = confirm_selection(engine, choice)
        self.last_voice_choice = confirmed
        log_choice(confirmed, where=self.name)
        self._enforce_voice(confirmed)

    def _enforce_voice(self, choice: VoiceChoice) -> None:
        """Turn an unmet voice preference into a typed failure, when configured to.

        Default is off: a student would rather hear the wrong voice than silence. But the
        fallback is always logged as a warning and always reported by the diagnostics script
        -- it was the *silence* about the fallback, not the fallback itself, that let this
        defect survive a configuration setting, a code fix and a passing test.
        """
        from app.core.config import settings

        if not (choice.outcome.is_fallback and settings.tts_voice_required):
            return
        # Unavailable rather than Failed: the fix is installing a voice on the host or
        # relaxing the requirement, not retrying the request.
        raise SpeechUnavailable(
            self.name,
            f"the configured voice could not be selected ({choice.describe()}); "
            "set TTS_VOICE_REQUIRED=false to speak with the host default instead",
        )

    def _create_engine(self) -> Any:
        factory = self._engine_factory or self._default_engine
        try:
            return factory()
        except Exception as exc:
            # On Linux this is what a missing espeak-ng OS package looks like: pyttsx3 loads
            # libespeak through ctypes and surfaces the failure as an OSError or its own
            # EngineError, so the type is not reliable enough to match on. Unavailable, not
            # failed -- the fix is installing a package, and the typed difference is what
            # tells an operator that rather than sending them to look at the request.
            raise SpeechUnavailable(
                self.name, f"the host speech driver could not be initialised: {exc}"
            ) from exc

    def observed_output_format(self, audio: bytes) -> Optional[AudioFormat]:
        """The format of audio this adapter produced, read from its header.

        Exposed because the declared capability is a statement about *this* host. A caller
        about to resample should ask the bytes, not the declaration.
        """
        try:
            _samples, fmt = read_wav(audio)
            return fmt
        except MalformedProviderOutput:
            return None
