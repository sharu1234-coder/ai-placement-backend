import re
from typing import Dict, List, Optional, Tuple

# Canonical section key -> heading synonyms an ATS-style plain-text scan would recognize.
# Deliberately regex/heading-based, not dependent on the upload-time AI parse, since this
# doubles as the "does an ATS see a heading called X" signal for the parse-check UI.
SECTION_SYNONYMS: Dict[str, List[str]] = {
    "summary": ["summary", "objective", "profile", "about me", "professional summary", "career objective"],
    "skills": ["skills", "technical skills", "core competencies", "key skills"],
    "experience": ["experience", "work experience", "professional experience", "employment history"],
    "education": ["education", "academic background", "academic qualifications"],
    "projects": ["projects", "academic projects", "personal projects"],
    "certifications": ["certifications", "certificates", "licenses"],
    "achievements": ["achievements", "accomplishments", "awards", "honors"],
    "languages": ["languages", "language proficiency"],
}

_SYNONYM_TO_CANONICAL: Dict[str, str] = {
    synonym: canonical for canonical, synonyms in SECTION_SYNONYMS.items() for synonym in synonyms
}


def _build_heading_pattern() -> "re.Pattern[str]":
    # Longest-first so "technical skills" matches before the shorter "skills" alternative.
    ordered = sorted(_SYNONYM_TO_CANONICAL.keys(), key=len, reverse=True)
    escaped = [re.escape(s) for s in ordered]
    # A heading line: starts with a known synonym, allows a short trailing decoration
    # (e.g. "SKILLS & TOOLS", "TECHNICAL SKILLS:") but the whole line must stay short --
    # this is what keeps the match from firing on the word appearing inside a sentence.
    return re.compile(r"(?im)^\s*(" + "|".join(escaped) + r")\b[\sA-Za-z&,/-]{0,20}:?\s*$")


_HEADING_PATTERN = _build_heading_pattern()


def split_into_sections(raw_text: str) -> Dict[str, str]:
    """Splits resume raw_text into canonical sections by heading-line detection. Returns
    only sections whose heading was actually found; each value is the text between that
    heading and the next detected heading (or end of document). First occurrence wins if
    a heading word appears more than once (e.g. "Skills" mentioned again in a summary)."""
    lines = raw_text.splitlines()

    matches: List[Tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _HEADING_PATTERN.match(line)
        if not m:
            continue
        canonical = _SYNONYM_TO_CANONICAL.get(m.group(1).lower())
        if canonical is not None:
            matches.append((i, canonical))

    sections: Dict[str, str] = {}
    for idx, (line_no, key) in enumerate(matches):
        if key in sections:
            continue
        start = line_no + 1
        end = matches[idx + 1][0] if idx + 1 < len(matches) else len(lines)
        sections[key] = "\n".join(lines[start:end]).strip()
    return sections


def heading_lines(raw_text: str) -> List[str]:
    """The actual raw text of every detected heading line (not just its canonical key) --
    used by readability checks to judge capitalization consistency across headings."""
    return [line.strip() for line in raw_text.splitlines() if _HEADING_PATTERN.match(line)]


def first_heading_line(raw_text: str) -> Optional[int]:
    """Line index of the first detected section heading, or None if no heading was found
    at all. Used to bound the "contact block" -- everything before the first real section
    heading, where contact info conventionally lives."""
    lines = raw_text.splitlines()
    for i, line in enumerate(lines):
        if _HEADING_PATTERN.match(line):
            return i
    return None
