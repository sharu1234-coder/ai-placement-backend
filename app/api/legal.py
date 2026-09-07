from html import escape
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.schemas.legal import LegalDocumentRead

router = APIRouter(prefix="/legal", tags=["legal"])

_LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"


def _read_legal_document(filename: str, version: str) -> LegalDocumentRead:
    content = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    return LegalDocumentRead(version=version, content=content)


# Minimal, dependency-free, and deliberately not a template engine: the only dynamic values
# are a title and the document body, both of which are escaped below.
_HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    margin: 0 auto; padding: 2rem 1.25rem 4rem; max-width: 42rem;
    font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }}
  pre {{ white-space: pre-wrap; word-wrap: break-word; font: inherit; margin: 0; }}
</style>
</head>
<body><pre>{body}</pre></body>
</html>
"""


def _html_document(filename: str, title: str) -> HTMLResponse:
    """Browser-readable rendering of the same versioned source file the JSON routes serve.

    Why this exists alongside the JSON: Google Play requires a privacy policy URL that a
    reviewer, and any user, can open and read in a browser. `GET /legal/privacy-policy`
    satisfies "publicly accessible" but returns `{{"version": ..., "content": ...}}`, which
    renders as a raw JSON blob with the whole document collapsed onto escaped 
 sequences --
    not a policy page. The in-app screens keep using the JSON routes; these two exist purely
    so there is a real URL to paste into Play Console and into the app's store listing.

    The body is escaped rather than interpreted: the source is a trusted repo file today, and
    escaping keeps it that way even if the text later gains a character that would otherwise
    be read as markup. `white-space: pre-wrap` preserves the paragraph breaks the plain-text
    source relies on without needing a Markdown dependency.
    """
    content = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    return HTMLResponse(_HTML_PAGE.format(title=escape(title), body=escape(content)))


@router.get("/privacy-policy", response_model=LegalDocumentRead)
def get_privacy_policy() -> LegalDocumentRead:
    """Public -- no auth. Must be readable before signup and by logged-out users (and
    typically linked from app stores), not gated behind a login. Content lives in a
    versioned file (app/legal/privacy_policy_v1.txt), the same convention this project
    already uses for AI prompts, so legal text changes don't require touching route code."""
    return _read_legal_document("privacy_policy_v1.txt", version="1")


@router.get("/terms-of-service", response_model=LegalDocumentRead)
def get_terms_of_service() -> LegalDocumentRead:
    """Public -- no auth, same reasoning as get_privacy_policy above."""
    return _read_legal_document("terms_of_service_v1.txt", version="1")


@router.get("/privacy-policy.html", response_class=HTMLResponse, include_in_schema=False)
def get_privacy_policy_html() -> HTMLResponse:
    """The URL to give Google Play. Public, no auth -- same reasoning as the JSON route."""
    return _html_document("privacy_policy_v1.txt", "AI Placement OS - Privacy Policy")


@router.get("/terms-of-service.html", response_class=HTMLResponse, include_in_schema=False)
def get_terms_of_service_html() -> HTMLResponse:
    """Public, no auth -- same reasoning as get_privacy_policy_html above."""
    return _html_document("terms_of_service_v1.txt", "AI Placement OS - Terms of Service")
