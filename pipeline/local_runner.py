"""Local (no-S3, no-Redis, no-RQ) orchestrator for the DrawDiff pipeline.

Mirrors `pipeline.run.run_comparison` but reads/writes everything under a
single local `job_dir`. The relative artifact key shape (`pages/...`,
`crops/<job_id>/<kind>/view_NN.png`, `metadata/<job_id>/...`) is preserved so
that downstream stage code (notably `pipeline.comparator._download_png`) can
be redirected with a single monkeypatch instead of signature changes.

Branch `lseg` change: after the parsing step, segmentation AND matching are
performed by a single Claude vision call (`pipeline.llm_segmenter`) instead
of the geometry-based segmentor + rapidfuzz matcher. Everything downstream
(extractor → comparator → aggregator → annotator → highlighter → report) is
unchanged because the artifacts produced (`view_metadata`, `match_manifest`)
have the same shape they did before.
"""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from config import config
from pipeline import comparator as _comparator_mod
from pipeline.aggregator import aggregate
from pipeline.annotator import build_side_by_side
from pipeline.bbox_snap import snap_view_bboxes_to_text
from pipeline.change_locator import annotate_change_locations
from pipeline.comparator import compare_views, viewdiff_to_dict
from pipeline.extractor import extract_dimensions, extract_revision_block, extract_title_block
from pipeline.highlighter import highlight_page
from pipeline.llm_segmenter import LLMMatch, segment_and_match
from pipeline.parser import ParsedPage, parse_pdf
from report.generator import count_summary, generate_report

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Local I/O — mirrors pipeline.run S3 helpers, swapping bucket for job_dir
# ---------------------------------------------------------------------------

def _write_bytes(job_dir: Path, key: str, data: bytes) -> Path:
    path = job_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_json(job_dir: Path, key: str, data: Any) -> Path:
    return _write_bytes(job_dir, key, json.dumps(data, default=str, indent=2).encode())


def _write_image(job_dir: Path, key: str, image: np.ndarray) -> Path:
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    return _write_bytes(job_dir, key, buf.getvalue())


def _read_bytes(job_dir: Path, key: str) -> bytes:
    return (job_dir / key).read_bytes()


def _install_local_crop_loader(job_dir: Path) -> Any:
    """Redirect comparator's S3 PNG reader at the local job_dir."""
    previous = _comparator_mod._download_png

    def _local_download_png(key: str) -> bytes:
        return (job_dir / key).read_bytes()

    _comparator_mod._download_png = _local_download_png
    return previous


def _stage(job_id: str, name: str, pct: int) -> None:
    log.info("[%s] %s %d%%", job_id, name, pct)


# ---------------------------------------------------------------------------
# Per-document parsing (no segmentation here — that happens via the LLM
# downstream once both PDFs are parsed)
# ---------------------------------------------------------------------------

@dataclass
class _ParsedDoc:
    pages: list[ParsedPage]                # rendered + text-block pages
    page_keys: list[str]                   # disk keys for each rendered page
    page_dims_pt: list[tuple[float, float]]  # (page_width_pt, page_height_pt) per page
    dims_by_page: dict[int, list]          # extract_dimensions output per page
    title_block: Any                       # extractor.TitleBlock
    revision_block: Any                    # extractor.RevisionBlock
    any_raster: bool


def _parse_document(pdf_bytes: bytes, job_id: str, kind: str, job_dir: Path) -> _ParsedDoc:
    """Render every page of `pdf_bytes`, persist page PNGs, and run text-only
    extraction (dimensions per page, title block, revision block).

    Segmentation is intentionally NOT done here — it is performed once by
    `pipeline.llm_segmenter.segment_and_match` after both sides are parsed.
    """
    import fitz as _fitz

    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    num_pages = len(doc)
    doc.close()

    log.info("[%s] Parsing %s PDF (%d bytes, %d page(s))",
             job_id, kind, len(pdf_bytes), num_pages)

    pages: list[ParsedPage] = []
    page_keys: list[str] = []
    page_dims_pt: list[tuple[float, float]] = []
    dims_by_page: dict[int, list] = {}
    any_raster = False

    for page_index in range(num_pages):
        page = parse_pdf(pdf_bytes, page_index=page_index)
        any_raster = any_raster or page.is_raster

        page_key = f"pages/{job_id}/{kind}_p{page_index:02d}.png"
        _write_image(job_dir, page_key, page.image)

        pages.append(page)
        page_keys.append(page_key)
        page_dims_pt.append((page.page_width_pt, page.page_height_pt))
        dims_by_page[page_index] = extract_dimensions(page.text_blocks)

    return _ParsedDoc(
        pages=pages,
        page_keys=page_keys,
        page_dims_pt=page_dims_pt,
        dims_by_page=dims_by_page,
        title_block=extract_title_block(pdf_bytes),
        revision_block=extract_revision_block(pdf_bytes),
        any_raster=any_raster,
    )


