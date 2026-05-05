"""Drawing-zone (chess-board) reference helper.

Engineering drawings typically have a grid printed in their borders: letters
along the vertical edges, numbers along the horizontal edges. A zone like
``B5`` identifies a roughly one-cell region of the sheet — used in revision
history (``"Bore changed in zone C3"``) and in this tool's change tables.

Defaults follow ASME Y14.1-style C/D-size conventions:
  * 8 columns labeled 1..8, increasing RIGHT-TO-LEFT (column 1 is on the right edge)
  * 4 rows    labeled A..D, increasing TOP-TO-BOTTOM   (row A is on the top edge)
  * zone format: ``"<ROW><COL>"`` (e.g. ``"B5"``)

If your drawings use a different border convention (e.g. A-size 4×4, or
ISO numbering), edit the four module constants below.
"""
from __future__ import annotations

from typing import Iterable, Optional

ZONE_COLS = 8
ZONE_ROWS = 4
COLS_RIGHT_TO_LEFT = True   # True = column 1 sits on the right edge (ASME)
ROWS_TOP_TO_BOTTOM = True   # True = row A sits on the top edge


def _col_label(col_idx: int) -> str:
    """0-indexed column → '1'..'N'."""
    return str(col_idx + 1)


def _row_label(row_idx: int) -> str:
    """0-indexed row → 'A'..'Z' (wraps to 'AA' style after Z)."""
    if row_idx < 26:
        return chr(ord("A") + row_idx)
    # Two-letter fallback for >26 rows. Not expected for any real sheet.
    a, b = divmod(row_idx, 26)
    return chr(ord("A") + a - 1) + chr(ord("A") + b)


def zone_for_point(x_frac: float, y_frac: float) -> str:
    """Zone label (e.g. "B5") for a normalized [0,1] point on the page."""
    if not (0.0 <= x_frac <= 1.0 and 0.0 <= y_frac <= 1.0):
        x_frac = min(1.0, max(0.0, x_frac))
        y_frac = min(1.0, max(0.0, y_frac))

    col_idx = min(ZONE_COLS - 1, int(x_frac * ZONE_COLS))
    row_idx = min(ZONE_ROWS - 1, int(y_frac * ZONE_ROWS))
    if COLS_RIGHT_TO_LEFT:
        col_idx = ZONE_COLS - 1 - col_idx
    if not ROWS_TOP_TO_BOTTOM:
        row_idx = ZONE_ROWS - 1 - row_idx
    return f"{_row_label(row_idx)}{_col_label(col_idx)}"


def zone_for_bbox(
    bbox_pt: Iterable[float],
    page_w_pt: float,
    page_h_pt: float,
) -> str:
    """Single-zone label for a page-space bbox, using the bbox CENTER."""
    if page_w_pt <= 0 or page_h_pt <= 0:
        return ""
    x0, y0, x1, y1 = bbox_pt
    cx = ((float(x0) + float(x1)) / 2.0) / page_w_pt
    cy = ((float(y0) + float(y1)) / 2.0) / page_h_pt
    return zone_for_point(cx, cy)


def zone_range_for_bbox(
    bbox_pt: Iterable[float],
    page_w_pt: float,
    page_h_pt: float,
) -> str:
    """Single zone OR a range like "B4–C5" for a bbox spanning >1 cells.

    The range is given as ``<top-left zone>–<bottom-right zone>`` in image
    coordinates (NOT in zone-label order — so when columns increase
    right-to-left, the second label may have a smaller number).
    """
    if page_w_pt <= 0 or page_h_pt <= 0:
        return ""
    x0, y0, x1, y1 = bbox_pt
    z00 = zone_for_point(float(x0) / page_w_pt, float(y0) / page_h_pt)
    z11 = zone_for_point(float(x1) / page_w_pt, float(y1) / page_h_pt)
    return z00 if z00 == z11 else f"{z00}–{z11}"


def zones_for_bboxes(
    bboxes_pt: list[Iterable[float]],
    page_w_pt: float,
    page_h_pt: float,
) -> str:
    """Comma-separated unique zones for a list of bboxes (centers).

    Useful when one logical change has multiple anchor regions (e.g. a
    multi-token dimension string the text-anchor search located in pieces).
    """
    seen: list[str] = []
    for b in bboxes_pt:
        z = zone_for_bbox(b, page_w_pt, page_h_pt)
        if z and z not in seen:
            seen.append(z)
    return ", ".join(seen)
