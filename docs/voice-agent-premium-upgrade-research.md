# Voice Agent — premium upgrade research

**Date:** 2026-10-17 · **For:** TALVIORA AI, SGLD Quant Labs
**Status:** research complete; one authorization gate open (see §6)

Every repository in §3 was fetched over the network on 2026-10-17 and the findings below come from
those pages. Where a page did not state something, this document says so rather than filling the
gap from memory. Licence findings are from the repository landing pages only — **a landing page is
not a licence file**, and §6's gate requires reading `LICENSE` and the model card before adoption.

---

## 1. Existing architecture (verified in source, not assumed)

| Layer | Implementation | Note |
|---|---|---|
| Transport | `WebSocketRealtimeTransport` over OkHttp, `/voice/session` | Client URL must be `http(s)`; `buildUrl` parses with `toHttpUrl()` |
| Backend gateway | `app/api/voice_session.py`, `APIRouter(prefix="/voice")` + `websocket("/session")` | Bearer → App Check → `authenticate_access_token`, all before `accept()` |
| Handshake params | `sample_rate`, `channels`, `locale`, `mode`, `interview_id`, `interview_question_id` | Exact parity with the Android client |
| Turn runtime | `app/voice/turn_runtime.py` — STT → responder → TTS | Typed `TurnFailure`; errors returned, not thrown |
| STT | `faster-whisper` (development only) | **Turn-based. No partial transcripts** — see §3.H |
| TTS | `pyttsx3` (development only) | `TTS_PROVIDERS = frozenset({"none", "pyttsx3"})` |
| Production defaults | `ai=ollama`, `stt=none`, `tts=none`, `job=none` | Unchanged by this work |
| Android states | `ConversationState`, **12 variants** | Idle, Connecting, Listening, Transcribing, Thinking, ToolCalling, Generating, Speaking, Interrupted, Reconnecting, Failed, Closed |

### The constraint that shapes everything below

`app/voice/responders.py::_speak_coaching` does **not** prompt a model to phrase an answer. It
concatenates fixed section labels with grounded claims, and its own comment states why: *"Nothing
is added… a summary of evidence is a new claim about it."* The coach's wording is therefore a
property of the grounding layer, not of a system prompt. See §5.

---

## 2. What was already working, and how it is preserved

The user manually confirmed: Voice Career Coach opens, captures speech, returns text, plays audio.

That path is preserved by construction — nothing in this increment touches the transport, the
handshake, the protocol, the recogniser, the synthesizer selection or the provider defaults. The
two changes are additive: a UI component, and a text-rendering function applied to the string
handed to the synthesizer. Regression evidence is in the final report.

---

## 3. Repository review

