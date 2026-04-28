"""View segmentation — Stage 3 of the DrawDiff pipeline.

Given a ParsedPage, detects individual drawing views (front, top, section, detail, etc.)
and returns cropped images with bounding boxes and view labels.

Two detection paths:
  Vector PDF → pymupdf rectangle geometry (fast, exact)
  Raster PDF → OpenCV morphological line detection (slower, approximate)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import fitz
import numpy as np
from PIL import Image

from config import config
from pipeline.parser import ParsedPage

log = logging.getLogger(__name__)


@dataclass
class ViewCrop:
    image: np.ndarray          # RGB crop of this view at render_dpi
    bbox_x0: float             # in PDF points (origin top-left), relative to this page
    bbox_y0: float
    bbox_x1: float
    bbox_y1: float
    view_label: str            # extracted or synthesised label ("VIEW A", "SECTION B-B", …)
    area_fraction: float       # fraction of full page area occupied by this view
    page_index: int = 0        # zero-based page number this crop came from


# ---------------------------------------------------------------------------
# Vector path: use pymupdf rectangle geometry
# ---------------------------------------------------------------------------

def _find_borders_vector(pdf_bytes: bytes, page_index: int, page_rect: fitz.Rect) -> list[fitz.Rect]:
    """Return large rectangles from vector draw commands — these are view borders."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[page_index]
    drawings = page.get_drawings()
    doc.close()

    page_area = page_rect.width * page_rect.height
    min_area = page_area * 0.05  # skip anything smaller than 5% of the page

    borders: list[fitz.Rect] = []
    for d in drawings:
        rect = d["rect"]
        if rect is None:
            continue
        rect_area = rect.width * rect.height
        width_ok = rect.width > 0 and rect.height > 0
        aspect = rect.width / rect.height if rect.height else 0
        if rect_area >= min_area and width_ok and 0.2 < aspect < 5.0:
            borders.append(rect)

    # Deduplicate near-identical rectangles (some CAD exporters emit duplicates)
    unique: list[fitz.Rect] = []
    for r in borders:
        if not any(abs(r.x0 - u.x0) < 5 and abs(r.y0 - u.y0) < 5 for u in unique):
            unique.append(r)

    return unique


# ---------------------------------------------------------------------------
# Raster path: OpenCV line detection
# ---------------------------------------------------------------------------