# ---------------------------------------------------------------------------
# Crop persistence + view metadata assembly (post-LLM segmentation)
# ---------------------------------------------------------------------------

def _crop_norm(image: np.ndarray, bbox_norm: tuple[float, float, float, float]) -> np.ndarray:
    h, w = image.shape[:2]
    x0, y0, x1, y1 = bbox_norm
    px0 = max(0, int(round(x0 * w)))
    py0 = max(0, int(round(y0 * h)))
    px1 = min(w, int(round(x1 * w)))
    py1 = min(h, int(round(y1 * h)))
    if px1 <= px0 or py1 <= py0:
        return np.zeros((1, 1, 3), dtype=image.dtype)
    return image[py0:py1, px0:px1]


def _snap_match_bboxes_to_text(
    matches: list[LLMMatch],
    orig_parsed: _ParsedDoc,
    rev_parsed: _ParsedDoc,
) -> None:
    """Snap each LLM-emitted view bbox so no view edge cuts annotation text.

    Mutates `matches` in place. Runs per side, per page: gathers the page's
    text-token bboxes (PyMuPDF word granularity, in PDF points), converts
    each match's normalized bbox to PDF points, runs the constraint-based
    snap from `pipeline.bbox_snap`, then writes the result back as a
    normalized [0,1] tuple.
    """
    for side, parsed in (("original", orig_parsed), ("revised", rev_parsed)):
        bbox_attr = "orig_bbox_norm" if side == "original" else "rev_bbox_norm"
        n_pages = len(parsed.pages)

        for pi in range(n_pages):
            page_w_pt, page_h_pt = parsed.page_dims_pt[pi]
            if page_w_pt <= 0 or page_h_pt <= 0:
                continue

            text_bboxes_pt: list[tuple[float, float, float, float]] = [
                (tb.bbox_x0, tb.bbox_y0, tb.bbox_x1, tb.bbox_y1)
                for tb in parsed.pages[pi].text_blocks
            ]

            page_match_indices: list[int] = []
            page_match_bboxes_pt: list[tuple[float, float, float, float]] = []
            for mi, m in enumerate(matches):
                if m.page_index != pi:
                    continue
                bbox_norm = getattr(m, bbox_attr)
                if bbox_norm is None:
                    continue
                x0n, y0n, x1n, y1n = bbox_norm
                page_match_indices.append(mi)
                page_match_bboxes_pt.append((
                    x0n * page_w_pt, y0n * page_h_pt,
                    x1n * page_w_pt, y1n * page_h_pt,
                ))

            if not page_match_bboxes_pt:
                continue

            new_bboxes_pt = snap_view_bboxes_to_text(
                page_match_bboxes_pt, text_bboxes_pt,
            )

            for mi_idx, new_pt in zip(page_match_indices, new_bboxes_pt):
                m = matches[mi_idx]
                new_norm = (
                    max(0.0, min(1.0, new_pt[0] / page_w_pt)),
                    max(0.0, min(1.0, new_pt[1] / page_h_pt)),
                    max(0.0, min(1.0, new_pt[2] / page_w_pt)),
                    max(0.0, min(1.0, new_pt[3] / page_h_pt)),
                )
                if new_norm[2] <= new_norm[0] or new_norm[3] <= new_norm[1]:
                    continue   # degenerate; keep the LLM's original
                if side == "original":
                    m.orig_bbox_norm = new_norm
                else:
                    m.rev_bbox_norm = new_norm

    log.info("Snapped match bboxes to text-token boundaries")


