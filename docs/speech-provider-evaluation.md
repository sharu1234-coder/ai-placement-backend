# Speech providers (VA-3) — evaluation, not a decision

**Status: no production provider selected.** Sources checked 2026-10-11. Everything below is
either measured on this machine or quoted from an official page with its date; nothing is
estimated.

## 1. What this phase built

A provider-neutral STT/TTS layer in `app/speech/`, plus adapters for the two speech
implementations this environment can actually run. The layer is **additive**: no existing
endpoint, provider or test changed behaviour.

The reason it is additive rather than wired in is in §6.

## 2. Candidate research

| Candidate | Official identifier | License | Runs on | Streaming | Formats | Blocker here |
|---|---|---|---|---|---|---|
Parakeet TDT 0.6B v2 | `nvidia/parakeet-tdt-0.6b-v2` (HF); NIM slug `nvidia/parakeet-tdt-0_6b-v2` | **cc-by-4.0** (HF card's own license field) | NeMo 2.2 + PyTorch, NVIDIA GPU (Volta/Ampere/Hopper/Blackwell), Linux preferred | Card does not state it; describes single-pass over ≤24-min segments | `.wav`/`.flac`, **16 kHz mono** | Needs `nemo_toolkit[asr]` + PyTorch — a multi-GB install, unauthorised |
Nemotron ASR Streaming | slug `nvidia/nemotron-asr-streaming`; container `nvcr.io/nim/nvidia/nemotron-asr-streaming` | NIM terms | Riva/NIM container, NVIDIA GPU | **Yes** — gRPC streaming via the Riva client | Riva ASR formats | Needs a NIM container + NGC credentials, and Docker; no deployment exists yet |
Magpie TTS Multilingual | `nvidia/magpie_tts_multilingual_357m` (card states 364M params) | **NVIDIA Open Model License Agreement**, "ready for commercial use" | NeMo Speech 25.11 / Riva 2.24.0; L4/L40/A10/A30/A100/H100, Linux | Card describes batched + sliding-window long-form; streaming not stated | Output **WAV mono 16-bit 22.05 kHz** | Same NeMo/GPU blocker; hosted path needs an API key |
Magpie TTS ZeroShot | catalog display name; separate model from Multilingual, English-only | NVIDIA terms; **requires access approval** | Riva/NIM | not established | — | Access approval + credentials |
faster-whisper (`tiny.en`) | `Systran/faster-whisper-tiny.en` via `faster_whisper.WhisperModel("tiny.en")` | MIT (library); weights are OpenAI Whisper derivatives | **CPU here**, already installed | No — offline encoder-decoder over a 30 s window | numpy float32 mono 16 kHz, or any container via FFmpeg | None — **runs today** |
pyttsx3 | `pyttsx3.init()`, host driver | MPL-2.0 | Host engine: SAPI5 / NSSpeech / eSpeak | No | WAV, host-chosen rate (22.05 kHz measured here) | None on Windows; **eSpeak on Linux NOT TESTED** |
ElevenLabs | — | — | — | — | — | **Excluded by instruction** |

Three things the research changed about my assumptions:

- **Magpie TTS Multilingual cannot clone voices.** The card states zero-shot voice cloning was
  removed "for security reasons". Any plan that assumed a cloned interviewer voice is dead on
  that model; ZeroShot is the cloning one, it is English-only, and it needs access approval.
- **Parakeet's license is contested across sources.** The NIM model card points at the NVIDIA
  Community Model License while the Hugging Face card's own license field says `cc-by-4.0`.
  The HF field is authoritative for the weights, and CC-BY-4.0 carries an **attribution
  obligation** that would have to appear in the app. Worth settling before, not after.
- **Every NVIDIA candidate needs an NVIDIA GPU on Linux, or a credentialed endpoint.** None of
  them is a "₹0 development" option in the sense the blueprint uses: the free part is the
  weights, not the inference.

### Costs and quotas — deliberately not stated

I found no official per-character or per-request NIM price for Magpie, and
`build.nvidia.com` labelling an endpoint "Free Endpoint" is not a quota or a retention
guarantee. The only number any search produced was `$0.00` from a third-party cost estimator,
which is not a source this project should price a feature on. **No pricing, free-tier or
no-training claim is recorded here**; obtaining them is a research item in §7.

## 3. Audio-format compatibility matrix

| Path | Produces / wants | Conversion | Status |
|---|---|---|---|
VA-1/VA-2 Android capture | PCM16LE, 16 kHz mono default (range 8–48 kHz), 20 ms frames, raw binary frames with no header | none needed at 16 kHz mono | measured |
VA-2 server→client audio | `[version:1][id_len:1][generation_id][PCM16LE]` | nothing emits it yet | unchanged |
Interview upload | MP4/AAC (`audio/mp4`, `audio/m4a`, `audio/x-m4a`), device-chosen rate, ≤10 MB | `decode_container` (FFmpeg) → mono float32 16 kHz | tested |
Interview question audio | `audio/wav` response | unchanged — still `AIProvider.synthesize_speech` | unchanged |
faster-whisper input | float32 mono **16 kHz** array | `to_recogniser_input` | tested against the real model |
pyttsx3 output (this host) | WAV mono 16-bit **22.05 kHz** | reported per call from the header, not assumed | measured |
Parakeet TDT v2 input | 16 kHz mono wav/flac | same 16 kHz target as above — the layer already produces it | not run |
Magpie TTS output | WAV mono 16-bit 22.05 kHz | would need 22.05 → 16 kHz for the realtime path | not run |

**The resampler question from VA-2 is resolved on the backend.** The VA-2 note recorded that
the Android resampler (linear + box pre-filter) must not feed a recogniser. `app/speech/audio.py`
uses **PyAV 18.1.0**, already installed as a faster-whisper dependency, which wraps FFmpeg's
`swresample` — a real polyphase resampler. No new dependency, no download. It is tested on the
property that matters: a 15 kHz tone resampled to 16 kHz comes back **attenuated, not folded
down into the speech band**, which is precisely what the naive resampler does wrong.

The Android resampler itself is **unchanged and still unsuitable** for that purpose. It is only
used for playback, which is what it was written for.

## 4. Measured numbers (this machine, CPU)

Windows 11, `.venv312`, `faster-whisper 1.2.1` + `ctranslate2 4.8.2`, `tiny.en`, int8, CPU.
From `tests/unit/test_speech_real_model.py` and a direct probe:

| Measurement | Value |
|---|---|
pyttsx3 synthesis, 9-word sentence | 1.14 s → 2.63 s of audio, 22.05 kHz mono |
Resample 22.05 kHz → 16 kHz mono (2.63 s) | 17 ms |
Model load (`tiny.en`, int8, CPU), once | 2.75 s |
Transcribe 2.63 s, cold | 0.90 s (RTF 0.34) |
Transcribe 2.63 s, warm | 0.25 s (**RTF 0.09**) |
Word recall on clean synthetic speech | 9/9 |

**What these numbers are not.** They are one machine, one clip of synthetic speech, no noise,
no accent, no crosstalk. They say the pipeline works and is not pathologically slow. They say
nothing about accuracy for an Indian student in a shared room, which is the only accuracy that
matters for this product, and they are not a basis for choosing a production recogniser.

## 5. Benchmark plan for the candidates that could not be run

Each needs an authorisation this phase does not have. Stated as a plan so the measurement is
reproducible when it is granted:

1. **Parakeet TDT 0.6B v2, CPU, no PyTorch.** `sherpa-onnx` publishes an int8 ONNX export
   (`csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`), and **onnxruntime 1.29.0 is
   already installed** — though CPU-only here (`get_available_providers()` returns
   `['AzureExecutionProvider', 'CPUExecutionProvider']`, no CUDA EP). Needs: authorisation to
   download ~600 MB of weights, and `sherpa-onnx` or a hand-written feature pipeline. Measure
   RTF and word error rate against the same clips as faster-whisper.
2. **Parakeet / Magpie via NeMo.** Needs: PyTorch + `nemo_toolkit`, several GB, and an NVIDIA
   GPU on Linux. Not worth doing before (1) answers whether the accuracy gain justifies it.
3. **Nemotron ASR Streaming.** Needs: Docker, NGC credentials, a GPU host. This is the only
   candidate that offers genuine streaming, which is what the realtime path wants, so it is
   the one worth the setup — but it is a deployment task and deployment is paused.
4. **Accuracy set.** No benchmark is meaningful on one synthetic sentence. Needed: a held-out
   set of real recordings at the target accents, which does not exist yet and is a data task,
   not an engineering one.

## 6. Why the layer is not wired into anything yet

Two integration points exist and both are deliberately left alone:

- **The file-based interview endpoints** (`POST /{id}/answer/audio`,
  `GET /{id}/questions/{qid}/audio`) still call `AIProvider.transcribe` /
  `synthesize_speech`. Switching them would change the behaviour of a shipped contract, and
  the brief requires a separately approved and tested plan for that. The new layer is ready
  for it: `decode_container` handles the same M4A upload, and the real-model test proves the
  container and PCM paths agree.
- **The VA-2 realtime gateway** stays provider-neutral and emits no transcript. Wiring STT in
  needs a *selected* provider, and selecting one before measurement is what §5 forbids.

So the honest state is: the contracts and conversions are built and tested; the decision they
exist to serve is still open.

## 7. Outstanding, and who can close it

| Item | Needs |
|---|---|
Official NIM/Riva pricing, quotas, retention and training policy | Vendor pages behind an account, or sales contact |
Parakeet v2 license reconciliation (HF `cc-by-4.0` vs NIM Community License) | Reading the linked license text; an attribution obligation affects the app's legal screens |
Linux eSpeak TTS | A Linux host. `tests/unit/test_tts_linux_contract.py` holds the test; **still NEEDS TEST** |
Real-device audio, live Android↔FastAPI socket, end-to-end spoken conversation | A device and a running server; **NEEDS TEST**, never performed |
Accuracy on target accents | A real recording set |
Production primary provider | All of the above |

## 8. Android native TextToSpeech

Evaluated only, as instructed. `android.speech.tts.TextToSpeech` is on-device, free, needs no
backend credential and no audio leaves the phone — attractive for reading questions aloud, and
it would remove the `audio/wav` round trip entirely. Against it: voice quality and available
languages are device-dependent in the same way `pyttsx3`'s are host-dependent, and it cannot
serve the agent's spoken replies if those are generated server-side.

**No migration in VA-3.** The interview flow is untouched; adopting it needs its own approved
and tested plan.

## 9. The female-voice defect: why every signal said "fixed" while the coach spoke as a man (2026-10-19)

The Voice Career Coach kept sounding male after a previous change that selected the Windows
Zira voice. Nothing about that change was wrong. It was in the wrong adapter.

### 9.1 Root cause: two synthesisers, one of them fixed

There are two independent TTS implementations in this backend:

| Path | Code | Reached from | Selected a voice? |
|---|---|---|---|
| AI provider | `OllamaProvider.synthesize_speech` in `app/ai/ollama_provider.py` | `app/services/interview_service.py:606`, the Mock Interview question-speaking call | **Yes** -- `_select_voice`, the previous fix |
| Speech layer | `Pyttsx3TextToSpeech` in `app/speech/pyttsx3_tts.py` | `app/voice/turn_runtime.py` -> `_default_tts()` -> `app.speech.selection.get_text_to_speech()`, resolved from `TTS_PROVIDER=pyttsx3` | **No** -- `pyttsx3.init()` then straight to `save_to_file` |

The Voice Career Coach takes the second path. It never asked the driver for a voice, so the
driver used the host default. Measured on this machine:

```
DEFAULT_VOICE_ID: ...\Tokens\TTS_MS_EN-US_DAVID_11.0
  name='Microsoft David Desktop - English (United States)' gender='Male'
  name='Microsoft Zira Desktop - English (United States)' gender='Female'
```

Zira was installed the whole time. The voice was never missing; the coach never asked for it.

This is why the usual checks all passed at once: `TTS_VOICE_GENDER=female` was set, the source
contained `Zira`, and a unit test asserted the selection -- all true, all about the other
adapter. Nothing in the running system contradicted them, because nothing in the running
system consulted them.

### 9.2 Second defect, found while fixing the first: verification placed too early

The fix added a read-back, so that a driver *accepting* a voice could be distinguished from a
driver *using* it. On the first run it reported `not_retained` -- the selection appearing to
fail.

It had not. pyttsx3 **queues** property writes: `DriverProxy.setProperty` calls
`self._push(self._driver.setProperty, (name, value))`, and the queue drains during
`runAndWait()`. `getProperty` reads the driver's current state. So a read-back taken straight
after the set returns the previous voice, always.

```
BEFORE: ...TTS_MS_EN-US_DAVID_11.0
WANTED: ...TTS_MS_EN-US_ZIRA_11.0
AFTER : ...TTS_MS_EN-US_DAVID_11.0     <- read back before runAndWait()
```

Confirmation now happens after synthesis, in `Pyttsx3TextToSpeech._confirm_voice`.
`tests/unit/test_voice_selection.py` carries a fake engine that reproduces the queueing, so
moving the check back before synthesis fails the suite rather than producing a false alarm in
production.

### 9.3 Proof the voice actually changed

A read-back proves the driver's state. It does not prove what the audio sounds like. Median
fundamental frequency does, and it needs no listener: adult male speech sits near 85-155 Hz,
adult female near 165-255 Hz. Same text, same adapter, one voice per process:

| Requested | Read-back after `runAndWait()` | Median F0 | Voiced frames | Peak |
|---|---|---|---|---|
| David | David | **93.4 Hz** | 123 | -0.4 dBFS |
| Zira | Zira | **175.7 Hz** | 144 | -3.5 dBFS |

93 Hz is male, 176 Hz is female, and the gap is far too large to be measurement noise.

### 9.4 The audio was never quiet

Measured through the real adapter via `scripts/voice_diagnostics.py`:

| Line | Peak | RMS | Clipped samples | Format |
|---|---|---|---|---|
| "Hey, how are you?" | -5.43 dBFS | -24.22 dBFS | 0 | WAV 22050 Hz mono |
| technical sentence | -3.58 dBFS | -20.25 dBFS | 0 | WAV 22050 Hz mono |

That is a healthy level with headroom and no clipping. **The "too quiet / sounds like the
earpiece" report is therefore not a synthesis-level problem**, and no gain was added -- raising
it would have introduced distortion to compensate for something happening elsewhere. The cause
is on the Android side: `AudioTrack` uses `USAGE_VOICE_COMMUNICATION`, the platform prefers the
earpiece for communication audio and follows `STREAM_VOICE_CALL`. `AndroidAudioOutputRouter`
addresses that and is wired in at `NetworkModule.createRealtimeVoiceSession`; it is now guarded
by `VoiceOutputWiringGuardTest`, because `VoiceSession`'s `output` parameter defaults to the
inert `AudioOutputRouter.None` and deleting the wiring compiles and passes every other test.

Whether the loudspeaker is actually loud enough in a room is a physical-device question and is
recorded as unverified.

### 9.5 Speaking rate: measured, then left alone

| Setting | Measured |
|---|---|
| driver default (SAPI rate 200) | **167 wpm** |
| forced rate 175 | **149 wpm** |

Conversational English is roughly 150-180 wpm, so the default is already inside the band.
`TTS_VOICE_RATE` therefore defaults to `0`, meaning "keep the driver default": changing it
would be an aesthetic preference presented as a fix, and nobody here has listened. The setting
exists so it can be tuned from configuration once somebody has.

### 9.6 What changed

- **`app/core/voice_selection.py`** (new) -- the single selector both paths call. It lives in
  `app/core` because `test_nothing_in_the_ai_layer_imports_the_speech_layer` forbids `app/ai`
  from importing `app.speech`; that test caught the first attempt, which put it in
  `app/speech`. Reports a typed `VoiceOutcome` rather than returning silently, so an unmet
  preference is a named state and a fallback is logged at **warning** -- previously it was
  logged at no level at all, which is how the wrong voice stayed invisible.
- **`app/speech/pyttsx3_tts.py`** -- selects a voice, confirms it after `runAndWait()`,
  reports `last_voice_choice`, and declares the driver's real voice list instead of the empty
  set the contract reads as "one voice".
- **`app/ai/ollama_provider.py`** -- `_select_voice` delegates to the shared selector, so the
  two paths cannot drift again.
- **`app/core/config.py`** -- `tts_voice_required` (make an unmet preference an error instead
  of a downgrade) and `tts_voice_rate`.
- **`app/voice/turn_metrics.py`** -- `tts_voice_outcome`, a closed vocabulary. A turn used to
  report provider, latency and `outcome=ok` while speaking in the wrong voice; the record
  could not answer "which voice spoke".
- **`scripts/voice_diagnostics.py`** (new) -- resolves the provider exactly as a voice turn
  does and reports adapter class, voice outcome, format and levels. Run this instead of
  reading configuration.

### 9.7 Kokoro: reviewed, not installed

Reviewed from the official repository (https://github.com/hexgrad/kokoro). **Not installed** --
installation and the model download remain authorization-gated.

| | |
|---|---|
| Install | `pip install "kokoro>=0.9.4" soundfile`, plus `misaki[en]` for English G2P |
| System package | **espeak-ng** still required, for English out-of-dictionary fallback and non-English |
| Parameters | 82 M |
| Weights | size not stated on the repo page; ~330 MB is consistent with 82 M parameters at fp32 (82e6 x 4 B = 328 MB) and matches the figure this project has been using. **Unconfirmed** until downloaded |
| Code licence | Apache-2.0 |
| Weights licence | Apache-2.0 -- permissive, commercial use allowed |
| Languages | American/British English, Spanish, French, Hindi, Italian, Japanese, Brazilian Portuguese, Mandarin |
| Output | 24 kHz |
| Voices | `af_heart` documented; **`af_bella` is not mentioned on the repo page** and could not be confirmed |
| Compute | not stated; pulls in **torch** |

Relevant consequences for this project:

1. It does **not** remove the espeak-ng dependency, so the open Linux question
   (`tests/unit/test_tts_linux_contract.py`, which skips and says so) stays open.
2. It adds **torch** to a backend that does not currently have it -- the largest dependency
   change in this codebase, and it lands in the request path.
3. Apache-2.0 on both code and weights is the best realistic licensing outcome; there is no
   commercial blocker.
4. No audition has been performed. **`af_heart` and `af_bella` have not been heard**, and no
   claim about their naturalness appears in this document.

ElevenLabs remains excluded.

### 9.8 Verification status

| Gate | Status |
|---|---|
| Active adapter identified | **Done**, measured, not inferred |
| Intended female voice selected at runtime | **Done** -- `outcome=selected`, Zira, confirmed by post-synthesis read-back |
| Audio generated and inspected | **Done** -- F0 175.7 Hz, peak -3.6 dBFS, 0 clipped |
| Backend regression tests | **Done** -- `tests/unit`: 2859 passed, 2 skipped, 0 failed (pytest exit 0, 783 s); `tests/api` real-adapter + transaction boundary: 17 passed |
| Android unit tests | **Done** -- 1538 tests, 0 failures, 0 errors, 0 skipped across 176 suites, counted from `TEST-*.xml` |
| Android lint | **Done** -- 0 errors, 32 pre-existing warnings |
| Android app builds | **Done** -- and the APK is byte-identical (sha256 `6b756f48...c92d3`): the Android changes were test-only, so **no reinstall is needed**. The fix is entirely backend-side and needs a backend restart |
| Heard on the phone | **BLOCKED** -- no device attached to this session |
| Loudspeaker routing confirmed in a room | **BLOCKED** -- same |
| Kokoro audition | **BLOCKED** -- installation not authorized |

The one claim this work cannot make is the one the user asked about first: nobody has heard it.
Everything above is instrument readings.
