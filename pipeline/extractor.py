"""Dimension and structured-text extraction — Stage 4 of the DrawDiff pipeline.

Operates on the text layer produced by parser.py (or OCR output for rasters).
Extracts:
  - Dimensions: numeric values matching engineering drawing notation
  - Title block: part number, revision, material, tolerance, etc.
  - Revision block: change history from the top-right sheet corner
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

import fitz

from pipeline.parser import TextBlock


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class Dimension:
    value: str              # raw matched string, e.g. "∅12.5±0.1"
    numeric: Optional[float]  # parsed centre value; None if unparseable
    bbox_x0: float
    bbox_y0: float
    bbox_x1: float
    bbox_y1: float


@dataclass
class TitleBlock:
    part_number: Optional[str] = None
    revision: Optional[str] = None
    material: Optional[str] = None
    tolerance: Optional[str] = None
    drawn_by: Optional[str] = None
    date: Optional[str] = None
    raw_text: str = ""


@dataclass
class RevisionBlock:
    entries: list[dict] = field(default_factory=list)  # [{rev, description, date, by}]
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Dimension regex (from PRD §7.5)
# ---------------------------------------------------------------------------

_DIM_PATTERN = re.compile(
    r"^[∅Rr]?"                          # optional diameter / radius prefix
    r"\d+\.?\d*"                         # integer or decimal mantissa
    r"(?:[/\-]\d+\.?\d*)?"              # optional fraction or second tolerance leg
    r"(?:\s*[±+\-]\s*\d+\.?\d*)?$"     # optional ± tolerance
)

_NUMERIC_STRIP = re.compile(r"[∅Rr±+\-\s]")


def _try_parse_numeric(value: str) -> Optional[float]:
    """Extract the centre numeric value from a raw dimension string."""
    stripped = _NUMERIC_STRIP.sub("", value.split("/")[0].split("-")[0])
    try:
        return float(stripped)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public extraction functions
# ---------------------------------------------------------------------------

def extract_dimensions(text_blocks: list[TextBlock]) -> list[Dimension]:
    """Match dimension values in a list of text blocks.

    Each block is tested individually because PDF text extraction preserves
    word boundaries — multi-word strings rarely form a single dimension token.

    Returns:
        List of Dimension objects ordered by position (top-to-bottom).
    """
    dims: list[Dimension] = []
    for tb in text_blocks:
        token = tb.text.strip()
        if _DIM_PATTERN.match(token):
            dims.append(Dimension(
                value=token,
                numeric=_try_parse_numeric(token),
                bbox_x0=tb.bbox_x0,
                bbox_y0=tb.bbox_y0,
                bbox_x1=tb.bbox_x1,
                bbox_y1=tb.bbox_y1,
            ))
    dims.sort(key=lambda d: (d.bbox_y0, d.bbox_x0))
    return dims


def extract_title_block(pdf_bytes: bytes, page_index: int = 0) -> TitleBlock:
    """Extract structured fields from the bottom-right title block region.

    The title block is assumed to occupy the bottom-right 35% × 20% of the sheet,
    which covers standard ASME Y14.1 / ISO 7200 layouts.

    Uses raw pymupdf text extraction so this works on vector PDFs without OCR.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_index]
    r = page.rect

    tb_rect = fitz.Rect(r.width * 0.65, r.height * 0.80, r.width, r.height)
    raw = page.get_textbox(tb_rect)
    doc.close()

    patterns = {
        "part_number": r"(?:PART\s*(?:NO|NUMBER|#)?)[:\s]+([A-Z0-9\-]+)",
        "revision":    r"(?:REV(?:ISION)?)[:\s]+([A-Z0-9]+)",
        "material":    r"(?:MATERIAL)[:\s]+(.+?)(?:\n|$)",
        "tolerance":   r"(?:TOLERANC(?:E|ES))[:\s]+(.+?)(?:\n|$)",
        "drawn_by":    r"(?:DRAWN\s*BY|DRN)[:\s]+([A-Z\s]+?)(?:\n|$)",
        "date":        r"(?:DATE)[:\s]+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
    }

    result: dict[str, Optional[str]] = {}
    for field_name, pattern in patterns.items():
        m = re.search(pattern, raw, re.IGNORECASE)
        result[field_name] = m.group(1).strip() if m else None

    return TitleBlock(**result, raw_text=raw)


def extract_revision_block(pdf_bytes: bytes, page_index: int = 0) -> RevisionBlock:
    """Extract the revision history table from the top-right corner of the sheet.

    Revision blocks in ASME drawings typically occupy the top-right 30% × 25%.
    Each row has the form: REV | DESCRIPTION | DATE | APPROVED.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_index]
    r = page.rect

    rev_rect = fitz.Rect(r.width * 0.70, 0, r.width, r.height * 0.25)
    raw = page.get_textbox(rev_rect)
    doc.close()

    # Parse rows: a revision line starts with a single letter or number
    row_pattern = re.compile(
        r"^([A-Z0-9])\s+(.+?)\s+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+([A-Z\s]+)?$",
        re.MULTILINE,
    )

    entries = []
    for m in row_pattern.finditer(raw):
        entries.append({
            "rev":         m.group(1),
            "description": m.group(2).strip(),
            "date":        m.group(3),
            "by":          m.group(4).strip() if m.group(4) else None,
        })

    return RevisionBlock(entries=entries, raw_text=raw)
