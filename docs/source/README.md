# Normative source documents — Voice Agent

Byte-identical copies of the owner's specification PDFs, placed here so that the acceptance
documents can cite a path inside the repository instead of a file on one developer's desktop.

## Hierarchy (owner decision, given — not inferred here)

| # | File | Role |
|---|---|---|
| 1 | `01-locked-spec-and-development-plan.NORMATIVE.pdf` | **Normative** source for the VA-0 … VA-12 roadmap and its gates. Cited as **LS** elsewhere. |
| 2 | `02-final-master-specification.SUPPORTING.pdf` | **Supporting** reference. Cited as **FMS**. |
| 3 | `03-master-specification.SUPERSEDED.pdf` | **SUPERSEDED** by #2. Retained for history. **Do not cite it as a requirement.** |

Where #1 and #2 disagree, #1 governs. Where #3 disagrees with either, #3 is simply out of date —
and no attempt has been made here to reconcile the three. Conflicts that matter are recorded in
[`../voice-agent-locked-spec.md`](../voice-agent-locked-spec.md) §12, not resolved.

## Integrity

Verified 2026-10-14. Each copy's SHA-256 was computed after copying and matches the original:

| File | SHA-256 | Pages |
|---|---|---|
| `01-locked-spec-and-development-plan.NORMATIVE.pdf` | `d96f0711aaad8f90b491cbbe9d913a7d66cd7714d39080456d5148a0c9d5bd29` | 24 |
| `02-final-master-specification.SUPPORTING.pdf` | `cdb9d609a61be7c2cc2188c1050ba64abc470c10543c6e3fb0632229353f19d5` | 24 |
| `03-master-specification.SUPERSEDED.pdf` | `b4af3f3bd44d33a0851f87c5faca9cf0b121240528695f9a63dfbad1838aa591` | 18 |

Re-verify with:

```
sha256sum docs/source/*.pdf
```

The originals remain in place at `~/OneDrive/Desktop/Downloads/AI_Placement_OS_Voice_Agent_*.pdf`
and were not moved, renamed or modified.

## Status of these files

**Present in the working tree, and uncommitted.** A file existing here is not version history: at
the time of writing these are untracked additions on the `professional-presence-intelligence`
branch, with no commit, no diff and no review trail. That was the substance of finding D5, and it
is narrowed rather than closed — citations are now reproducible from the repository, but the
documents still have no history until someone commits them. Doing so is the owner's call.

## A note on reading them

The pages are CID-encoded under subset fonts, so copy-paste from some viewers yields nothing
usable and naive text extraction yields font-program noise. The section and page citations used
in the acceptance documents were produced by decoding each page through its embedded `ToUnicode`
CMap and walking the `/Pages` `/Kids` tree for true page order — every glyph in all three files
resolved, with none unmapped. Page numbers in citations are **1-based positions in that tree**,
which is what a viewer's page counter shows; they are not the printed folio, which these
documents do not carry.