def _find_borders_raster(image: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Detect view borders from pixel data using morphological line detection.

    Returns list of (x0, y0, x1, y1) tuples in pixel coordinates.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    _, binary = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (80, 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 80))
    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)

    grid = cv2.add(h_lines, v_lines)
    contours, _ = cv2.findContours(grid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    h, w = image.shape[:2]
    min_w, min_h = w * 0.08, h * 0.08

    boxes: list[tuple[int, int, int, int]] = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw >= min_w and bh >= min_h:
            boxes.append((x, y, x + bw, y + bh))

    return boxes


# ---------------------------------------------------------------------------
# Label extraction from a view crop
# ---------------------------------------------------------------------------

def _extract_label_from_crop(crop_image: np.ndarray, text_blocks_in_region, *, above_bbox_y0: float) -> str:
    """Try to find a view label ('VIEW A', 'SECTION B-B', 'DETAIL C') in the text
    blocks that sit just above the crop (within 30 PDF points) or inside the top
    15% of the crop itself.

    Falls back to an empty string if nothing matches.
    """
    import re
    label_pattern = re.compile(
        r"\b(VIEW|SECTION|DETAIL|SCALE|FRONT|TOP|SIDE|ISO|RIGHT|LEFT|BOTTOM|PLAN|ELEVATION)\b",
        re.IGNORECASE,
    )

    candidates: list[str] = []
    for tb in text_blocks_in_region:
        if label_pattern.search(tb.text):
            candidates.append(tb.text)

    return " ".join(candidates[:4]).strip() if candidates else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def segment_views(
    page: ParsedPage,
    pdf_bytes: bytes,
    page_index: int = 0,
) -> list[ViewCrop]:
    """Detect and crop individual views from a parsed drawing page.

    Args:
        page: ParsedPage produced by parser.parse_pdf().
        pdf_bytes: Original PDF bytes (needed for vector-path border detection).
        page_index: Zero-based page number.

    Returns:
        List of ViewCrop objects, sorted by top-left position (reading order).
        Views smaller than 5% of page area are omitted.
    """
    h_px, w_px = page.image.shape[:2]
    page_area_pt = page.page_width_pt * page.page_height_pt

    scale = config.render_dpi / 72.0  # pt → px conversion

    crops: list[ViewCrop] = []

    if not page.is_raster:
        page_rect = fitz.Rect(0, 0, page.page_width_pt, page.page_height_pt)
        borders = _find_borders_vector(pdf_bytes, page_index, page_rect)

        if not borders:
            log.info("Vector border detection found no rectangles; falling back to raster path")
            borders = None

        if borders:
            for rect in borders:
                area_fraction = (rect.width * rect.height) / max(page_area_pt, 1)
                if area_fraction < 0.05:
                    continue

                # Convert PDF points → pixel coordinates
                px0 = int(rect.x0 * scale)
                py0 = int(rect.y0 * scale)
                px1 = int(rect.x1 * scale)
                py1 = int(rect.y1 * scale)

                px0 = max(0, px0); py0 = max(0, py0)
                px1 = min(w_px, px1); py1 = min(h_px, py1)

                crop = page.image[py0:py1, px0:px1]
                if crop.size == 0:
                    continue

                # Collect text blocks near the top of this crop for label extraction
                top_threshold = rect.y0 + (rect.height * 0.15)
                nearby_blocks = [
                    tb for tb in page.text_blocks
                    if rect.x0 <= tb.bbox_x0 <= rect.x1 and tb.bbox_y0 <= top_threshold
                ]
                # Also grab blocks just above the rectangle
                above_blocks = [
                    tb for tb in page.text_blocks
                    if rect.x0 <= tb.bbox_x0 <= rect.x1 and rect.y0 - 30 <= tb.bbox_y0 < rect.y0
                ]
                label = _extract_label_from_crop(crop, nearby_blocks + above_blocks, above_bbox_y0=rect.y0)

                crops.append(ViewCrop(
                    image=crop,
                    bbox_x0=rect.x0,
                    bbox_y0=rect.y0,
                    bbox_x1=rect.x1,
                    bbox_y1=rect.y1,
                    view_label=label,
                    area_fraction=area_fraction,
                    page_index=page_index,
                ))

    if page.is_raster or not crops:
        # Raster path: work purely in pixel space
        boxes = _find_borders_raster(page.image)
        page_area_px = w_px * h_px

        for (x0, y0, x1, y1) in boxes:
            area_fraction = ((x1 - x0) * (y1 - y0)) / max(page_area_px, 1)
            if area_fraction < 0.05:
                continue

            crop = page.image[y0:y1, x0:x1]
            if crop.size == 0:
                continue

            # Convert pixel bbox back to approximate PDF points for uniform contract
            bx0 = x0 / scale; by0 = y0 / scale
            bx1 = x1 / scale; by1 = y1 / scale

            top_threshold_pt = by0 + (by1 - by0) * 0.15
            nearby_blocks = [
                tb for tb in page.text_blocks
                if bx0 <= tb.bbox_x0 <= bx1 and tb.bbox_y0 <= top_threshold_pt
            ]
            label = _extract_label_from_crop(crop, nearby_blocks, above_bbox_y0=by0)

            crops.append(ViewCrop(
                image=crop,
                bbox_x0=bx0,
                bbox_y0=by0,
                bbox_x1=bx1,
                bbox_y1=by1,
                view_label=label,
                area_fraction=area_fraction,
                page_index=page_index,
            ))

    # Sort into reading order: top-to-bottom, left-to-right
    crops.sort(key=lambda c: (round(c.bbox_y0 / 50) * 50, c.bbox_x0))

    # Synthesise numeric labels for unlabelled views
    for i, crop in enumerate(crops):
        if not crop.view_label:
            crop.view_label = f"VIEW_{i + 1}"

    log.info("Segmented %d view(s) from page (raster=%s)", len(crops), page.is_raster)
    return crops
