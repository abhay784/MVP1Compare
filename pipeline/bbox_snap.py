"""Snap LLM-emitted view bboxes so no view edge cuts through annotation text.

The LLM's seg+match call produces approximate view rectangles. Inevitably,
some text token (a dimension value, a callout, a view label) ends up
straddling a view edge — half inside one view, half inside its neighbour.
This module post-processes those bboxes so every text token is fully inside
exactly one view (or fully outside all views, if it's an orphan like a
title-block string).

Algorithm — per page, in PDF-point space:

  1. Ownership. For each text token T, the OWNING view is the one whose
     bbox has the greatest intersection area with T. Ties are broken by
     center distance. A text with zero intersection with every view is an
     orphan and never forces any change.

  2. Expand. Each view's bbox is expanded outward (element-wise min/max)
     so it fully contains every text token it owns.

  3. Shrink. For each foreign text token (owned by another view) that
     still intrudes into the expanded bbox, pull whichever single edge
     fully excludes that token AND keeps every owned token contained,
     choosing the edge that loses the least area. If no edge can do both
     (foreign and owned text are interleaved), the foreign token is left
     in place — that's a degenerate case the LLM can't be saved from.

The snap is a strict improvement when the LLM is approximately right: if a
view's bbox already contains all its owned text and no foreign text, every
step is a no-op.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

log = logging.getLogger(__name__)

Bbox = tuple[float, float, float, float]   # (x0, y0, x1, y1), x/y increase right/down


# ---------------------------------------------------------------------------
# Bbox helpers
# ---------------------------------------------------------------------------

def _intersect_area(a: Bbox, b: Bbox) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    return (ix1 - ix0) * (iy1 - iy0)


def _bbox_area(b: Bbox) -> float:
    return max(0.0, (b[2] - b[0]) * (b[3] - b[1]))


def _bbox_center(b: Bbox) -> tuple[float, float]:
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def _center_dist_sq(a: Bbox, b: Bbox) -> float:
    ax, ay = _bbox_center(a)
    bx, by = _bbox_center(b)
    return (ax - bx) ** 2 + (ay - by) ** 2


# ---------------------------------------------------------------------------
# Ownership assignment
# ---------------------------------------------------------------------------

def _assign_ownership(view_bboxes: list[Bbox], text_bboxes: list[Bbox]) -> list[int]:
    """For each text bbox, return the index of its owning view (or -1).

    Owner = view with strictly greatest intersection area; ties broken by
    closer center. Text with zero intersection with every view is orphan.
    """
    owners: list[int] = []
    for t in text_bboxes:
        best_i = -1
        best_area = 0.0
        best_dist_sq = float("inf")
        for i, v in enumerate(view_bboxes):
            area = _intersect_area(t, v)
            if area <= 0:
                continue
            if area > best_area:
                best_i = i
                best_area = area
                best_dist_sq = _center_dist_sq(t, v)
            elif area == best_area:
                d = _center_dist_sq(t, v)
                if d < best_dist_sq:
                    best_i = i
                    best_dist_sq = d
        owners.append(best_i)
    return owners


# ---------------------------------------------------------------------------
# Per-view bbox snap
# ---------------------------------------------------------------------------

def _snap_view_bbox(
    view_bbox: Bbox,
    owned_text: list[Bbox],
    foreign_text: list[Bbox],
) -> Bbox:
    """Expand view to fully include owned text, then shrink to fully exclude
    intruding foreign text. Owned-text containment is preserved at all costs.
    """
    vx0, vy0, vx1, vy1 = view_bbox

    # Step 1 — expand to include all owned text.
    if owned_text:
        ox0_min = min(t[0] for t in owned_text)
        oy0_min = min(t[1] for t in owned_text)
        ox1_max = max(t[2] for t in owned_text)
        oy1_max = max(t[3] for t in owned_text)
        new_x0 = min(vx0, ox0_min)
        new_y0 = min(vy0, oy0_min)
        new_x1 = max(vx1, ox1_max)
        new_y1 = max(vy1, oy1_max)
        owned_min_x0, owned_max_x1 = ox0_min, ox1_max
        owned_min_y0, owned_max_y1 = oy0_min, oy1_max
    else:
        # No owned text — nothing to anchor; leave shrink window equal to bbox.
        new_x0, new_y0, new_x1, new_y1 = vx0, vy0, vx1, vy1
        cx, cy = _bbox_center(view_bbox)
        owned_min_x0 = owned_max_x1 = cx
        owned_min_y0 = owned_max_y1 = cy

    # Step 2 — shrink to exclude foreign text. Cut biggest intrusions first
    # so smaller adjacent intrusions either become irrelevant or are cut on
    # the same edge.
    sorted_foreign = sorted(foreign_text, key=_bbox_area, reverse=True)

    for t in sorted_foreign:
        tx0, ty0, tx1, ty1 = t
        # Skip if t no longer intersects the current bbox.
        if tx1 <= new_x0 or tx0 >= new_x1 or ty1 <= new_y0 or ty0 >= new_y1:
            continue

        candidates: list[tuple[str, float, float]] = []  # (edge, new_value, area_loss)

        # Pull left edge inward (push x0 right to tx1). Valid only if t lies
        # entirely to the LEFT of every owned token AND of the current x1.
        if tx1 <= owned_min_x0 and tx1 > new_x0 and tx1 < new_x1:
            candidates.append(("x0", tx1, (tx1 - new_x0) * (new_y1 - new_y0)))

        # Pull right edge inward (pull x1 left to tx0).
        if tx0 >= owned_max_x1 and tx0 < new_x1 and tx0 > new_x0:
            candidates.append(("x1", tx0, (new_x1 - tx0) * (new_y1 - new_y0)))

        # Pull top edge down (y0 down to ty1).
        if ty1 <= owned_min_y0 and ty1 > new_y0 and ty1 < new_y1:
            candidates.append(("y0", ty1, (new_x1 - new_x0) * (ty1 - new_y0)))

        # Pull bottom edge up (y1 up to ty0).
        if ty0 >= owned_max_y1 and ty0 < new_y1 and ty0 > new_y0:
            candidates.append(("y1", ty0, (new_x1 - new_x0) * (new_y1 - ty0)))

        if not candidates:
            log.debug(
                "Cannot exclude foreign text %s from view %s without losing owned text",
                t, (new_x0, new_y0, new_x1, new_y1),
            )
            continue

        edge, val, _loss = min(candidates, key=lambda c: c[2])
        if edge == "x0":   new_x0 = val
        elif edge == "x1": new_x1 = val
        elif edge == "y0": new_y0 = val
        elif edge == "y1": new_y1 = val

    return (new_x0, new_y0, new_x1, new_y1)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def snap_view_bboxes_to_text(
    view_bboxes: list[Bbox],
    text_bboxes: list[Bbox],
) -> list[Bbox]:
    """Snap view bboxes so no view edge cuts a text bbox.

    Args:
        view_bboxes: list of view rectangles in some shared coordinate space
                     (e.g. PDF points). Order is preserved in the output.
        text_bboxes: list of text-token rectangles in the SAME coordinate
                     space as `view_bboxes`. Token granularity (PyMuPDF
                     "words") works well — coarser blocks tend to swallow
                     unrelated text and produce worse ownership decisions.

    Returns:
        New view bboxes, same length and order as `view_bboxes`. If a snap
        would invert a bbox (degenerate input), the original is returned
        for that index unchanged.
    """
    if not view_bboxes:
        return []
    if not text_bboxes:
        return list(view_bboxes)

    owners = _assign_ownership(view_bboxes, text_bboxes)

    new_bboxes: list[Bbox] = []
    for i, v in enumerate(view_bboxes):
        owned = [t for t, o in zip(text_bboxes, owners) if o == i]
        foreign = [t for t, o in zip(text_bboxes, owners) if o not in (-1, i)]
        snapped = _snap_view_bbox(v, owned, foreign)
        # Defensive: never return an inverted/zero bbox; keep original.
        if snapped[2] <= snapped[0] or snapped[3] <= snapped[1]:
            new_bboxes.append(v)
            continue
        new_bboxes.append(snapped)

    return new_bboxes
