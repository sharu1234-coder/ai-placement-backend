# Correction notice — `PROJECT_HANDOFF.md` status

**What this is:** a separate, reviewable correction to the status claims in
`../../PROJECT_HANDOFF.md` (workspace root). It exists because that file's status is stale in a
way that misleads anyone who reads it first — which it instructs them to do.

**What this is not:** a rewrite. The original file is **not** overwritten, not reordered and not
edited beyond a single pointer banner inserted above its first line, which changes no existing
sentence. Its wording, structure and the record of what was planned are preserved exactly.

**Why the correction lives here rather than there.** The workspace root is **not a git
repository** (`git rev-parse --is-inside-work-tree` → `fatal: not a git repository`), and
`PROJECT_HANDOFF.md` is tracked by neither repo (`git ls-files` in both returns nothing). So a
change made there has no diff, no history and no review trail — the exact condition finding D5
was opened about. This file is inside a repository, so this correction does.

---

## 1. The claim that is wrong

> *"...and the approved V1 architecture. **No implementation has started yet.**"*
> — `PROJECT_HANDOFF.md`, paragraph 2

That sentence was true when it was written and is now substantially false. As of **2026-10-13**
both repositories carry a large implemented system across milestones M1–M11, two feature
branches with extensive uncommitted work, and a backend test suite in the thousands.

The sentence should be read as a **historical preamble describing the document's own moment of
authorship**, not as a current status. It has been left in place rather than deleted, because
deleting it would destroy the only record of when this document was written.

## 2. The claim that is a scope conflict, not an error

`PROJECT_HANDOFF.md` §"V1 flow" names **Voice Interview** as a V1 step. It does **not** mention:

- a realtime voice agent (VA-2 / VA-12),
- a Career Coach conversation (VA-8),
- write-actions with user confirmation (VA-7).

All three were built. This is recorded as an unresolved conflict, **not reconciled**: it is not
for an implementer to decide that the shipped scope retroactively defines V1. See
[`voice-agent-locked-spec.md`](voice-agent-locked-spec.md) §12, conflict 1.

**Owner decision required:** is the V1 flow in `PROJECT_HANDOFF.md` still the V1 flow, with
these three treated as post-V1 work, or is it revised to include them?

## 3. Current status, stated plainly

| | |
|---|---|
| Implementation | M1–M11 built across both repos; two feature branches with uncommitted work |
| Voice Agent | both halves connected; server half exercised end to end with real inference on one Windows host |
| Physical device | **no acceptance of any kind.** `adb devices -l` is empty. No emulator substituted |
| Speech providers | development-only (faster-whisper, pyttsx3); production defaults `none` and fails closed; ElevenLabs excluded; **no production provider approved** |
| Live job-market data | `job_provider` is `none`; the agent states the absence rather than fabricating listings |
| Measured latency / accuracy / audio quality | **none exists** for the spoken path |
| Deployment | nothing committed, pushed, signed, published or deployed |

## 4. Where the authoritative records now are

| Subject | File |
|---|---|
| VA-0 … VA-12 requirements and their provenance | `docs/voice-agent-locked-spec.md` |
| Per-requirement status, with the seven states kept distinct | `docs/voice-agent-acceptance-matrix.md` |
| Device acceptance procedure and what is unexercised | `ai-placement-android/VOICE_DEVICE_ACCEPTANCE_VA11.md` |
| The audit that opened D1–D5 | `docs/va12-final-audit.md` |

## 5. Still open, and still the owner's call

1. **Which document is authoritative** for product scope. The recommendation from
   `va12-final-audit.md` §11 stands: promote `PROJECT_HANDOFF.md` into a repository (e.g.
   `docs/PRODUCT_SPEC.md`) so that it has a history.
2. **Whether the V1 flow stands** (§2 above).
3. **The normative Locked Spec / Development Plan and Final Master Specification are readable
   but unversioned.** Corrected 2026-10-14: they were found on this machine, inspected, and
   hashed, so the locked spec now carries real `LS §n` citations — see its §0. They still sit
   outside both repositories, so they have no history, no diff and no review trail, which is the
   unclosed remainder of D5. Whether to place them under version control is the owner's call.
