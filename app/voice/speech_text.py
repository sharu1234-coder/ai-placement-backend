"""Turn a written answer into something worth hearing, without changing what it says.

## Why this exists

`LS §13.2` forbids describing an unverified action as successful, and the same honesty applies to
speech: the words a student hears must mean what the words on screen mean. But the coach's answer
is assembled for *reading* -- `_speak_coaching` concatenates grounded claims, and a claim may carry
a job URL, a figure, or markdown emphasis that came from a job advert. Handed straight to a
synthesizer, `https://careers.example.com/jobs/48123?src=talviora` is read out character by
character, for nine seconds, in the middle of a sentence about a role.

## What it deliberately does not do

**It does not summarise, soften, or shorten.** Every number, warning, uncertainty and
qualification survives verbatim -- those are the parts a student acts on, and dropping one to save
two seconds of audio would make the spoken answer a different answer. The only substitutions are
of *notation*: syntax that exists to be seen, replaced by the shortest phrase that means the same
thing aloud. Removing a sentence is out of scope for this module by design.

**It does not add a wire field.** The protocol carries one answer text, and it still does: the
display text travels unchanged and this rendering is applied only to the string handed to the
synthesizer. A second field would need parity tests on both sides and a protocol version bump for
a presentation concern that belongs on the server.

**It does not guess at pronunciation.** No phoneme hints, no SSML, no respelling of names: the
approved recogniser and synthesizer are turn-based and plain-text, and inventing markup they do
not implement would simply be spoken aloud.
"""

from __future__ import annotations

import re

#: Said in place of a URL. A student cannot act on a spoken URL anyway -- the readable one is on
#: screen, which is what this phrase points at.
_LINK_PHRASE = "a link on your screen"

#: Scheme-qualified and bare-domain URLs. Bare domains need the trailing-path form so that an
#: ordinary sentence-ending full stop is not mistaken for a domain dot.
_URL = re.compile(r"""(?xi)
    \b(?:
        [a-z][a-z0-9+.\-]*://[^\s<>"')]+      # scheme://host/path
      | www\.[^\s<>"')]+                      # www.host/path
      | [a-z0-9][a-z0-9\-]*(?:\.[a-z0-9\-]+)+ / [^\s<>"')]*   # host.tld/path
    )
""")

#: `**bold**`, `*italic*`, `__bold__`, `_italic_`, and `` `code` `` -- the markers go, the words
#: stay. Applied to inline runs only, so a lone asterisk in prose is left alone.
_EMPHASIS = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1|(?<!\w)([*_`])(?=\S)(.+?)(?<=\S)\3(?!\w)")

#: A heading's `#` markers, and a list item's bullet. The text of both is kept: a heading is often
#: the only thing naming what follows.
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^\s{0,6}(?:[-*+]|\d{1,2}[.)])\s+", re.MULTILINE)

#: A markdown table's separator row -- all pipes, dashes and colons. Pure notation, nothing said.
_TABLE_RULE = re.compile(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", re.MULTILINE)

#: A horizontal rule.
_HR = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.MULTILINE)

#: `[label](target)` -- say the label, drop the target.
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\(([^)\n]*)\)")

#: Remaining cell dividers become sentence pauses, so a row is not run together into one phrase.
_PIPES = re.compile(r"[ \t]*\|[ \t]*")

_BLANK_RUN = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]{2,}")


#: Pictographic characters, which have no spoken form.
#:
#: Added when the conversational responder shipped. Until then every spoken string came from the
#: grounded renderer, which assembles sentences from recorded claims and cannot emit an emoji --
#: so `for_speech` only ever had to deal with markdown notation. A model can and does: asked for
#: a greeting, the configured model returned "Hey there! Glad you asked \U0001f60a", and the
#: emoji reached the synthesizer, where SAPI5 either skips it or vocalises something that is not
#: a word.
#:
#: Scoped to pictographic blocks rather than "non-ASCII", deliberately. Stripping non-ASCII would
#: destroy Devanagari, which matters because the product serves students in India and Hindi is a
#: supported language of the premium voice under evaluation. Accented Latin text is likewise
#: left intact.
_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f900-\U0001f9ff"  # supplemental symbols & pictographs
    "\U0001fa70-\U0001faff"  # symbols & pictographs extended-A
    "☀-⛿"          # miscellaneous symbols
    "✀-➿"          # dingbats
    "️"                 # variation selector-16, the emoji presentation marker
    "‍"                 # zero-width joiner, which binds emoji sequences
    "]+"
)


def for_speech(text: str) -> str:
    """Render `text` for a synthesizer. Meaning preserved; notation removed.

    Idempotent: rendering an already-rendered string returns it unchanged, which matters because
    the caller cannot always tell whether a responder already did this.
    """
    if not text or not text.strip():
        return ""

    out = text

    # Links first: a markdown link's label is worth saying, and doing this before the bare-URL
    # pass stops the target inside the parentheses being spoken.
    out = _MD_LINK.sub(lambda m: m.group(1), out)
    out = _URL.sub(_LINK_PHRASE, out)

    out = _TABLE_RULE.sub("", out)
    out = _HR.sub("", out)
    out = _HEADING.sub("", out)
    out = _BULLET.sub("", out)
    out = _EMOJI.sub("", out)

    # Emphasis can nest (`**bold with _italic_**`), so apply until it settles rather than once.
    for _ in range(3):
        collapsed = _EMPHASIS.sub(lambda m: m.group(2) or m.group(4), out)
        if collapsed == out:
            break
        out = collapsed

    out = _PIPES.sub(". ", out)

    # A blank line is a spoken pause, not a silent gap: one sentence break reads as intended by a
    # turn-based synthesizer, which has no notion of paragraphs.
    out = _BLANK_RUN.sub(". ", out)
    out = out.replace("\n", " ")
    out = _SPACES.sub(" ", out)

    # The substitutions above can leave doubled punctuation at a boundary ("result.. Next").
    out = re.sub(r"\.\s*\.(\s|$)", r".\1", out)
    out = re.sub(r"\s+([,.;:!?])", r"\1", out)
    out = re.sub(r"([,;:])\s*\.", r"\1", out)

    return out.strip(" .\t").strip() + "." if out.strip(" .\t") else ""
