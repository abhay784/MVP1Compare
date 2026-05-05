"""Resolve change locations: per-change page-space bboxes + zone labels.

Runs after the comparator + aggregator. For each change we attach four new
fields in place on the change dict:

    orig_bbox_pt : list[list[float]]   page-space PDF-point bboxes (orig side)
    rev_bbox_pt  : list[list[float]]   page-space PDF-point bboxes (rev side)
    orig_zone    : str                  e.g. "B5" (or "" if unresolved)
    rev_zone     : str                  e.g. "B5"
    zone         : str                  display value: rev_zone or orig_zone

Two location sources, in priority order:

  1. The comparator-emitted ``orig_bbox`` / ``rev_bbox`` fields, which are
     normalized [0,1] within the respective view-crop image. These are
     projected to page-space PDF points using the view's bbox in extraction
     metadata.

  2. Text-anchor search (the same routine used for in-crop highlights):
     fuzzy/numeric matching of the change's value strings against extracted
     dimensions and text tokens within the view. Each anchor token's bbox
     is already in page-space PDF points.

For ``title_block`` and ``revision_block`` match types we use a fixed zone
label since those changes don't have spatial coordinates on the drawing.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from pipeline.annotator import _find_anchor_bboxes
from pipeline.zoning import zone_range_for_bbox, zones_for_bboxes

log = logging.getLogger(__name__)


def _project_crop_bbox_to_page(
    crop_bbox: list[float],
    view_bbox_pt: list[float],
) -> Optional[list[float]]:
    """Map a [0,1]-normalized crop-relative bbox to PDF page-space points."""
    if crop_bbox is None or len(crop_bbox) != 4 or len(view_bbox_pt) != 4:
        return None
    cx0, cy0, cx1, cy1 = crop_bbox
    vx0, vy0, vx1, vy1 = view_bbox_pt
    vw = float(vx1) - float(vx0)
    vh = float(vy1) - float(vy0)
    if vw <= 0 or vh <= 0:
        return None
    return [
        float(vx0) + cx0 * vw,
        float(vy0) + cy0 * vh,
        float(vx0) + cx1 * vw,
        float(vy0) + cy1 * vh,
    ]


def _resolve_side(
    change: dict,
    side: str,                    # "original" | "revised"
    view_meta: Optional[dict],
) -> list[list[float]]:
    """Return page-space PDF-point bboxes for this change on the given side.

    Strategy: trust the comparator's bbox first; fall back to text-anchor
    search; return [] if neither finds anything.
    """
    if view_meta is None:
        return []

    view_bbox = view_meta.get("bbox") or []
    if len(view_bbox) != 4:
        return []

    crop_bbox_key = "orig_bbox" if side == "original" else "rev_bbox"
    crop_bbox = change.get(crop_bbox_key)
    if crop_bbox:
        projected = _project_crop_bbox_to_page(crop_bbox, view_bbox)
        if projected is not None:
            return [projected]

    # Fallback: text-anchor search (already returns page-space PDF-point bboxes)
    value_for_side = change.get("orig_value") if side == "original" else change.get("revised_value")
    return _find_anchor_bboxes(
        value_for_side,
        view_meta.get("dimensions") or [],
        view_meta.get("text_blocks") or [],
    )


_TITLE_BLOCK_ZONE = "Title Block"
_REVISION_BLOCK_ZONE = "Revision Block"


def annotate_change_locations(
    view_diffs: list[dict],
    orig_views_by_idx: dict[int, dict],
    rev_views_by_idx: dict[int, dict],
    page_dims_pt: dict[int, tuple[float, float]],
) -> None:
    """Mutate `view_diffs` in place: attach bbox_pt + zone fields to each change.

    Args:
        view_diffs:        list of view_diff dicts (post-aggregate).
        orig_views_by_idx: original-side view_metadata indexed by ``index``.
        rev_views_by_idx:  revised-side view_metadata indexed by ``index``.
        page_dims_pt:      per page_index, ``(page_w_pt, page_h_pt)``.

    Default zone behavior: prefer rev_zone for the display column (the
    user is typically reading the new revision) and fall back to orig_zone
    when only the original side has spatial info.
    """
    for vd in view_diffs:
        match_type = vd.get("match_type")

        orig_idx = vd.get("orig_index")
        rev_idx  = vd.get("rev_index")
        orig_view = orig_views_by_idx.get(orig_idx) if orig_idx is not None else None
        rev_view  = rev_views_by_idx.get(rev_idx)   if rev_idx  is not None else None

        # Pick a representative page index → page dims for zone math
        pi = None
        if orig_view is not None:
            pi = orig_view.get("page_index")
        if pi is None and rev_view is not None:
            pi = rev_view.get("page_index")
        page_w_pt, page_h_pt = page_dims_pt.get(pi, (0.0, 0.0)) if pi is not None else (0.0, 0.0)

        for c in vd.get("changes", []):
            # Title/revision block changes don't have geometric coordinates.
            if match_type == "title_block":
                c["orig_bbox_pt"] = []
                c["rev_bbox_pt"]  = []
                c["orig_zone"] = _TITLE_BLOCK_ZONE
                c["rev_zone"]  = _TITLE_BLOCK_ZONE
                c["zone"] = _TITLE_BLOCK_ZONE
                continue
            if match_type == "revision_block":
                c["orig_bbox_pt"] = []
                c["rev_bbox_pt"]  = []
                c["orig_zone"] = _REVISION_BLOCK_ZONE
                c["rev_zone"]  = _REVISION_BLOCK_ZONE
                c["zone"] = _REVISION_BLOCK_ZONE
                continue

            orig_bboxes_pt = _resolve_side(c, "original", orig_view)
            rev_bboxes_pt  = _resolve_side(c, "revised",  rev_view)

            c["orig_bbox_pt"] = orig_bboxes_pt
            c["rev_bbox_pt"]  = rev_bboxes_pt

            c["orig_zone"] = zones_for_bboxes(orig_bboxes_pt, page_w_pt, page_h_pt) if orig_bboxes_pt else ""
            c["rev_zone"]  = zones_for_bboxes(rev_bboxes_pt,  page_w_pt, page_h_pt) if rev_bboxes_pt  else ""

            # Display column: prefer revised side; fall back to original.
            c["zone"] = c["rev_zone"] or c["orig_zone"] or ""