def _build_side_views(
    job_id: str,
    job_dir: Path,
    kind: str,                     # "original" | "revised"
    matches: list[LLMMatch],
    parsed: _ParsedDoc,
) -> tuple[list[dict], dict[int, int]]:
    """Persist per-view crops for one side and build its view_metadata list.

    Returns:
        view_metadata: list of dicts (same shape pipeline.run produces).
        match_to_view_index: maps LLMMatch list-index → view_metadata.index
                             (so the match_manifest can resolve crop_s3_key).
    """
    bbox_attr = "orig_bbox_norm" if kind == "original" else "rev_bbox_norm"

    view_metadata: list[dict] = []
    match_to_view_index: dict[int, int] = {}
    next_index = 0

    for mi, m in enumerate(matches):
        bbox_norm = getattr(m, bbox_attr)
        if bbox_norm is None:
            continue  # this match has no view on this side (added/removed)

        page_image = parsed.pages[m.page_index].image
        crop = _crop_norm(page_image, bbox_norm)

        crop_key = f"crops/{job_id}/{kind}/view_{next_index:02d}.png"
        _write_image(job_dir, crop_key, crop)

        page_w_pt, page_h_pt = parsed.page_dims_pt[m.page_index]
        x0n, y0n, x1n, y1n = bbox_norm
        bbox_pt = (x0n * page_w_pt, y0n * page_h_pt, x1n * page_w_pt, y1n * page_h_pt)
        area_fraction = (x1n - x0n) * (y1n - y0n)

        # Filter dimensions falling inside this view's bbox (PDF-point space).
        page_dims = parsed.dims_by_page.get(m.page_index, [])
        view_dims = [
            d for d in page_dims
            if bbox_pt[0] <= d.bbox_x0 <= bbox_pt[2]
            and bbox_pt[1] <= d.bbox_y0 <= bbox_pt[3]
        ]

        # Also keep ALL text tokens that fall inside this view's bbox, so the
        # report-time annotator can locate change values that aren't dimension
        # regex-matches (notes, callouts, multi-token labels, etc.).
        page_text = parsed.pages[m.page_index].text_blocks
        view_text_blocks = [
            {"text": tb.text,
             "bbox": [tb.bbox_x0, tb.bbox_y0, tb.bbox_x1, tb.bbox_y1]}
            for tb in page_text
            if bbox_pt[0] <= tb.bbox_x0 <= bbox_pt[2]
            and bbox_pt[1] <= tb.bbox_y0 <= bbox_pt[3]
        ]

        view_metadata.append({
            "index":         next_index,
            "page_index":    m.page_index,
            "view_label":    m.label,
            "area_fraction": area_fraction,
            "bbox":          [bbox_pt[0], bbox_pt[1], bbox_pt[2], bbox_pt[3]],
            "crop_s3_key":   crop_key,
            "dimensions":    [{"value": d.value, "numeric": d.numeric,
                               "bbox": [d.bbox_x0, d.bbox_y0, d.bbox_x1, d.bbox_y1]}
                              for d in view_dims],
            "text_blocks":   view_text_blocks,
        })
        match_to_view_index[mi] = next_index
        next_index += 1

    return view_metadata, match_to_view_index


def _doc_payload(parsed: _ParsedDoc, view_metadata: list[dict]) -> dict:
    """Repackage parsed + per-side view_metadata into the extraction.json
    sub-tree shape that the rest of the pipeline already expects.
    """
    page_w_pt = parsed.page_dims_pt[0][0] if parsed.page_dims_pt else 0.0
    page_h_pt = parsed.page_dims_pt[0][1] if parsed.page_dims_pt else 0.0

    tb = parsed.title_block
    rb = parsed.revision_block

    return {
        "views":          view_metadata,
        "is_raster":      parsed.any_raster,
        "page_width_pt":  page_w_pt,
        "page_height_pt": page_h_pt,
        "page_s3_keys":   parsed.page_keys,
        "title_block":    {
            "part_number": tb.part_number,
            "revision":    tb.revision,
            "material":    tb.material,
            "tolerance":   tb.tolerance,
            "drawn_by":    tb.drawn_by,
            "date":        tb.date,
            "raw_text":    tb.raw_text,
        },
        "revision_block": {
            "entries":  rb.entries,
            "raw_text": rb.raw_text,
        },
    }


