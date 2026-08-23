from pathlib import Path

from fastapi import APIRouter

from app.schemas.legal import LegalDocumentRead

router = APIRouter(prefix="/legal", tags=["legal"])

_LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"
_PRIVACY_POLICY_VERSION = "1"


@router.get("/privacy-policy", response_model=LegalDocumentRead)
def get_privacy_policy() -> LegalDocumentRead:
    """Public -- no auth. Must be readable before signup and by logged-out users (and
    typically linked from app stores), not gated behind a login. Content lives in a
    versioned file (app/legal/privacy_policy_v1.txt), the same convention this project
    already uses for AI prompts, so legal text changes don't require touching route code."""
    content = (_LEGAL_DIR / "privacy_policy_v1.txt").read_text(encoding="utf-8")
    return LegalDocumentRead(version=_PRIVACY_POLICY_VERSION, content=content)
