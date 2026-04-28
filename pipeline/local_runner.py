"""Local (no-S3, no-Redis, no-RQ) orchestrator for the DrawDiff pipeline.

Mirrors `pipeline.run.run_comparison` but reads/writes everything under a
single local `job_dir`. The relative artifact key shape (`pages/...`,
`crops/<job_id>/<kind>/view_NN.png`, `metadata/<job_id>/...`) is preserved so
that downstream stage code (notably `pipeline.comparator._download_png`) can
be redirected with a single monkeypatch instead of signature changes.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from config import config
from pipeline import comparator as _comparator_mod
from pipeline.aggregator import aggregate
from pipeline.annotator import build_side_by_side
from pipeline.comparator import compare_views, viewdiff_to_dict
from pipeline.extractor import extract_dimensions, extract_revision_block, extract_title_block
from pipeline.highlighter import highlight_page
from pipeline.matcher import ViewMatch, match_views
from pipeline.parser import parse_pdf
from pipeline.segmentor import ViewCrop, segment_views
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
    """Redirect comparator's S3 PNG reader at the local job_dir.

    Returns the previous function so it can be restored after the run.
    The comparator was written to fetch crops via boto3; in local mode the
    same key (e.g. ``crops/<job_id>/original/view_03.png``) is just a path.
    """
    previous = _comparator_mod._download_png

    def _local_download_png(key: str) -> bytes:
        return (job_dir / key).read_bytes()

    _comparator_mod._download_png = _local_download_png
    return previous


def _stage(job_id: str, name: str, pct: int) -> None:
    log.info("[%s] %s %d%%", job_id, name, pct)


# ---------------------------------------------------------------------------
# Per-document processing — local mirror of pipeline.run._process_document
# ---------------------------------------------------------------------------

def _process_document(pdf_bytes: bytes, job_id: str, kind: str, job_dir: Path) -> dict:
    import fitz as _fitz

    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    num_pages = len(doc)
    doc.close()

    log.info("[%s] Parsing %s PDF (%d bytes, %d page(s))", job_id, kind, len(pdf_bytes), num_pages)

    all_view_crops: list[ViewCrop] = []
    all_dims_by_page: dict[int, list] = {}
    page_keys: list[str] = []
    page_width_pt = 0.0
    page_height_pt = 0.0
    any_raster = False

    for page_index in range(num_pages):
        page = parse_pdf(pdf_bytes, page_index=page_index)
        any_raster = any_raster or page.is_raster

        if page_index == 0:
            page_width_pt = page.page_width_pt
            page_height_pt = page.page_height_pt

        page_key = f"pages/{job_id}/{kind}_p{page_index:02d}.png"
        _write_image(job_dir, page_key, page.image)
        page_keys.append(page_key)

        log.info("[%s] Segmenting %s page %d (raster=%s)", job_id, kind, page_index, page.is_raster)
        view_crops = segment_views(page, pdf_bytes, page_index=page_index)
        dims = extract_dimensions(page.text_blocks)

        all_view_crops.extend(view_crops)
        all_dims_by_page[page_index] = dims

    title_block = extract_title_block(pdf_bytes)
    rev_block = extract_revision_block(pdf_bytes)

    log.info("[%s] %s → %d view(s) across %d page(s)", job_id, kind, len(all_view_crops), num_pages)

    view_metadata: list[dict] = []
    for i, vc in enumerate(all_view_crops):
        crop_key = f"crops/{job_id}/{kind}/view_{i:02d}.png"
        _write_image(job_dir, crop_key, vc.image)

        page_dims = all_dims_by_page.get(vc.page_index, [])
        view_dims = [
            d for d in page_dims
            if vc.bbox_x0 <= d.bbox_x0 <= vc.bbox_x1 and vc.bbox_y0 <= d.bbox_y0 <= vc.bbox_y1
        ]

        view_metadata.append({
            "index":         i,
            "page_index":    vc.page_index,
            "view_label":    vc.view_label,
            "area_fraction": vc.area_fraction,
            "bbox":          [vc.bbox_x0, vc.bbox_y0, vc.bbox_x1, vc.bbox_y1],
            "crop_s3_key":   crop_key,  # kept under same name for compat with comparator/annotator
            "dimensions":    [{"value": d.value, "numeric": d.numeric,
                               "bbox": [d.bbox_x0, d.bbox_y0, d.bbox_x1, d.bbox_y1]}
                              for d in view_dims],
        })

    return {
        "views":          view_metadata,
        "is_raster":      any_raster,
        "page_width_pt":  page_width_pt,
        "page_height_pt": page_height_pt,
        "page_s3_keys":   page_keys,
        "title_block":    {
            "part_number": title_block.part_number,
            "revision":    title_block.revision,
            "material":    title_block.material,
            "tolerance":   title_block.tolerance,
            "drawn_by":    title_block.drawn_by,
            "date":        title_block.date,
            "raw_text":    title_block.raw_text,
        },
        "revision_block": {
            "entries": rev_block.entries,
            "raw_text": rev_block.raw_text,
        },
    }


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
    """Run the full DrawDiff pipeline locally. Returns a dict of result paths
    and the ChangeSummary. Raises on failure (caller decides how to report).
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    previous_download = _install_local_crop_loader(job_dir)
    try:
        _stage(job_id, "parsing", 5)

        # Persist the input PDFs at the canonical keys (parity with hosted layout)
        _write_bytes(job_dir, config.s3_artifact_key(job_id, "original"), original_pdf)
        _write_bytes(job_dir, config.s3_artifact_key(job_id, "revised"), revised_pdf)

        _stage(job_id, "parsing", 25)

        _stage(job_id, "segmenting (original)", 30)
        orig_data = _process_document(original_pdf, job_id, "original", job_dir)
        _stage(job_id, "segmenting (original)", 50)

        _stage(job_id, "extracting (revised)", 55)
        rev_data = _process_document(revised_pdf, job_id, "revised", job_dir)
        _stage(job_id, "extracting (revised)", 75)

        _stage(job_id, "storing extraction", 80)

        metadata_key = f"metadata/{job_id}/extraction.json"
        extraction_payload = {
            "job_id":      job_id,
            "part_number": part_number,
            "notes":       notes,
            "original":    orig_data,
            "revised":     rev_data,
        }
        _write_json(job_dir, metadata_key, extraction_payload)

        log.info(
            "[%s] Extraction complete — orig views=%d, rev views=%d",
            job_id, len(orig_data["views"]), len(rev_data["views"]),
        )

        _stage(job_id, "matching", 90)
        matches = match_views(orig_data["views"], rev_data["views"])

        orig_by_index = {v["index"]: v for v in orig_data["views"]}
        rev_by_index  = {v["index"]: v for v in rev_data["views"]}

        def _match_dict(m: ViewMatch) -> dict:
            return {
                "orig_index":      m.orig_index,
                "rev_index":       m.rev_index,
                "label":           m.label,
                "score":           m.score,
                "match_type":      m.match_type,
                "orig_crop_s3_key": (
                    orig_by_index[m.orig_index]["crop_s3_key"]
                    if m.orig_index is not None else None
                ),
                "rev_crop_s3_key": (
                    rev_by_index[m.rev_index]["crop_s3_key"]
                    if m.rev_index is not None else None
                ),
            }

        match_manifest = [_match_dict(m) for m in matches]
        _write_json(job_dir, f"metadata/{job_id}/matches.json", {
            "job_id":  job_id,
            "matches": match_manifest,
        })

        log.info("[%s] Matching complete — %d match(es)", job_id, len(matches))

        _stage(job_id, "comparing (LLM)", 92)
        view_diffs = compare_views(job_id, match_manifest, extraction_payload)

        view_diffs_dicts = [viewdiff_to_dict(vd) for vd in view_diffs]
        for vd_dict, m in zip(view_diffs_dicts, match_manifest):
            vd_dict["orig_index"] = m.get("orig_index")
            vd_dict["rev_index"]  = m.get("rev_index")
        view_diffs_dicts = aggregate(view_diffs_dicts)

        changeset_dict = {"job_id": job_id, "view_diffs": view_diffs_dicts}
        _write_json(job_dir, f"metadata/{job_id}/changeset.json", changeset_dict)

        log.info("[%s] Comparison complete — %d view diff(s)", job_id, len(view_diffs_dicts))

        _stage(job_id, "annotating + report", 95)

        # Side-by-side per non-text-only view
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

            view_images[i] = build_side_by_side(orig_png, rev_png, vd["label"])

        # Highlighted full-page renders
        orig_keys = orig_data["page_s3_keys"]
        rev_keys  = rev_data["page_s3_keys"]
        if len(orig_keys) != len(rev_keys):
            log.warning(
                "[%s] Page count mismatch: original=%d revised=%d",
                job_id, len(orig_keys), len(rev_keys),
            )

        orig_views_by_page: dict[int, list] = {}
        for v in orig_data["views"]:
            orig_views_by_page.setdefault(v["page_index"], []).append(v)
        rev_views_by_page: dict[int, list] = {}
        for v in rev_data["views"]:
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
