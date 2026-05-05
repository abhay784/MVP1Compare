"""LLM-driven view segmentation + matching.

Replaces the geometry-based `segmentor.match_views(parser→segmentor→matcher)`
pipeline path with a single Claude vision call per page-pair. Given the
rendered full-page image of the original and revised PDFs, Claude returns a
list of matched (or added/removed) views with normalized bounding boxes.

The output of this module is two artifacts the rest of the pipeline already
expects:
  - per-side `view_metadata` dicts (same shape as `pipeline.run` produces)
  - a `match_manifest` (same shape as `pipeline.matcher.match_views` would)

So nothing downstream (extractor / comparator / aggregator / annotator /
highlighter / report) needs to change.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import anthropic
import numpy as np
from PIL import Image

from config import config

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "segment_and_match.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# Public data contract
# ---------------------------------------------------------------------------

@dataclass
class LLMMatch:
    """One matched view-pair (or added/removed) returned by the LLM."""
    label: str
    match_type: str              # "matched" | "added" | "removed"
    orig_bbox_norm: Optional[tuple[float, float, float, float]]
    rev_bbox_norm:  Optional[tuple[float, float, float, float]]
    confidence: float
    rationale: str
    page_index: int = 0


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _png_bytes(image: np.ndarray, max_dim: int) -> bytes:
    """Serialize numpy image to PNG, downscaling if larger than max_dim."""
    img = Image.fromarray(image)
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _crop_norm(image: np.ndarray, bbox_norm: tuple[float, float, float, float]) -> np.ndarray:
    """Crop an image given a normalized [0,1] bbox."""
    h, w = image.shape[:2]
    x0, y0, x1, y1 = bbox_norm
    px0 = max(0, int(round(x0 * w)))
    py0 = max(0, int(round(y0 * h)))
    px1 = min(w, int(round(x1 * w)))
    py1 = min(h, int(round(y1 * h)))
    if px1 <= px0 or py1 <= py0:
        return np.zeros((1, 1, 3), dtype=image.dtype)
    return image[py0:py1, px0:px1]


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _build_messages(orig_png: bytes, rev_png: bytes) -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "ORIGINAL DRAWING (full page, prior revision):"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(orig_png).decode(),
            }},
            {"type": "text", "text": "REVISED DRAWING (full page, proposed revision):"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": base64.standard_b64encode(rev_png).decode(),
            }},
            {"type": "text", "text": "Segment views and return JSON per the contract."},
        ],
    }]


def _parse_json(raw: str) -> list[dict]:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    matches = data.get("matches", [])
    if not isinstance(matches, list):
        raise ValueError(f"LLM returned non-list 'matches' field: {type(matches).__name__}")
    return matches


def _coerce_bbox(b) -> Optional[tuple[float, float, float, float]]:
    if b is None:
        return None
    if not isinstance(b, (list, tuple)) or len(b) != 4:
        raise ValueError(f"bbox must be a 4-element list, got {b!r}")
    x0, y0, x1, y1 = (float(v) for v in b)
    # Clamp + reorder defensively
    x0, x1 = sorted((max(0.0, min(1.0, x0)), max(0.0, min(1.0, x1))))
    y0, y1 = sorted((max(0.0, min(1.0, y0)), max(0.0, min(1.0, y1))))
    return (x0, y0, x1, y1)


def _segment_and_match_page(
    orig_image: np.ndarray,
    rev_image: np.ndarray,
    page_index: int,
    model: str,
) -> list[LLMMatch]:
    """Call Claude once for a single page-pair. Returns parsed LLMMatch list."""
    orig_png = _png_bytes(orig_image, config.max_image_dimension)
    rev_png  = _png_bytes(rev_image,  config.max_image_dimension)

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=_build_messages(orig_png, rev_png),
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    parsed = _parse_json(raw)
    out: list[LLMMatch] = []
    for item in parsed:
        try:
            mtype = str(item.get("match_type", "matched")).lower()
            if mtype not in ("matched", "added", "removed"):
                log.warning("Unknown match_type %r; coercing to 'matched'", mtype)
                mtype = "matched"
            out.append(LLMMatch(
                label=str(item.get("label") or "").strip() or f"VIEW_p{page_index}_{len(out)+1}",
                match_type=mtype,
                orig_bbox_norm=_coerce_bbox(item.get("orig_bbox")),
                rev_bbox_norm=_coerce_bbox(item.get("rev_bbox")),
                confidence=float(item.get("confidence", 0.0)),
                rationale=str(item.get("rationale", "")),
                page_index=page_index,
            ))
        except Exception as exc:
            log.warning("Skipping malformed LLM match entry %r: %s", item, exc)

    # Validate match_type vs bbox presence
    for m in out:
        if m.match_type == "added" and m.rev_bbox_norm is None:
            log.warning("'added' match %r has no rev_bbox; dropping", m.label)
        if m.match_type == "removed" and m.orig_bbox_norm is None:
            log.warning("'removed' match %r has no orig_bbox; dropping", m.label)
    out = [
        m for m in out
        if not (m.match_type == "added"   and m.rev_bbox_norm  is None)
        and not (m.match_type == "removed" and m.orig_bbox_norm is None)
        and not (m.match_type == "matched" and (m.orig_bbox_norm is None or m.rev_bbox_norm is None))
    ]
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def segment_and_match(
    orig_pages: list[np.ndarray],
    rev_pages: list[np.ndarray],
    model: Optional[str] = None,
) -> list[LLMMatch]:
    """Run LLM segmentation + matching for every page-pair.

    Args:
        orig_pages: rendered full-page images (numpy RGB) for the original PDF.
        rev_pages:  rendered full-page images (numpy RGB) for the revised PDF.
        model:      Anthropic model id; defaults to config.base_model.

    Returns:
        Flat list of LLMMatch across all page-pairs (page_index set per match).

    Notes:
        - Pages are paired by index. If the two PDFs have a different page
          count, only the overlapping range is sent to the LLM. Surplus pages
          on either side are emitted as fully-removed / fully-added matches
          covering the entire page (so the rest of the pipeline still sees
          them).
    """
    chosen_model = model or config.base_model
    n_pair = min(len(orig_pages), len(rev_pages))

    all_matches: list[LLMMatch] = []
    for pi in range(n_pair):
        log.info("LLM seg+match — page %d (model=%s)", pi, chosen_model)
        page_matches = _segment_and_match_page(orig_pages[pi], rev_pages[pi], pi, chosen_model)
        log.info("LLM seg+match — page %d returned %d match(es)", pi, len(page_matches))
        all_matches.extend(page_matches)

    # Surplus pages: orig has pages the revised does not, or vice-versa.
    for pi in range(n_pair, len(orig_pages)):
        all_matches.append(LLMMatch(
            label=f"PAGE_{pi}_REMOVED",
            match_type="removed",
            orig_bbox_norm=(0.0, 0.0, 1.0, 1.0),
            rev_bbox_norm=None,
            confidence=1.0,
            rationale=f"Page {pi} present in original but not revised.",
            page_index=pi,
        ))
    for pi in range(n_pair, len(rev_pages)):
        all_matches.append(LLMMatch(
            label=f"PAGE_{pi}_ADDED",
            match_type="added",
            orig_bbox_norm=None,
            rev_bbox_norm=(0.0, 0.0, 1.0, 1.0),
            confidence=1.0,
            rationale=f"Page {pi} present in revised but not original.",
            page_index=pi,
        ))

    return all_matches


# ---------------------------------------------------------------------------
# Adapter helpers — convert LLMMatch results into the shapes the rest of the
# pipeline (extractor, comparator, annotator, highlighter, report) expects.
# ---------------------------------------------------------------------------

@dataclass
class _SideView:
    """Internal helper: a view crop on one side (original or revised)."""
    page_index: int
    bbox_norm: tuple[float, float, float, float]
    label: str
    image: np.ndarray   # cropped RGB array


def build_side_view_metadata(
    matches: list[LLMMatch],
    side_pages: list[np.ndarray],
    side_page_dims_pt: list[tuple[float, float]],
    side: str,
) -> tuple[list[dict], list[np.ndarray], list[Optional[int]]]:
    """For a single side ("original" | "revised"), produce:
        - view_metadata list (same shape as pipeline.run produces)
        - parallel list of cropped numpy images (so caller can persist them)
        - parallel list mapping each crop to its source LLMMatch index
          (so the match_manifest can later resolve crop_s3_key correctly)

    `side_page_dims_pt` is per-page (page_width_pt, page_height_pt) used to
    convert normalized bboxes back to PDF points (kept in metadata for the
    extractor/highlighter, which work in PDF point space).
    """
    bbox_attr = "orig_bbox_norm" if side == "original" else "rev_bbox_norm"

    metadata: list[dict] = []
    crops: list[np.ndarray] = []
    match_indices: list[Optional[int]] = []

    next_index = 0
    for mi, m in enumerate(matches):
        bbox_norm = getattr(m, bbox_attr)
        if bbox_norm is None:
            continue  # this match has no view on this side (added/removed)

        page_image = side_pages[m.page_index]
        crop = _crop_norm(page_image, bbox_norm)

        # Pixel space → PDF points using this page's dimensions
        page_w_pt, page_h_pt = side_page_dims_pt[m.page_index]
        x0, y0, x1, y1 = bbox_norm
        bbox_pt = (x0 * page_w_pt, y0 * page_h_pt, x1 * page_w_pt, y1 * page_h_pt)
        area_fraction = (x1 - x0) * (y1 - y0)

        metadata.append({
            "index":         next_index,
            "page_index":    m.page_index,
            "view_label":    m.label,
            "area_fraction": area_fraction,
            "bbox":          [bbox_pt[0], bbox_pt[1], bbox_pt[2], bbox_pt[3]],
            "crop_s3_key":   None,   # caller fills in once the crop is persisted
            "dimensions":    [],     # caller fills in via extractor.extract_dimensions
        })
        crops.append(crop)
        match_indices.append(mi)
        next_index += 1

    return metadata, crops, match_indices
