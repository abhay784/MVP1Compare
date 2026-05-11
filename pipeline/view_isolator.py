"""Per-view crop isolation — erase foreign content instead of moving bbox edges.

Companion to (and intended replacement for) `pipeline.bbox_snap`. The LLM is
now asked to emit deliberately generous, possibly-overlapping view bboxes. To
keep the per-view comparator focused on this-view content, this module:

  1. Assigns every text token (and, on vector PDFs, every drawing primitive)
     to a single owning view via greatest-intersection-area (same rule as
     `bbox_snap._assign_ownership`).
  2. For each view V, takes the full bbox crop and white-outs the pixel
     rectangle of every element owned by some OTHER view U ≠ V whose bbox
     overlaps V. The bbox itself is NOT moved.

The bboxes that get persisted in `view_metadata.bbox` and `match.*_bbox_norm`
are still the LLM's original bboxes — only the *image bytes* of the crop are
subtracted. Downstream code (highlighter, change_locator) that works in
PDF-point space therefore sees unchanged geometry.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import fitz
import numpy as np

from config import config
from pipeline.bbox_snap import _assign_ownership
from pipeline.parser import ParsedPage

if TYPE_CHECKING:
    from pipeline.llm_segmenter import LLMMatch

log = logging.getLogger(__name__)

Bbox = tuple[float, float, float, float]   # (x0, y0, x1, y1) in PDF points


# ---------------------------------------------------------------------------
# Element collection
# ---------------------------------------------------------------------------

def _collect_elements_for_page(
    page: ParsedPage,
    pdf_bytes: bytes,
    page_index: int,
) -> list[Bbox]:
    """Gather ownership-candidate bboxes (in PDF points) for one page.

    On vector PDFs: text tokens + drawing primitives, with tiny primitives
    filtered out. On raster PDFs: text tokens only.
    """
    elements: list[Bbox] = [
        (tb.bbox_x0, tb.bbox_y0, tb.bbox_x1, tb.bbox_y1)
        for tb in page.text_blocks
    ]

    if page.is_raster:
        return elements

    # Vector primitives — small dashes / hatch fills are noise; filter by area.
    page_area = max(page.page_width_pt * page.page_height_pt, 1.0)
    min_area = page_area * 0.0001  # 0.01% of the page

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        drawings = doc[page_index].get_drawings()
        doc.close()
    except Exception as exc:  # defensive — never let drawing collection break the run
        log.warning("get_drawings() failed on page %d: %s", page_index, exc)
        drawings = []

    for d in drawings:
        rect = d.get("rect")
        if rect is None:
            continue
        if rect.width <= 0 or rect.height <= 0:
            continue
        if rect.width * rect.height < min_area:
            continue
        elements.append((rect.x0, rect.y0, rect.x1, rect.y1))

    return elements


# ---------------------------------------------------------------------------
# Pixel erasure
# ---------------------------------------------------------------------------

def _erase_in_crop(
    crop: np.ndarray,
    crop_bbox_pt: Bbox,
    foreign_elements_pt: list[Bbox],
    page_w_pt: float,
    page_h_pt: float,
    pad_px: int = 2,
) -> np.ndarray:
    """White-out foreign element rectangles inside a crop.

    `crop_bbox_pt` is the view's PDF-point bbox; the crop's pixel coordinate
    system has its origin at (crop_bbox_pt.x0, crop_bbox_pt.y0).
    """
    if crop.size == 0 or not foreign_elements_pt:
        return crop

    out = crop.copy()
    h, w = out.shape[:2]
    cx0, cy0, cx1, cy1 = crop_bbox_pt
    crop_w_pt = max(cx1 - cx0, 1e-6)
    crop_h_pt = max(cy1 - cy0, 1e-6)

    sx = w / crop_w_pt   # px per PDF point, x
    sy = h / crop_h_pt   # px per PDF point, y

    for (ex0, ey0, ex1, ey1) in foreign_elements_pt:
        # Clip element rect to crop rect, in PDF points.
        ix0 = max(ex0, cx0)
        iy0 = max(ey0, cy0)
        ix1 = min(ex1, cx1)
        iy1 = min(ey1, cy1)
        if ix1 <= ix0 or iy1 <= iy0:
            continue

        # Map to crop-local pixel space.
        px0 = int(round((ix0 - cx0) * sx)) - pad_px
        py0 = int(round((iy0 - cy0) * sy)) - pad_px
        px1 = int(round((ix1 - cx0) * sx)) + pad_px
        py1 = int(round((iy1 - cy0) * sy)) + pad_px
        px0 = max(0, px0); py0 = max(0, py0)
        px1 = min(w, px1); py1 = min(h, py1)
        if px1 <= px0 or py1 <= py0:
            continue

        out[py0:py1, px0:px1] = 255  # white-out (RGB or grayscale both fine)

    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

@dataclass
class _PageMatchView:
    match_index: int
    bbox_pt: Bbox


def _crop_pixels(image: np.ndarray, bbox_norm: tuple[float, float, float, float]) -> np.ndarray:
    h, w = image.shape[:2]
    x0, y0, x1, y1 = bbox_norm
    px0 = max(0, int(round(x0 * w)))
    py0 = max(0, int(round(y0 * h)))
    px1 = min(w, int(round(x1 * w)))
    py1 = min(h, int(round(y1 * h)))
    if px1 <= px0 or py1 <= py0:
        return np.zeros((1, 1, 3), dtype=image.dtype)
    return image[py0:py1, px0:px1]


def isolate_match_crops(
    matches: list["LLMMatch"],
    pages: list[ParsedPage],
    page_dims_pt: list[tuple[float, float]],
    pdf_bytes: bytes,
    side: str,                                    # "original" | "revised"
) -> dict[int, np.ndarray]:
    """Build isolated (foreign-erased) crops for every match on one side.

    Returns a dict keyed by match index in `matches` whose value is the
    isolated crop (numpy RGB). Matches that have no bbox on this side
    (added/removed) are absent from the returned dict.
    """
    bbox_attr = "orig_bbox_norm" if side == "original" else "rev_bbox_norm"

    isolated: dict[int, np.ndarray] = {}

    # Group matches by page — ownership is a per-page calculation.
    by_page: dict[int, list[_PageMatchView]] = {}
    for mi, m in enumerate(matches):
        bbox_norm = getattr(m, bbox_attr)
        if bbox_norm is None:
            continue
        page_w_pt, page_h_pt = page_dims_pt[m.page_index]
        if page_w_pt <= 0 or page_h_pt <= 0:
            continue
        x0n, y0n, x1n, y1n = bbox_norm
        bbox_pt: Bbox = (
            x0n * page_w_pt, y0n * page_h_pt,
            x1n * page_w_pt, y1n * page_h_pt,
        )
        by_page.setdefault(m.page_index, []).append(_PageMatchView(mi, bbox_pt))

    for pi, page_matches in by_page.items():
        page = pages[pi]
        page_w_pt, page_h_pt = page_dims_pt[pi]

        view_bboxes: list[Bbox] = [pm.bbox_pt for pm in page_matches]
        elements: list[Bbox] = _collect_elements_for_page(page, pdf_bytes, pi)

        if not elements:
            # Nothing to erase — fall back to plain crops.
            for pm in page_matches:
                bbox_norm = getattr(matches[pm.match_index], bbox_attr)
                isolated[pm.match_index] = _crop_pixels(page.image, bbox_norm)
            continue

        owners = _assign_ownership(view_bboxes, elements)

        for vi, pm in enumerate(page_matches):
            foreign = [
                e for e, o in zip(elements, owners)
                if o != -1 and o != vi
            ]
            bbox_norm = getattr(matches[pm.match_index], bbox_attr)
            crop = _crop_pixels(page.image, bbox_norm)
            isolated[pm.match_index] = _erase_in_crop(
                crop, pm.bbox_pt, foreign, page_w_pt, page_h_pt,
            )

    log.info(
        "View isolator (%s): produced %d isolated crop(s) across %d page(s)",
        side, len(isolated), len(by_page),
    )
    return isolated