| # | Repository | Code licence | Weights / assets licence | Verdict |
|---|---|---|---|---|
| A | [hexgrad/kokoro](https://github.com/hexgrad/kokoro) | Apache-2.0 | **Apache-2.0** ("Apache-licensed weights") | **Recommended candidate** |
| A2 | [kokoro.js README](https://github.com/hexgrad/kokoro/blob/main/kokoro.js/README.md) | not stated on page | — | Voice grades, ONNX build |
| B1 | [pipecat-ai/pipecat](https://github.com/pipecat-ai/pipecat) | BSD-2-Clause | n/a | **Reject** — framework |
| B2 | [livekit/agents](https://github.com/livekit/agents) | Apache-2.0 | **LiveKit Model License** (turn detection) | **Reject** — needs WebRTC infra |
| B3 | [livekit-examples/agent-starter-python](https://github.com/livekit-examples/agent-starter-python) | MIT | n/a | **Reject** — LiveKit Cloud + paid hosted models |
| C1 | [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | Apache-2.0 | **not stated** per model | Possible future on-device path |
| C2 | [snakers4/silero-vad](https://github.com/snakers4/silero-vad) | MIT — **but see note** | not separately stated | **Licence must be resolved first** |
| C3 | [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | HF download (CTranslate2) | Already in use |
| D1 | [myshell-ai/OpenVoice](https://github.com/myshell-ai/OpenVoice) | MIT, "free for commercial" | not separately addressed | **Reject** — see below |
| D2 | [kyutai-labs/moshi](https://github.com/kyutai-labs/moshi) | MIT / Apache-2.0 | **CC-BY 4.0** | **Reject** — ~24 GB VRAM |

### Notes that matter

**Kokoro (A).** 82M parameters. Languages: American/British English, Spanish, French, **Hindi**,
Italian, Japanese, Brazilian Portuguese, Mandarin. Installed as `pip install kokoro>=0.9.4`, uses
`misaki` for G2P; espeak-ng is used for English out-of-dictionary fallback and some non-English
languages. Weights live at `huggingface.co/hexgrad/Kokoro-82M`. An ONNX build exists
(`onnx-community/Kokoro-82M-v1.0-ONNX`) with fp32/fp16/q8/q4 quantisation. Apache-2.0 on *both*
code and weights is the decisive property — it is the only candidate here with no commercial or
attribution encumbrance on the asset actually shipped.

**Silero VAD (C2) — licence ambiguity, recorded not resolved.** The landing page presents MIT
*and* carries a CC BY-NC 4.0 badge whose alt text contradicts its MIT label. CC BY-NC would
prohibit commercial use. This must be settled from the repository's licence files before adoption.
Technically it is attractive: ~2 MB, 8/16 kHz, sub-millisecond per 30 ms chunk, ONNX anywhere.

**Pipecat / LiveKit (B).** Both are orchestration *frameworks* with their own pipeline models and
transports. Adopting either would replace the hand-built FastAPI/WebSocket layer, which §2 of the
governing prompt forbids. LiveKit additionally requires LiveKit Cloud or a self-hosted WebRTC
server for anything beyond `console` mode, and its turn-detection models are under a bespoke
LiveKit Model License rather than Apache-2.0. Neither page documented its interruption or
sentence-chunked-TTS pattern, so no pattern was borrowed — **no claim is made here about how they
implement barge-in**, because the pages did not say.

**OpenVoice (D1) — rejected on purpose, not on licence.** MIT and commercially permitted, but it is
an *instant voice cloning* model. Cloning a voice introduces a consent question this product has
no process for, and the page offers no latency or quality benchmarks. It is not a general-purpose
TTS engine. Not recommended regardless of licence.

**faster-whisper (C3) — confirms an existing limitation.** The page does not describe streaming;
real-time use is delegated to third-party projects (WhisperLive, Whisper-Streaming). CPU figures:
small model, int8, 13 min audio in 1m42s. **This is the documentary basis for never claiming
partial transcripts or streaming recognition.**

---

## 4. Female TTS candidate comparison

Kokoro's own published grades for American female voices:

| Voice | Target quality | Overall grade |
|---|---|---|
| `af_heart` | A | **A** |
| `af_bella` | A | **A-** |
| `af_nicole` | B | B- |
| `af_aoede` / `af_kore` / `af_sarah` | B | C+ |
| `af_alloy` / `af_nova` | B | C |
| `af_sky` | B | C- |
| `af_jessica` / `af_river` | C | D |

**These are the publisher's grades, not an audition.** `af_heart` and `af_bella` are the only two
graded A-range and are the shortlist. §4 of the governing prompt requires recorded listening
observations against the five sample scripts; **none has been performed**, because auditioning
requires installing `kokoro` and downloading weights — the gate in §6.

| Criterion | Kokoro (proposed) | pyttsx3 / SAPI5 (current dev) |
|---|---|---|
| Female presentation | `af_heart`, `af_bella` | Host-voice dependent |
| Naturalness | **NOT TESTED** | Robotic; formant synthesis |
| Indian-accented English | **NOT TESTED** — not an advertised locale | Not supported |
| Hindi | Advertised | No |
| Telugu | **Not supported** — absent from the language list | No |
| Latency to first audio | **NOT MEASURED** | ~instant, local |
| Streaming/chunking | Not stated on page | No |
| Commercial rights | Apache-2.0 code + weights | OS-bundled voices; redistribution unclear |
| Deployment | New dep + ~330 MB weights | Already present; **Windows-only in practice** |

**Telugu must not be advertised.** It is not in Kokoro's language list. Hindi may be advertised
only after the audition confirms it.

---

## 5. Conversation persona — a conflict, recorded not resolved

§5 of the governing prompt specifies a casual persona: contractions, one-to-four-sentence replies,
varied phrasing, one follow-up question.

The current coach cannot deliver that without changing what it guarantees. `_speak_coaching`
renders *verified claims* produced by the grounding layer and deliberately adds nothing. Letting a
model rephrase those sentences would convert grounded claims into model-authored text — the exact
thing the grounding layer exists to prevent, and a direct conflict with `LS §13.2` ("a rejected or
unverified action is never described to the user as successful").

**This is an owner decision, not an engineering one.** Three options:

1. **Keep grounding; improve only the fixed labels.** Section labels and connectives are
   server-authored constants, so they can be warmed up with no loss of guarantee. Narrow but safe.
2. **Add a phrasing pass over grounded claims, constrained to not alter facts.** Needs a new
   verification mechanism proving the paraphrase preserved every claim, number and qualification.
   Substantial work; new failure mode.
3. **Exempt the conversational shell only** (greetings, acknowledgements, follow-up questions)
   while claim sentences stay verbatim. Middle ground; needs a tested boundary between the two.

**Nothing in this increment changed the persona.** Option 1's groundwork is what §7 delivers.

---

## 6. Authorization gate — Kokoro

Required before any audition or adoption:

| | |
|---|---|
| **Component** | `kokoro>=0.9.4` (PyPI) + `misaki` G2P + `Kokoro-82M` weights from Hugging Face; optionally `espeak-ng` system package |
| **Why** | Only evaluated candidate that is both premium-quality female and unencumbered (Apache-2.0 code *and* weights) |
| **Licensing** | Apache-2.0 both layers per the landing page. **Must be confirmed from `LICENSE` and the HF model card before adoption.** |
| **Resources** | ~82M params, ~330 MB weights; CPU-viable, no GPU required |
| **External cost** | **None** — self-hosted, no API, no per-request charge |
| **Benefit** | A genuinely premium female voice; replaces a Windows-only dev adapter that cannot deploy to Linux |
| **Tests after approval** | New `KokoroTextToSpeech` adapter against the existing `TextToSpeech` contract; add `"kokoro"` to `TTS_PROVIDERS`; empty/failed-synthesis paths; audio format and sample-rate parity; the five §4 sample scripts auditioned and recorded; cold-start vs warm-turn timings separated |

Blocked items that are **not** unblocked by this gate: Linux TTS contract (needs a Linux host),
physical-device acceptance (needs a handset), PostgreSQL CI (needs a CI run).

---

## 7. Delivered in this increment

1. **`VoiceOrb`** — a 12-state Android visualiser replacing a shared 16 dp spinner. Its invariant:
   expanding rings are drawn **only** for `Listening`, because outward motion is what a person
   reads as "the microphone is open". Reduced-motion is honoured via `ANIMATOR_DURATION_SCALE`.
2. **`app/voice/speech_text.py::for_speech`** — renders an answer for listening: URLs become "a
   link on your screen", markdown/table/heading/bullet notation is dropped, cells become pauses.
   Meaning is preserved by design and by test: no sentence, number, warning or qualification is
   removed, and no new wire field was introduced — display text travels unchanged.

## 8. Not done, and why

| Item | Status | Reason |
|---|---|---|
| Kokoro audition on the five scripts | **BLOCKED** | §6 gate — needs install + weights |
| Production TTS selection | **BLOCKED** | Owner decision; defaults stay `none` |
| Casual persona | **BLOCKED** | §5 conflict; owner decision |
| Silero VAD adoption | **BLOCKED** | Licence ambiguity unresolved |
| Streaming TTS chunking | **NOT APPLICABLE** | No approved streaming adapter exists |
| Device latency metrics | **BLOCKED** | No handset on adb |
| Telugu speech | **NOT SUPPORTED** | Absent from Kokoro's languages |
