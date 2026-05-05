"""View-pair composer — Step 6 of the DrawDiff pipeline.

Given the original + revised PNG crops for one matched view (plus, optionally,
the per-view changes and view metadata), this module:

  1. Draws severity-coloured overlays on each side, highlighting the
     dimensions / annotations the comparator flagged as changed. Falls back to
     a single corner badge when a change cannot be located on the crop (the
     comparator does not emit pixel coordinates per change, so we fuzzy-match
     change values against the view's extracted dimension list — same approach
     as `pipeline.highlighter`).

  2. Picks a layout. Engineering view crops are often very wide (landscape
     section views), and shrinking two of them side-by-side onto an A4 page
     drops the in-drawing text below the readable threshold. So when either
     crop is wider than tall by a meaningful margin we stack original ABOVE
     revised; otherwise we keep the classic side-by-side layout.

The output PNG is embedded inline (base64 data URI) by the report generator.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont
from rapidfuzz import fuzz

from config import config

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Layout constants (tuned for A4 report width at 96 DPI print CSS)
# ---------------------------------------------------------------------------

_TARGET_HEIGHT = 900       # px, side-by-side layout (each crop scaled to this height)
_TARGET_WIDTH  = 2200      # px, stacked layout    (each crop scaled to this width)
_GAP_PX = 24
_CAPTION_H = 48
_SUBCAPTION_H = 36
_CAPTION_BG = (30, 30, 30)
_CAPTION_FG = (255, 255, 255)
_PLACEHOLDER_BG = (235, 235, 235)
_PLACEHOLDER_FG = (110, 110, 110)

# Aspect-ratio threshold above which we stack rather than side-by-side.
# Most engineering section/elevation views are 1.5–3.0× wider than tall;
# shrinking such a pair side-by-side onto A4 makes drawing text unreadable.
_STACK_ASPECT_RATIO = 1.5


# ---------------------------------------------------------------------------
# Severity styling — kept in sync with pipeline/highlighter.py
# ---------------------------------------------------------------------------

_SEVERITY_RGB = {
    "CRITICAL":  (220, 38, 38),
    "MAJOR":     (234, 88, 12),
    "MINOR":     (202, 138, 4),
    "UNCERTAIN": (107, 114, 128),
}
_SEVERITY_RANK = {"CRITICAL": 3, "MAJOR": 2, "MINOR": 1, "UNCERTAIN": 0}
_DIM_MATCH_RATIO = 90      # rapidfuzz threshold for matching change values to dimensions
_HIGHLIGHT_WIDTH = 6       # outline width for in-crop highlight ellipses
_HIGHLIGHT_PAD_PX = 14
_BADGE_PADDING_PX = 8


# ---------------------------------------------------------------------------
# Font + helper utilities
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size=size)
    except OSError:
        return ImageFont.load_default()


def _scaled_to_height(img: Image.Image, height: int) -> Image.Image:
    if img.height == height:
        return img
    w = max(1, round(img.width * (height / img.height)))
    return img.resize((w, height), Image.LANCZOS)


def _scaled_to_width(img: Image.Image, width: int) -> Image.Image:
    if img.width == width:
        return img
    h = max(1, round(img.height * (width / img.width)))
    return img.resize((width, h), Image.LANCZOS)


def _placeholder(label: str, size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGB", size, _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(img)
    font = _load_font(28)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size[0] - tw) / 2, (size[1] - th) / 2),
              label, fill=_PLACEHOLDER_FG, font=font)
    return img


# ---------------------------------------------------------------------------
# In-crop highlight overlay
# ---------------------------------------------------------------------------

def _max_severity(changes: list[dict]) -> Optional[str]:
    sev, rank = None, -1
    for c in changes:
        s = c.get("severity")
        r = _SEVERITY_RANK.get(s, -1)
        if r > rank:
            rank, sev = r, s
    return sev


def _find_dim_bbox(value: Optional[str], dimensions: list[dict]) -> Optional[list[float]]:
    if not value or not dimensions:
        return None
    val = str(value).strip()
    if not val:
        return None
    best_score, best_bbox = 0, None
    for d in dimensions:
        dv = str(d.get("value", "")).strip()
        if not dv:
            continue
        score = fuzz.ratio(val, dv)
        if score > best_score:
            best_score, best_bbox = score, d.get("bbox")
    return best_bbox if best_score >= _DIM_MATCH_RATIO else None


def _draw_corner_badge(
    draw: ImageDraw.ImageDraw,
    text: str,
    fill: tuple[int, int, int],
    image_size: tuple[int, int],
) -> None:
    """Top-left badge summarizing severity + change count when no per-change
    spatial match is found in the dimension list."""
    font = _load_font(28, bold=True)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = _BADGE_PADDING_PX
    x0, y0 = 12, 12
    rect = [x0, y0, x0 + tw + 2 * pad, y0 + th + 2 * pad]
    draw.rectangle(rect, fill=fill)
    draw.text((x0 + pad, y0 + pad), text, fill=(255, 255, 255), font=font)


def _highlight_crop(
    crop: Image.Image,
    side: str,                          # "original" | "revised"
    changes: list[dict],
    view_meta: Optional[dict],
) -> Image.Image:
    """Draw severity-coloured overlays on a crop image (in place on a copy).

    Args:
        crop:      Full-resolution crop (Pillow Image, RGB).
        side:      Which side this crop is, controls which value of each Change
                   is used for the dimension fuzzy-match.
        changes:   List of change dicts for this view (changeset.json shape).
        view_meta: View metadata dict for THIS side (extraction.json view), used
                   to translate dimension bboxes from page-space PDF points to
                   crop-pixel coordinates. May be None — in that case we skip
                   per-dimension overlays and just draw a corner badge.
    """
    if not changes:
        return crop

    # Pick the dominant severity for the badge / fallback colour.
    sev = _max_severity(changes) or "UNCERTAIN"
    colour = _SEVERITY_RGB.get(sev, _SEVERITY_RGB["UNCERTAIN"])

    out = crop.copy()
    draw = ImageDraw.Draw(out, "RGBA")

    cw, ch = out.size
    matched_any = False

    # Try to project each change's matching dimension bbox onto the crop.
    if view_meta is not None:
        view_bbox = view_meta.get("bbox") or []
        dimensions = view_meta.get("dimensions") or []
        if len(view_bbox) == 4 and dimensions:
            vx0, vy0, vx1, vy1 = view_bbox
            view_w_pt = max(0.0, float(vx1) - float(vx0))
            view_h_pt = max(0.0, float(vy1) - float(vy0))
            if view_w_pt > 0 and view_h_pt > 0:
                for c in changes:
                    value_for_side = c.get("orig_value") if side == "original" else c.get("revised_value")
                    dim_bbox = _find_dim_bbox(value_for_side, dimensions)
                    if dim_bbox is None:
                        continue
                    dx0, dy0, dx1, dy1 = dim_bbox

                    # PDF-points page-space → crop-pixel space (proportional;
                    # robust to off-by-one rounding between the rendered crop
                    # and `view_w_pt * scale`).
                    fx0 = (float(dx0) - float(vx0)) / view_w_pt
                    fy0 = (float(dy0) - float(vy0)) / view_h_pt
                    fx1 = (float(dx1) - float(vx0)) / view_w_pt
                    fy1 = (float(dy1) - float(vy0)) / view_h_pt

                    px0 = max(0, int(round(fx0 * cw))) - _HIGHLIGHT_PAD_PX
                    py0 = max(0, int(round(fy0 * ch))) - _HIGHLIGHT_PAD_PX
                    px1 = min(cw, int(round(fx1 * cw))) + _HIGHLIGHT_PAD_PX
                    py1 = min(ch, int(round(fy1 * ch))) + _HIGHLIGHT_PAD_PX
                    if px1 <= px0 or py1 <= py0:
                        continue

                    c_sev = c.get("severity") or sev
                    c_colour = _SEVERITY_RGB.get(c_sev, colour)
                    draw.ellipse([px0, py0, px1, py1],
                                 outline=c_colour, width=_HIGHLIGHT_WIDTH)
                    matched_any = True

    # Always show a corner badge so the reader sees the change count + severity
    # at-a-glance, even when no individual dimension was located.
    n = len(changes)
    badge_text = f"{n} CHANGE{'S' if n != 1 else ''}  •  {sev}"
    if not matched_any:
        badge_text += "  (see table)"
    _draw_corner_badge(draw, badge_text, colour, out.size)

    return out


# ---------------------------------------------------------------------------
# Composition (side-by-side OR stacked)
# ---------------------------------------------------------------------------

def _should_stack(orig: Image.Image, rev: Image.Image) -> bool:
    """Stack vertically when either crop's width:height exceeds the threshold."""
    def aspect(img: Image.Image) -> float:
        return img.width / max(1, img.height)
    return max(aspect(orig), aspect(rev)) >= _STACK_ASPECT_RATIO


