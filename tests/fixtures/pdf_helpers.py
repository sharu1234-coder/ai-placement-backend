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


def make_multi_column_pdf() -> bytes:
    """Two real, separately-positioned text blocks far enough apart on the x-axis to
    trigger the ats_scoring layout module's multi-column heuristic."""
    doc = fitz.open()
    page = doc.new_page()
    for i in range(4):
        page.insert_text((50, 100 + i * 20), f"Left column line {i}")
        page.insert_text((320, 100 + i * 20), f"Right column line {i}")
    return doc.tobytes()


def make_single_column_pdf_with_right_aligned_dates() -> bytes:
    """A single-column layout where several lines also carry a short right-aligned date
    fragment (a very common, legitimate resume pattern) -- must NOT be flagged as
    multi-column, since the right-hand content is nowhere near as substantial as a real
    second column."""
    doc = fitz.open()
    page = doc.new_page()
    for i in range(8):
        page.insert_text((50, 100 + i * 20), f"Experience bullet describing real work item {i}")
    for i in range(2):
        page.insert_text((450, 100 + i * 20), "2025")
    return doc.tobytes()
