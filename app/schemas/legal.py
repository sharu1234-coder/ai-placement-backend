from pydantic import BaseModel


class LegalDocumentRead(BaseModel):
    version: str
    content: str
