"""Full-page highlight overlay — draws coloured boxes (and dimension circles)
on a rendered drawing page to show where each view's changes are located.

Companion to pipeline/annotator.py:
- annotator builds side-by-side crop pairs for the per-view PDF report sections.
- highlighter draws on the full-page render so the user can see where on the
  drawing each change happens.

The comparator does NOT emit per-change pixel coordinates, so granularity is:
  - View-level: one box per view that has any changes, severity-coloured.
  - Dimension-level: when a change's orig/revised value matches a dimension
    string extracted for that view, a tighter ellipse is drawn on the
    dimension's bbox.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from rapidfuzz import fuzz

from config import config

log = logging.getLogger(__name__)


# ---- severity styling -----------------------------------------------------

_SEVERITY_RGB = {
    "CRITICAL":  (220, 38, 38),    # red
    "MAJOR":     (234, 88, 12),    # orange
    "MINOR":     (202, 138, 4),    # gold
    "UNCERTAIN": (107, 114, 128),  # grey
}
_SEVERITY_RANK = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1, "UNCERTAIN": 0}

_VIEW_BOX_WIDTH_PX = 6
_DIM_ELLIPSE_WIDTH_PX = 4
_LABEL_PADDING_PX = 6
_DIM_MATCH_RATIO = 90  # rapidfuzz threshold for matching change values to dimension strings

_SKIP_MATCH_TYPES = {"title_block", "revision_block"}


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans-Bold.ttf", size=size)
    except OSError:
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size=size)
        except OSError:
            return ImageFont.load_default()


def _max_severity(changes: list[dict]) -> Optional[str]:
    sev = None
    rank = -1
    for c in changes:
        s = c.get("severity")
        r = _SEVERITY_RANK.get(s, -1)
        if r > rank:
            rank = r
            sev = s
    return sev


def _bbox_to_pixels(bbox_pts: list[float], scale: float) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox_pts
    return (round(x0 * scale), round(y0 * scale),
            round(x1 * scale), round(y1 * scale))


def _draw_label(draw: ImageDraw.ImageDraw, x: int, y: int,
                text: str, fill: tuple[int, int, int], font: ImageFont.ImageFont) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = _LABEL_PADDING_PX
    rect = [x, y, x + tw + 2 * pad, y + th + 2 * pad]
    draw.rectangle(rect, fill=fill)
    draw.text((x + pad, y + pad), text, fill=(255, 255, 255), font=font)


def _find_dim_bbox(value: Optional[str], dimensions: list[dict]) -> Optional[list[float]]:
    """Return the bbox of the first dimension whose value fuzzy-matches `value`."""
    if not value or not dimensions:
        return None
    val = str(value).strip()
    if not val:
        return None
    best_score = 0
    best_bbox = None
    for d in dimensions:
        dv = str(d.get("value", "")).strip()
        if not dv:
            continue
        score = fuzz.ratio(val, dv)
        if score > best_score:
            best_score = score
            best_bbox = d.get("bbox")
    if best_score >= _DIM_MATCH_RATIO:
        return best_bbox
    return None


def highlight_page(
    page_png: bytes,
    view_diffs: list[dict],
    views_extraction: list[dict],
    side: str,
) -> bytes:
    """Draw severity-coloured overlays on a full-page render.

    Args:
        page_png:         Full-page PNG bytes (rendered at config.render_dpi).
        view_diffs:       changeset.json view_diffs list. Each entry has
                          orig_index/rev_index (added by run.py before
                          aggregation), match_type, label, changes.
        views_extraction: extraction.json's original.views or revised.views list.
        side:             "original" or "revised".

    Returns:
        Annotated PNG bytes.
    """
    img = Image.open(io.BytesIO(page_png)).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    scale = config.render_dpi / 72.0
    label_font = _load_font(28)

    views_by_index = {v["index"]: v for v in views_extraction}
    index_field = "orig_index" if side == "original" else "rev_index"

    drawn = 0
    for vd in view_diffs:
        if vd.get("match_type") in _SKIP_MATCH_TYPES:
            continue
        idx = vd.get(index_field)
        if idx is None:
            continue
        view = views_by_index.get(idx)
        if view is None:
            log.warning("highlight_page: view index %s not found in %s extraction", idx, side)
            continue

        bbox_pts = view.get("bbox")
        if not bbox_pts:
            continue
        sev = _max_severity(vd.get("changes", [])) or "UNCERTAIN"
        colour = _SEVERITY_RGB.get(sev, _SEVERITY_RGB["UNCERTAIN"])

        x0, y0, x1, y1 = _bbox_to_pixels(bbox_pts, scale)
        draw.rectangle([x0, y0, x1, y1], outline=colour, width=_VIEW_BOX_WIDTH_PX)

        change_count = len(vd.get("changes", []))
        label = f"{vd.get('label', 'view')}  •  {change_count} change{'s' if change_count != 1 else ''}  •  {sev}"
        _draw_label(draw, x0, max(0, y0 - 50), label, colour, label_font)

        # Dimension-level refinement when a change targets a known dimension string.
        dimensions = view.get("dimensions", []) or []
        for c in vd.get("changes", []):
            value_for_side = c.get("orig_value") if side == "original" else c.get("revised_value")
            dim_bbox = _find_dim_bbox(value_for_side, dimensions)
            if dim_bbox is None:
                continue
            dx0, dy0, dx1, dy1 = _bbox_to_pixels(dim_bbox, scale)
            pad = 12
            draw.ellipse([dx0 - pad, dy0 - pad, dx1 + pad, dy1 + pad],
                         outline=colour, width=_DIM_ELLIPSE_WIDTH_PX)

        drawn += 1

    log.info("highlight_page (%s): drew overlays for %d view(s)", side, drawn)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
