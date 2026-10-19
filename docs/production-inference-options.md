# Production inference — options, not a decision

**Status: DESIGN NOTE ONLY. No provider or model is selected here, and no code implements any
option below.** VA-0.5 item 0.5-6. Catalog facts were read on 2026-10-10; model catalogs and
terms change, so re-verify before acting on this.

## The problem

`AI_PROVIDER` defaults to `ollama` at `http://localhost:11434` with `qwen3:8b`. That is a
developer's machine. Nothing in the repository can serve production inference, and this is a
deployment blocker alongside the production URL and the Android keystore.

Two constraints shape every option:

- **No provider credential may reach Android.** The app talks only to this backend. Anything
  that would require a client-side key is excluded by that rule alone, not by preference.
- **Provider independence must survive.** Whatever is chosen is an adapter behind
  `AIProvider`, selected by `AI_PROVIDER`. No business logic learns the vendor's name, and no
  second provider framework is introduced.

## What the product actually needs

Taken from the audited call sites, not from a wish list:

| Capability | Needed by | Status today |
|---|---|---|
Structured output (40+ Pydantic schemas) | every AI feature | the hard requirement; `generate_structured` is the dominant call |
Text generation | a few paths | fine |
Transcription | voice interviews | local faster-whisper, dev-only |
Synthesis | voice interviews | local pyttsx3, unproven on Linux |
Tool calling | nothing yet | **not used anywhere**; needed from VA-4 |
Streaming | nothing yet | not used anywhere; needed from VA-2 |
Embeddings / rerank | nothing | a commented-out future option in `jd_service.py:86` |

The honest reading: **structured output reliability matters more than anything else today**,
and tool calling becomes a hard requirement at VA-4. A provider that is excellent at prose
and unreliable at schema-constrained JSON would be a bad fit however good its benchmarks are.

## Options

### A. Hosted text provider, local voice stays dev-only

Configure `anthropic` (already implemented) or another hosted text provider for
structured/text, and leave voice as a dev-only local path that production does not expose.

- **Cost:** metered per token. Not ₹0.
- **Effort:** none for Anthropic — the adapter exists and only needs a key.
- **Voice:** production voice interviews stay unavailable. The capability gate added in
  VA-0.5 already makes that a clean 503 rather than a failure, so this is a shippable state.
- **Risk:** low. This is the smallest step that makes text features production-capable.

### B. Hosted provider covering text *and* voice

One provider for structured output, STT and TTS, so voice ships too.

- **Groq** publishes `whisper-large-v3` and `whisper-large-v3-turbo` for STT (production
  tier) and Orpheus TTS in **preview, explicitly not for production**. Text:
  `openai/gpt-oss-120b`/`-20b`, `llama-3.3-70b-versatile`, `llama-3.1-8b-instant`, all
  131,072 context. **Blocking unknowns: pricing (the pricing page published no figures and
  `console.groq.com/docs/pricing` 404s), free-tier limits, and per-model tool-calling
  support.** Most importantly, its Privacy Policy explicitly excludes API data — retention
  and training for prompts and outputs are governed by the Services Agreement and DPA, which
  have not been read. **Student résumés, transcripts and interview answers must not be sent
  to a provider whose retention terms are unknown**, so Groq cannot be adopted until that
  document is reviewed.
- **NVIDIA** is the only catalog audited that lists streaming ASR (`nemotron-asr-streaming`),
  TTS (`magpie-tts-multilingual`, `magpie-tts-zeroshot`), a safety classifier
  (`llama-guard-4-12b`), embeddings and rerank — relevant from VA-2 onward. But the catalog
  publishes display names and slugs, **not formal API model IDs**, and no pricing, quotas,
  rate limits or retention terms. Nothing is adoptable from the catalog alone.
- **Risk:** medium-to-high until terms are read. The capability work in VA-0.5 is what makes
  a partial adoption safe: a provider that does text but not speech is now a declared,
  supported configuration rather than a latent failure.

### C. Self-hosted Ollama on a server

Move the existing provider to a host you control.

- **Cost:** no per-token cost; real hardware, GPU, uptime and maintenance cost instead.
- **Effort:** low in code — **zero adapter work**, since `OllamaProvider` already exists and
  only `OLLAMA_BASE_URL` changes.
- **Quality/latency:** `qwen3:8b` on CPU has already been measured in this project as
  frequently exceeding the 240 s timeout. A server without a capable GPU would make this
  worse, not better, and `generate_structured` is the dominant call.
- **Voice:** faster-whisper works on Linux; TTS needs `espeak-ng` and is unproven (README).
- **Risk:** medium. It is the only option that keeps recurring spend near ₹0, and the only
  one where no student data leaves infrastructure you control — which is a genuine privacy
  advantage over B, not just a cost one.

### D. FreeLLMAPI — excluded

Its own terms exclude it: *"Built for personal use only"*, single-user, *"Don't expose this
proxy publicly"*. It is a self-hosted router requiring each upstream provider's own key, with
no stated logging or retention policy and an inconsistent model count (343 vs 311 on the same
page). **Not recommended for production on terms grounds, not reliability grounds.** It is
also architecturally redundant: `AIProvider` + `AI_PROVIDER` already does provider selection.
It may be useful on a developer machine for trying candidate models before benchmarking.

## Recommendation

**Option A now, and defer the rest to VA-3 benchmarking.** It makes text features
production-capable with no new adapter, leaves voice cleanly unavailable rather than broken,
and commits to nothing. Option C is the strongest ₹0 and privacy candidate and should be
benchmarked beside A; option B stays blocked until Groq's DPA and NVIDIA's API IDs and terms
are in hand.

**No final choice is proposed here**, per the planning decision. The inputs that would settle
it are a terms review per candidate and a measured benchmark on this product's own workload —
structured-output validity rate first, then tool-call accuracy, latency and cost per completed
task — not model names or published benchmarks.

## Not in scope here

Credentials in Android (prohibited); a new provider framework (the existing abstraction is
sufficient and was confirmed so in VA-0); embeddings/rerank adoption; any migration of the
turn-based interview API.
