"""Side-by-side crop composer — Step 6 of the DrawDiff pipeline.

Takes the original + revised PNG crops for one matched view, normalises them
to the same height, and composes a horizontal side-by-side image with a
caption bar. The output PNG is embedded inline (base64 data URI) by the
report generator — per Step 6 contract there are no per-change bbox overlays
(Step 5's comparator does not emit coordinates).
"""
from __future__ import annotations

import io
from typing import Optional

from PIL import Image, ImageDraw, ImageFont


# Layout constants (tuned for A4 report width at 96 DPI print CSS).
_TARGET_HEIGHT = 900      # px; tall enough that 300 DPI crops keep detail
_GAP_PX = 24              # horizontal gap between the two crops
_CAPTION_H = 48           # caption bar height above the images
_CAPTION_BG = (30, 30, 30)
_CAPTION_FG = (255, 255, 255)
_PLACEHOLDER_BG = (235, 235, 235)
_PLACEHOLDER_FG = (110, 110, 110)


def _load_font(size: int) -> ImageFont.ImageFont:
    """Prefer DejaVuSans (bundled with Pillow); fall back to default bitmap."""
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except OSError:
        return ImageFont.load_default()


def _scaled_to_height(img: Image.Image, height: int) -> Image.Image:
    """Scale preserving aspect ratio so output height == `height`."""
    if img.height == height:
        return img
    w = max(1, round(img.width * (height / img.height)))
    return img.resize((w, height), Image.LANCZOS)


def _placeholder(label: str, height: int) -> Image.Image:
    """Produce a light-gray square with centred caption for missing sides."""
    side = height  # square placeholder
    img = Image.new("RGB", (side, height), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(img)
    font = _load_font(28)
    bbox = draw.textbbox((0, 0), label, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((side - tw) / 2, (height - th) / 2), label, fill=_PLACEHOLDER_FG, font=font)
    return img


def build_side_by_side(
    orig_png_bytes: Optional[bytes],
    rev_png_bytes: Optional[bytes],
    label: str,
) -> bytes:
    """Compose a caption + orig-on-left / revised-on-right PNG.

    Either side may be None; that side renders as a "MISSING" placeholder.
    Returns PNG bytes ready for base64 embedding in the report HTML.
    """
    if orig_png_bytes is None and rev_png_bytes is None:
        raise ValueError("build_side_by_side requires at least one side")

    orig = (Image.open(io.BytesIO(orig_png_bytes)).convert("RGB")
            if orig_png_bytes else _placeholder("ORIGINAL MISSING", _TARGET_HEIGHT))
    rev = (Image.open(io.BytesIO(rev_png_bytes)).convert("RGB")
           if rev_png_bytes else _placeholder("REVISED MISSING", _TARGET_HEIGHT))

    orig = _scaled_to_height(orig, _TARGET_HEIGHT)
    rev = _scaled_to_height(rev, _TARGET_HEIGHT)

    total_w = orig.width + _GAP_PX + rev.width
    total_h = _CAPTION_H + _TARGET_HEIGHT
    canvas = Image.new("RGB", (total_w, total_h), "white")

    # Caption bar
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, total_w, _CAPTION_H], fill=_CAPTION_BG)
    font = _load_font(22)
    caption = f"{label}   —   original  |  revised"
    bbox = draw.textbbox((0, 0), caption, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((total_w - tw) / 2, (_CAPTION_H - th) / 2), caption,
              fill=_CAPTION_FG, font=font)

    canvas.paste(orig, (0, _CAPTION_H))
    canvas.paste(rev, (orig.width + _GAP_PX, _CAPTION_H))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