def _compose_side_by_side(orig: Image.Image, rev: Image.Image, label: str) -> Image.Image:
    orig_s = _scaled_to_height(orig, _TARGET_HEIGHT)
    rev_s  = _scaled_to_height(rev,  _TARGET_HEIGHT)

    total_w = orig_s.width + _GAP_PX + rev_s.width
    total_h = _CAPTION_H + _TARGET_HEIGHT
    canvas = Image.new("RGB", (total_w, total_h), "white")

    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, total_w, _CAPTION_H], fill=_CAPTION_BG)
    font = _load_font(22, bold=True)
    caption = f"{label}   —   ORIGINAL  |  REVISED"
    bbox = draw.textbbox((0, 0), caption, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((total_w - tw) / 2, (_CAPTION_H - th) / 2),
              caption, fill=_CAPTION_FG, font=font)

    canvas.paste(orig_s, (0, _CAPTION_H))
    canvas.paste(rev_s,  (orig_s.width + _GAP_PX, _CAPTION_H))
    return canvas


def _compose_stacked(orig: Image.Image, rev: Image.Image, label: str) -> Image.Image:
    """Stack original above revised so wide drawings keep their text readable."""
    orig_s = _scaled_to_width(orig, _TARGET_WIDTH)
    rev_s  = _scaled_to_width(rev,  _TARGET_WIDTH)

    total_w = _TARGET_WIDTH
    total_h = _CAPTION_H + _SUBCAPTION_H + orig_s.height + _SUBCAPTION_H + rev_s.height
    canvas = Image.new("RGB", (total_w, total_h), "white")
    draw = ImageDraw.Draw(canvas)

    # Main caption
    draw.rectangle([0, 0, total_w, _CAPTION_H], fill=_CAPTION_BG)
    font = _load_font(22, bold=True)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((total_w - tw) / 2, (_CAPTION_H - th) / 2),
              label, fill=_CAPTION_FG, font=font)

    # Sub-caption "ORIGINAL"
    y = _CAPTION_H
    draw.rectangle([0, y, total_w, y + _SUBCAPTION_H], fill=(80, 80, 80))
    sub_font = _load_font(18, bold=True)
    for txt, top in (("ORIGINAL", y),):
        b = draw.textbbox((0, 0), txt, font=sub_font)
        draw.text((12, top + (_SUBCAPTION_H - (b[3] - b[1])) / 2),
                  txt, fill=_CAPTION_FG, font=sub_font)
    y += _SUBCAPTION_H
    canvas.paste(orig_s, (0, y))
    y += orig_s.height

    # Sub-caption "REVISED"
    draw.rectangle([0, y, total_w, y + _SUBCAPTION_H], fill=(80, 80, 80))
    b = draw.textbbox((0, 0), "REVISED", font=sub_font)
    draw.text((12, y + (_SUBCAPTION_H - (b[3] - b[1])) / 2),
              "REVISED", fill=_CAPTION_FG, font=sub_font)
    y += _SUBCAPTION_H
    canvas.paste(rev_s, (0, y))
    return canvas


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_side_by_side(
    orig_png_bytes: Optional[bytes],
    rev_png_bytes: Optional[bytes],
    label: str,
    *,
    changes: Optional[list[dict]] = None,
    orig_view: Optional[dict] = None,
    rev_view: Optional[dict] = None,
) -> bytes:
    """Compose a captioned view-pair PNG (side-by-side OR stacked).

    Args:
        orig_png_bytes: Original-side crop bytes, or None.
        rev_png_bytes:  Revised-side crop bytes, or None.
        label:          View label for the caption bar.
        changes:        Optional list of change dicts (changeset.json shape) for
                        this view. When provided, severity-coloured highlights
                        are drawn on each crop.
        orig_view:      Optional view-metadata dict for the original side
                        (must contain `bbox` in PDF points and `dimensions`).
                        Used to project dimension bboxes onto the crop.
        rev_view:       Same, for the revised side.

    Returns: PNG bytes ready for base64 embedding in the report HTML.
    """
    if orig_png_bytes is None and rev_png_bytes is None:
        raise ValueError("build_side_by_side requires at least one side")

    placeholder_size = (_TARGET_HEIGHT, _TARGET_HEIGHT)  # square; only used when missing
    orig = (Image.open(io.BytesIO(orig_png_bytes)).convert("RGB")
            if orig_png_bytes else _placeholder("ORIGINAL MISSING", placeholder_size))
    rev = (Image.open(io.BytesIO(rev_png_bytes)).convert("RGB")
           if rev_png_bytes else _placeholder("REVISED MISSING", placeholder_size))

    # Per-side highlights are applied at full resolution before any scaling so
    # ellipse outlines stay crisp under downscale.
    if changes:
        if orig_png_bytes is not None:
            orig = _highlight_crop(orig, "original", changes, orig_view)
        if rev_png_bytes is not None:
            rev  = _highlight_crop(rev,  "revised",  changes, rev_view)

    if _should_stack(orig, rev):
        canvas = _compose_stacked(orig, rev, label)
    else:
        canvas = _compose_side_by_side(orig, rev, label)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
