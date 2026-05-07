"""Page-graph builder — view-relationship intelligence layer.

Produces a PageGraph for each parsed page that captures:
  - which ViewCrop is a parent (orthographic projection) vs derived (section / detail)
  - for each derived view, the cut_id (e.g. "A-A") and its parent_label
  - a one-line semantic role per derived view ("describes")
  - a one-sentence part_summary for the whole page

Two passes:
  Pass A — deterministic. Reads pymupdf draw commands + text blocks. Pairs
           SECTION X-X labels with cut markers sharing their X-X id, builds
           parent→derived edges geometrically. Vector PDFs only.
  Pass B — LLM. Sends the downscaled full-page render plus Pass A's partial
           topology to the model and asks it to (i) confirm Pass A's
           parent/derived assignments, (ii) fill `describes` per derived view,
           (iii) write a one-sentence part_summary, (iv) flag any view Pass A
           missed (detail / auxiliary). Pass B never overrides Pass A's
           topology — it only annotates.

The graph is *attached* back onto the input ViewCrop list (mutating
view_role / parent_label / cut_id / describes fields) so downstream stages
need no schema-aware plumbing beyond what already exists in segmentor.ViewCrop.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import anthropic
import fitz
import numpy as np
from PIL import Image

from config import config
from pipeline.parser import ParsedPage
from pipeline.segmentor import ViewCrop

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DerivedView:
    label: str               # e.g. "SECTION A-A"
    parent_label: str        # label of the parent ViewCrop this is derived from
    cut_id: str | None       # "A-A", "B-B"; None for detail views without a cut id
    describes: str | None    # one-line semantic role (filled by Pass B)


@dataclass
class ParentView:
    label: str               # e.g. "main_top"
    cut_ids: list[str] = field(default_factory=list)  # cut markers detected inside this parent's bbox


@dataclass
class PageGraph:
    page_index: int
    parents: list[ParentView] = field(default_factory=list)
    derived: list[DerivedView] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)  # labels of title_block / revision_block / notes regions
    part_summary: str | None = None
    raw_pass_a: dict[str, Any] | None = None
    raw_pass_b: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Pass A — deterministic cut-marker geometry
# ---------------------------------------------------------------------------

# A "cut marker" on a drawing looks like: a single capital letter sitting next
# to an arrow glyph, with another identical letter+arrow somewhere across the
# parent view to define the cut line. The accompanying derived view is labeled
# "SECTION X-X" elsewhere on the page.
#
# We detect the markers by their text content (single capital letter) and match
# them up by letter. The presence of *two* such letters inside the same view
# rectangle is what tells us "this rectangle is a parent view with cut X."
_SECTION_LABEL_RE = re.compile(r"\bSECTION\s+([A-Z])-\1\b", re.IGNORECASE)
_DETAIL_LABEL_RE = re.compile(r"\bDETAIL\s+([A-Z])\b", re.IGNORECASE)
_CUT_LETTER_RE = re.compile(r"^[A-Z]$")


def _bbox_contains_point(bbox: tuple[float, float, float, float], x: float, y: float) -> bool:
    x0, y0, x1, y1 = bbox
    return x0 <= x <= x1 and y0 <= y <= y1


def _crop_bbox(vc: ViewCrop) -> tuple[float, float, float, float]:
    return (vc.bbox_x0, vc.bbox_y0, vc.bbox_x1, vc.bbox_y1)


def _find_cut_markers_in_parent(parent_bbox: tuple[float, float, float, float],
                                text_blocks) -> dict[str, int]:
    """Return {letter: count} for single-capital text blocks falling inside the parent bbox.

    A letter appearing exactly twice inside one parent's bbox is a confident
    cut marker (start + end of the cut line).
    """
    counts: dict[str, int] = {}
    for tb in text_blocks:
        word = tb.text.strip()
        if not _CUT_LETTER_RE.match(word):
            continue
        cx = (tb.bbox_x0 + tb.bbox_x1) / 2.0
        cy = (tb.bbox_y0 + tb.bbox_y1) / 2.0
        if _bbox_contains_point(parent_bbox, cx, cy):
            counts[word] = counts.get(word, 0) + 1
    return counts


def _label_for_section(section_letter: str) -> str:
    return f"SECTION {section_letter}-{section_letter}"


def _detect_section_views_in_crops(view_crops: list[ViewCrop]) -> dict[str, ViewCrop]:
    """Return {cut_id: ViewCrop} for any crop whose label contains 'SECTION X-X'.

    cut_id is "A-A" style (matching what shows up on the drawing as cut line letter).
    """
    out: dict[str, ViewCrop] = {}
    for vc in view_crops:
        lbl = (vc.view_label or "").upper()
        m = _SECTION_LABEL_RE.search(lbl)
        if m:
            letter = m.group(1).upper()
            out[f"{letter}-{letter}"] = vc
    return out


def _build_pass_a(parsed_page: ParsedPage, view_crops: list[ViewCrop]) -> dict[str, Any]:
    """Deterministic topology from cut-marker geometry. Returns plain JSON-able dict."""
    if parsed_page.is_raster:
        return {"parents": [], "derived": [], "skipped": "raster"}

    # 1. Find every crop whose label contains "SECTION X-X". These are derived.
    section_crops = _detect_section_views_in_crops(view_crops)

    # 2. For every non-section crop, count single-capital cut-marker glyphs
    #    inside its bbox. A crop containing the *pair* of letters that match
    #    a SECTION X-X derived view is the parent for that section.
    parent_candidates: list[tuple[ViewCrop, dict[str, int]]] = []
    for vc in view_crops:
        if (vc.view_label or "").upper() in {sc.view_label.upper() for sc in section_crops.values()}:
            continue  # this is itself a section crop; can't be its own parent
        marker_counts = _find_cut_markers_in_parent(_crop_bbox(vc), parsed_page.text_blocks)
        if marker_counts:
            parent_candidates.append((vc, marker_counts))

    # 3. For each derived section, pick the parent with the matching letter
    #    appearing at least twice (i.e. the cut line is fully inside it).
    derived_records: list[dict[str, Any]] = []
    parent_records: dict[str, dict[str, Any]] = {}

    for cut_id, derived_vc in section_crops.items():
        letter = cut_id.split("-")[0]
        # Pick the largest parent containing letter≥2.
        candidates = [
            (pvc, counts) for (pvc, counts) in parent_candidates
            if counts.get(letter, 0) >= 2
        ]
        if not candidates:
            # Fallback: a parent that contains the letter at all (some drawings
            # only render one arrow glyph as text and the second as pure draw).
            candidates = [
                (pvc, counts) for (pvc, counts) in parent_candidates
                if counts.get(letter, 0) >= 1
            ]
        if not candidates:
            # Last resort: largest non-section crop on the page.
            candidates = [(pvc, counts) for (pvc, counts) in parent_candidates] or [
                (vc, {}) for vc in view_crops if vc not in section_crops.values()
            ]
            if not candidates:
                continue

        # Choose the largest by area (parent views are physically the largest).
        parent_vc, _ = max(candidates, key=lambda pc: pc[0].area_fraction)
        parent_label = parent_vc.view_label

        derived_records.append({
            "label":        derived_vc.view_label,
            "parent_label": parent_label,
            "cut_id":       cut_id,
        })
        rec = parent_records.setdefault(parent_label, {"label": parent_label, "cut_ids": []})
        if cut_id not in rec["cut_ids"]:
            rec["cut_ids"].append(cut_id)

    # 4. Any non-section crop that wasn't already named a parent above is a
    #    candidate parent (could be lone top view or unannotated isometric).
    section_labels = {sc.view_label for sc in section_crops.values()}
    for vc in view_crops:
        if vc.view_label in section_labels:
            continue
        parent_records.setdefault(vc.view_label, {"label": vc.view_label, "cut_ids": []})

    return {
        "parents": list(parent_records.values()),
        "derived": derived_records,
    }


# ---------------------------------------------------------------------------
# Pass B — LLM annotation
# ---------------------------------------------------------------------------

_PASS_B_SCHEMA_HINT = (
    'Return JSON with exact keys: '
    '{"part_summary": "...", '
    '"label_corrections": {"<crop_index>": "<corrected_label>"}, '
    '"derived": [{"label": "<corrected_label>", "parent_label": "...", '
    '"cut_id": "A-A|B-B|...", "describes": "..."}], '
    '"missed_derived": [{"label": "...", "parent_label": "...", "describes": "..."}]}'
)


def _load_pass_b_prompt() -> str:
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "prompts" / "page_graph.txt"
    return p.read_text()


def _png_bytes_from_array(image: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    return buf.getvalue()


def _downscale_png(png_bytes: bytes, max_dim: int) -> bytes:
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    if max(w, h) <= max_dim:
        return png_bytes
    scale = max_dim / float(max(w, h))
    img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def _call_pass_b(page_image: np.ndarray, pass_a: dict[str, Any],
                 view_crops: list[ViewCrop], model: str) -> dict[str, Any]:
    """Invoke the LLM with the downscaled page + Pass A's partial topology + crop bboxes.

    The LLM may return `label_corrections` mapping crop_index → corrected_label
    when the segmenter's text-extraction produced a wrong label (common when
    multiple views share text near the rectangle border). These corrections
    are applied to view_crops by build_page_graph BEFORE Pass A is re-run.
    """
    if not config.anthropic_api_key:
        log.warning("Pass B skipped: ANTHROPIC_API_KEY not set")
        return {}

    page_png = _downscale_png(_png_bytes_from_array(page_image), config.max_image_dimension)
    system_prompt = _load_pass_b_prompt()

    crop_descriptors = [
        {
            "crop_index": i,
            "current_label": vc.view_label,
            "bbox_pdf_pt": [round(vc.bbox_x0, 1), round(vc.bbox_y0, 1),
                            round(vc.bbox_x1, 1), round(vc.bbox_y1, 1)],
            "area_fraction": round(vc.area_fraction, 3),
        }
        for i, vc in enumerate(view_crops)
    ]

    pass_a_json = json.dumps(pass_a, indent=2)
    user_text = (
        "Crops detected by the segmenter (with PDF-point bboxes; origin top-left):\n"
        + json.dumps(crop_descriptors, indent=2)
        + "\n\nDeterministic Pass A topology (from vector cut-marker geometry — may be empty):\n"
        + pass_a_json
        + "\n\n" + _PASS_B_SCHEMA_HINT
    )

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=config.max_pre_pass_tokens,
        temperature=config.temperature,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(page_png).decode(),
                    },
                },
            ],
        }],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    try:
        return json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError as e:
        log.warning("Pass B returned malformed JSON (%s); using Pass A only", e)
        return {}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_page_graph(parsed_page: ParsedPage,
                     view_crops: list[ViewCrop],
                     *,
                     run_llm_pass: bool = True) -> PageGraph:
    """Build a PageGraph and mutate `view_crops` in place with graph annotations.

    After this call, every ViewCrop has its view_role / parent_label / cut_id /
    describes fields populated. Downstream stages just read those fields.

    Args:
        parsed_page:   Output of pipeline.parser.parse_pdf for one page.
        view_crops:    Output of pipeline.segmentor.segment_views for the same page.
        run_llm_pass:  If False, skip Pass B (useful for tests / no-API runs).
    """
    # Initial Pass A on raw segmenter labels — usually empty when labels are
    # contaminated, but cheap to compute and surfaces information for Pass B.
    pass_a = _build_pass_a(parsed_page, view_crops)

    pass_b: dict[str, Any] = {}
    if run_llm_pass:
        try:
            pass_b = _call_pass_b(
                parsed_page.image,
                pass_a,
                view_crops,
                config.pre_pass_model,
            )
        except Exception as e:  # noqa: BLE001 — never let pre-pass crash the run
            log.warning("Pass B failed (%s); using Pass A only", e)
            pass_b = {}

    # Apply label corrections from Pass B before re-deriving topology. The LLM
    # sees the page image and the crop bboxes, so it can rename crops that the
    # segmenter mislabeled (e.g. "SIDE" → "SECTION B-B"). We trust corrections
    # only when the new label looks like a real view label.
    corrections = pass_b.get("label_corrections") or {}
    if corrections:
        for k, new_label in corrections.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(view_crops) and isinstance(new_label, str) and new_label.strip():
                view_crops[idx].view_label = new_label.strip().upper()
        # Re-run Pass A now that labels are corrected — cut-marker geometry can
        # only pair to SECTION X-X labels that actually exist.
        pass_a = _build_pass_a(parsed_page, view_crops)

    # Index Pass A derived edges by label so we can attach them to crops.
    derived_by_label: dict[str, dict[str, Any]] = {
        d["label"]: d for d in pass_a.get("derived", [])
    }
    parent_labels: set[str] = {p["label"] for p in pass_a.get("parents", [])}

    # Pass B describes-fields keyed by (lowered) label, only for labels Pass A
    # already knew about. Pass B cannot rearrange topology.
    describes_by_label: dict[str, str] = {}
    for d in (pass_b.get("derived") or []):
        lbl = str(d.get("label") or "").strip()
        if lbl:
            describes_by_label[lbl] = str(d.get("describes") or "") or None  # type: ignore[assignment]

    # Mutate each ViewCrop in place.
    for vc in view_crops:
        if vc.view_label in derived_by_label:
            d = derived_by_label[vc.view_label]
            vc.view_role = "derived"
            vc.parent_label = d["parent_label"]
            vc.cut_id = d.get("cut_id")
            vc.describes = describes_by_label.get(vc.view_label)
        elif vc.view_label in parent_labels:
            vc.view_role = "parent"
            vc.parent_label = None
            vc.cut_id = None
            vc.describes = describes_by_label.get(vc.view_label)
        else:
            # Unknown view — leave default ("parent") so we don't accidentally
            # collapse it into someone else's findings.
            vc.view_role = "parent"

    parents = [ParentView(label=p["label"], cut_ids=list(p.get("cut_ids") or []))
               for p in pass_a.get("parents", [])]
    derived = [DerivedView(label=d["label"],
                           parent_label=d["parent_label"],
                           cut_id=d.get("cut_id"),
                           describes=describes_by_label.get(d["label"]))
               for d in pass_a.get("derived", [])]

    return PageGraph(
        page_index=view_crops[0].page_index if view_crops else 0,
        parents=parents,
        derived=derived,
        annotations=[],
        part_summary=(pass_b.get("part_summary") or None),
        raw_pass_a=pass_a,
        raw_pass_b=pass_b or None,
    )


def graph_to_dict(g: PageGraph) -> dict[str, Any]:
    return {
        "page_index": g.page_index,
        "parents":    [{"label": p.label, "cut_ids": p.cut_ids} for p in g.parents],
        "derived":    [{"label": d.label, "parent_label": d.parent_label,
                        "cut_id": d.cut_id, "describes": d.describes} for d in g.derived],
        "part_summary": g.part_summary,
    }