def _build_match_manifest(
    matches: list[LLMMatch],
    orig_views: list[dict],
    rev_views: list[dict],
    orig_match_to_idx: dict[int, int],
    rev_match_to_idx: dict[int, int],
) -> list[dict]:
    """Convert LLMMatch list (plus the resolved per-side view_metadata
    indices) into the match_manifest dicts the comparator expects.

    Always prepends two synthetic entries (title_block, revision_block) so
    the deterministic structural diffs in `comparator._diff_title_block` and
    `_diff_revision_block` still run.
    """
    manifest: list[dict] = [
        {
            "orig_index":       None,
            "rev_index":        None,
            "label":            "title_block",
            "score":            1.0,
            "match_type":       "title_block",
            "orig_crop_s3_key": None,
            "rev_crop_s3_key":  None,
        },
        {
            "orig_index":       None,
            "rev_index":        None,
            "label":            "revision_block",
            "score":            1.0,
            "match_type":       "revision_block",
            "orig_crop_s3_key": None,
            "rev_crop_s3_key":  None,
        },
    ]

    for mi, m in enumerate(matches):
        orig_view_idx = orig_match_to_idx.get(mi)
        rev_view_idx  = rev_match_to_idx.get(mi)

        orig_crop_key = orig_views[orig_view_idx]["crop_s3_key"] if orig_view_idx is not None else None
        rev_crop_key  = rev_views[rev_view_idx]["crop_s3_key"]   if rev_view_idx  is not None else None

        manifest.append({
            "orig_index":       orig_views[orig_view_idx]["index"] if orig_view_idx is not None else None,
            "rev_index":        rev_views[rev_view_idx]["index"]   if rev_view_idx  is not None else None,
            "label":            m.label,
            "score":            round(float(m.confidence), 4),
            "match_type":       m.match_type,
            "orig_crop_s3_key": orig_crop_key,
            "rev_crop_s3_key":  rev_crop_key,
        })

    return manifest


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_comparison_local(
    job_id: str,
    original_pdf: bytes,
    revised_pdf: bytes,
    part_number: str,
    notes: str,
    job_dir: Path,
) -> dict:
    """Run the full DrawDiff pipeline locally."""
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    previous_download = _install_local_crop_loader(job_dir)
    try:
        _stage(job_id, "parsing", 5)

        _write_bytes(job_dir, config.s3_artifact_key(job_id, "original"), original_pdf)
        _write_bytes(job_dir, config.s3_artifact_key(job_id, "revised"), revised_pdf)

        _stage(job_id, "parsing (original)", 15)
        orig_parsed = _parse_document(original_pdf, job_id, "original", job_dir)
        _stage(job_id, "parsing (revised)", 30)
        rev_parsed  = _parse_document(revised_pdf,  job_id, "revised",  job_dir)

        _stage(job_id, "LLM seg+match", 50)
        matches = segment_and_match(
            orig_pages=[p.image for p in orig_parsed.pages],
            rev_pages=[p.image for p in rev_parsed.pages],
        )
        log.info("[%s] LLM produced %d view match(es)", job_id, len(matches))

        _stage(job_id, "snapping bboxes to text", 65)
        _snap_match_bboxes_to_text(matches, orig_parsed, rev_parsed)

        _stage(job_id, "writing crops + extraction", 70)
        orig_views, orig_m2v = _build_side_views(job_id, job_dir, "original", matches, orig_parsed)
        rev_views,  rev_m2v  = _build_side_views(job_id, job_dir, "revised",  matches, rev_parsed)

        orig_doc_payload = _doc_payload(orig_parsed, orig_views)
        rev_doc_payload  = _doc_payload(rev_parsed,  rev_views)

        extraction_payload = {
            "job_id":      job_id,
            "part_number": part_number,
            "notes":       notes,
            "original":    orig_doc_payload,
            "revised":     rev_doc_payload,
        }
        _write_json(job_dir, f"metadata/{job_id}/extraction.json", extraction_payload)

        log.info(
            "[%s] Extraction complete — orig views=%d, rev views=%d",
            job_id, len(orig_views), len(rev_views),
        )

        _stage(job_id, "matching (LLM)", 80)
        match_manifest = _build_match_manifest(
            matches, orig_views, rev_views, orig_m2v, rev_m2v,
        )
        _write_json(job_dir, f"metadata/{job_id}/matches.json", {
            "job_id":  job_id,
            "matches": match_manifest,
        })
        log.info("[%s] Matching complete — %d match(es)", job_id, len(match_manifest))

        _stage(job_id, "comparing (LLM)", 88)
        view_diffs = compare_views(job_id, match_manifest, extraction_payload)

        view_diffs_dicts = [viewdiff_to_dict(vd) for vd in view_diffs]
        for vd_dict, m in zip(view_diffs_dicts, match_manifest):
            vd_dict["orig_index"] = m.get("orig_index")
            vd_dict["rev_index"]  = m.get("rev_index")
        view_diffs_dicts = aggregate(view_diffs_dicts)

        # Resolve per-change locations + zone labels (LLM-provided bbox first,
        # text-anchor fallback otherwise). Mutates each change dict in place.
        annotate_change_locations(
            view_diffs=view_diffs_dicts,
            orig_views_by_idx={v["index"]: v for v in orig_views},
            rev_views_by_idx={v["index"]: v for v in rev_views},
            page_dims_pt={pi: dims for pi, dims in enumerate(orig_parsed.page_dims_pt)},
        )

        changeset_dict = {"job_id": job_id, "view_diffs": view_diffs_dicts}
        _write_json(job_dir, f"metadata/{job_id}/changeset.json", changeset_dict)

        log.info("[%s] Comparison complete — %d view diff(s)", job_id, len(view_diffs_dicts))

        _stage(job_id, "annotating + report", 95)

        # Side-by-side per non-text-only view
        orig_views_by_idx = {v["index"]: v for v in orig_views}
        rev_views_by_idx  = {v["index"]: v for v in rev_views}
        view_images: dict[int, bytes] = {}
        for i, vd in enumerate(changeset_dict["view_diffs"]):
            if vd["match_type"] in ("title_block", "revision_block"):
                continue

            orig_idx = vd.get("orig_index")
            rev_idx  = vd.get("rev_index")
            orig_key = f"crops/{job_id}/original/view_{orig_idx:02d}.png" if orig_idx is not None else None
            rev_key  = f"crops/{job_id}/revised/view_{rev_idx:02d}.png"  if rev_idx  is not None else None

            orig_png = _read_bytes(job_dir, orig_key) if orig_key else None
            rev_png  = _read_bytes(job_dir, rev_key)  if rev_key  else None

            view_images[i] = build_side_by_side(
                orig_png, rev_png, vd["label"],
                changes=vd.get("changes") or [],
                orig_view=orig_views_by_idx.get(orig_idx) if orig_idx is not None else None,
                rev_view=rev_views_by_idx.get(rev_idx)   if rev_idx  is not None else None,
            )

        # Highlighted full-page renders
        orig_keys = orig_parsed.page_keys
        rev_keys  = rev_parsed.page_keys
        if len(orig_keys) != len(rev_keys):
            log.warning(
                "[%s] Page count mismatch: original=%d revised=%d",
                job_id, len(orig_keys), len(rev_keys),
            )

        orig_views_by_page: dict[int, list] = {}
        for v in orig_views:
            orig_views_by_page.setdefault(v["page_index"], []).append(v)
        rev_views_by_page: dict[int, list] = {}
        for v in rev_views:
            rev_views_by_page.setdefault(v["page_index"], []).append(v)

        orig_highlighted_paths: list[Path] = []
        orig_highlighted_pages: list[bytes] = []
        for pi, key in enumerate(orig_keys):
            page_png = _read_bytes(job_dir, key)
            png = highlight_page(page_png, view_diffs_dicts, orig_views_by_page.get(pi, []), "original")
            orig_highlighted_pages.append(png)
            orig_highlighted_paths.append(
                _write_bytes(job_dir, f"highlighted/original_p{pi:02d}.png", png)
            )

        rev_highlighted_paths: list[Path] = []
        rev_highlighted_pages: list[bytes] = []
        for pi, key in enumerate(rev_keys):
            page_png = _read_bytes(job_dir, key)
            png = highlight_page(page_png, view_diffs_dicts, rev_views_by_page.get(pi, []), "revised")
            rev_highlighted_pages.append(png)
            rev_highlighted_paths.append(
                _write_bytes(job_dir, f"highlighted/revised_p{pi:02d}.png", png)
            )

        pdf_bytes = generate_report(
            job_id=job_id,
            changeset=changeset_dict,
            extraction=extraction_payload,
            part_number=part_number,
            notes=notes,
            view_images=view_images,
            highlighted_pages={"original": orig_highlighted_pages, "revised": rev_highlighted_pages},
        )

        report_path     = _write_bytes(job_dir, "report.pdf", pdf_bytes)
        changeset_path  = _write_json(job_dir, "changeset.json", changeset_dict)

        summary = count_summary(changeset_dict["view_diffs"])
        log.info(
            "[%s] Report written — critical=%d significant=%d minor=%d uncertain=%d views=%d",
            job_id,
            summary.critical, summary.significant, summary.minor,
            summary.uncertain, summary.total_views_compared,
        )
        _stage(job_id, "complete", 100)

        return {
            "job_dir":          job_dir,
            "report_path":      report_path,
            "changeset_path":   changeset_path,
            "highlighted_pages": {
                "original": orig_highlighted_paths,
                "revised":  rev_highlighted_paths,
            },
            "summary": summary,
        }
    finally:
        _comparator_mod._download_png = previous_download
