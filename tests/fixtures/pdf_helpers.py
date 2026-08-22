import fitz


def make_pdf_with_text(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    return doc.tobytes()


def make_empty_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    return doc.tobytes()
