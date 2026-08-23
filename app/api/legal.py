from pathlib import Path

from fastapi import APIRouter

from app.schemas.legal import LegalDocumentRead

router = APIRouter(prefix="/legal", tags=["legal"])

_LEGAL_DIR = Path(__file__).resolve().parent.parent / "legal"


def _read_legal_document(filename: str, version: str) -> LegalDocumentRead:
    content = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    return LegalDocumentRead(version=version, content=content)


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
