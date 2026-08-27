from dataclasses import dataclass
from typing import List, Optional

import fitz

_SMALL_TEXT_THRESHOLD_PT = 8.0
_MULTI_COLUMN_MIN_CLUSTER_GAP = 50.0  # points -- gap between x0 clusters to call it multi-column
_MULTI_COLUMN_MIN_CLUSTER_SIZE = 3  # ignore a lone stray span (e.g. one indented bullet)
_MULTI_COLUMN_BALANCE_RATIO = 0.3  # second cluster must be a real fraction of the first,
# not just present -- filters out a short right-aligned date sharing a row with body text


@dataclass
class LayoutSignals:
    page_count: int
    is_multi_column: bool
    has_tables: bool
    distinct_font_count: int
    distinct_size_count: int
    small_text_ratio: float
    has_images: bool
    image_count: int


def extract_layout_signals(pdf_bytes: bytes) -> Optional[LayoutSignals]:
    """Re-extracts page/font/table/image layout data from the stored PDF bytes for
    formatting checks that plain extracted text can't answer (multi-column layout, tables,
    small text, embedded images, font consistency). Returns None if the PDF can't be
    reopened -- callers must degrade gracefully (checks/parsing.py falls back to a
    text-only heuristic set when this is None), never crash."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None

    try:
        all_x0: List[float] = []
        font_names = set()
        font_sizes = set()
        small_spans = 0
        total_spans = 0
        table_found = False
        image_count = 0

        for page in doc:
            page_dict = page.get_text("dict")
            for block in page_dict.get("blocks", []):
                if block.get("type") != 0:  # 0 = text block (1 = image block)
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        # Span-level x0, not block-level: PyMuPDF merges same-row text
                        # into one block spanning the full row width, so a block's own
                        # bbox[0] is always the leftmost span regardless of how many
                        # separate columns share that row -- only per-span x0 actually
                        # reveals a column split.
                        span_bbox = span.get("bbox")
                        if span_bbox:
                            all_x0.append(span_bbox[0])
                        total_spans += 1
                        font_names.add(span.get("font", ""))
                        size = span.get("size", 0)
                        if size:
                            font_sizes.add(round(size))
                            if size < _SMALL_TEXT_THRESHOLD_PT:
                                small_spans += 1

            image_count += len(page.get_images())

            try:
                tables = page.find_tables()
                if tables.tables:
                    table_found = True
            except Exception:
                pass  # find_tables can raise on malformed pages -- not fatal to the rest

        return LayoutSignals(
            page_count=doc.page_count,
            is_multi_column=_detect_multi_column(all_x0),
            has_tables=table_found,
            distinct_font_count=len(font_names),
            distinct_size_count=len(font_sizes),
            small_text_ratio=(small_spans / total_spans) if total_spans else 0.0,
            has_images=image_count > 0,
            image_count=image_count,
        )
    finally:
        doc.close()


def _detect_multi_column(x0_values: List[float]) -> bool:
    """Clusters span left-edges; a single-column resume's spans nearly all start near the
    same left margin, while a multi-column layout produces two distinct, recurring x0
    clusters far enough apart to be separate columns. Requires the second cluster to be a
    real, substantial fraction of the first, not just present -- a very common
    single-column pattern (a right-aligned date on the same line as a job title) also
    produces a second x0 cluster, but with far fewer spans than genuine column content, so
    a bare "2+ clusters" test would false-positive on it constantly."""
    if len(x0_values) < 6:
        return False
    sorted_x0 = sorted(x0_values)
    clusters: List[List[float]] = [[sorted_x0[0]]]
    for x in sorted_x0[1:]:
        if x - clusters[-1][-1] > _MULTI_COLUMN_MIN_CLUSTER_GAP:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    clusters.sort(key=len, reverse=True)
    if len(clusters) < 2:
        return False
    largest, second = len(clusters[0]), len(clusters[1])
    return second >= _MULTI_COLUMN_MIN_CLUSTER_SIZE and second >= largest * _MULTI_COLUMN_BALANCE_RATIO
