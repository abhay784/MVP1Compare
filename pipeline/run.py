"""Pipeline orchestrator — called by RQ worker for each comparison job.

Coordinates Stages 1–4 (parse → segment → extract) for both uploaded PDFs,
stores intermediate JSON artifacts to S3, and updates Redis job progress.
Steps 5–9 (match, compare, annotate, report) are implemented in later steps.
"""
from __future__ import annotations

import io
import json
import logging
import traceback
from dataclasses import asdict
from typing import Any

import boto3
import numpy as np
from PIL import Image

from api.jobs import complete_job, fail_job, update_job_stage
from config import config
from pipeline.aggregator import aggregate
from pipeline.annotator import build_side_by_side
from pipeline.comparator import compare_views, viewdiff_to_dict
from pipeline.highlighter import highlight_page
from pipeline.extractor import extract_dimensions, extract_revision_block, extract_title_block
from pipeline.matcher import ViewMatch, match_views
from pipeline.parser import ParsedPage, parse_pdf
from pipeline.segmentor import ViewCrop, segment_views
from report.generator import count_summary, generate_report

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
        region_name=config.aws_region,
    )


def _download(key: str) -> bytes:
    s3 = _s3_client()
    obj = s3.get_object(Bucket=config.s3_bucket, Key=key)
    return obj["Body"].read()


def _upload_json(key: str, data: Any) -> None:
    s3 = _s3_client()
    body = json.dumps(data, default=str).encode()
    s3.put_object(
        Bucket=config.s3_bucket,
        Key=key,
        Body=body,
        ContentType="application/json",
        ServerSideEncryption="AES256",
    )


def _upload_bytes(key: str, body: bytes, content_type: str) -> None:
    s3 = _s3_client()
    s3.put_object(
        Bucket=config.s3_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )


def _upload_image(key: str, image: np.ndarray) -> None:
    """Upload an RGB numpy array as PNG."""
    s3 = _s3_client()
    pil_img = Image.fromarray(image)
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    s3.put_object(
        Bucket=config.s3_bucket,
        Key=key,
        Body=buf.getvalue(),
        ContentType="image/png",
        ServerSideEncryption="AES256",
    )


# ---------------------------------------------------------------------------
# Per-document processing helper
# ---------------------------------------------------------------------------

def _process_document(pdf_bytes: bytes, job_id: str, kind: str) -> dict:
    """Run Stages 1–4 on one PDF and return a JSON-serialisable summary dict.

    Args:
        pdf_bytes: Raw PDF content.
        job_id:    Job identifier (used for S3 keys).
        kind:      "original" or "revised" (used in artifact key prefixes).

    Returns:
        Dict with keys: views (list of view metadata), title_block, revision_block.
    """
    import fitz as _fitz

    doc = _fitz.open(stream=pdf_bytes, filetype="pdf")
    num_pages = len(doc)
    doc.close()

    log.info("[%s] Parsing %s PDF (%d bytes, %d page(s))", job_id, kind, len(pdf_bytes), num_pages)

    all_view_crops: list[ViewCrop] = []
    all_dims_by_page: dict[int, list] = {}
    page_s3_keys: list[str] = []
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
        _upload_image(page_key, page.image)
        page_s3_keys.append(page_key)

        log.info("[%s] Segmenting %s page %d (raster=%s)", job_id, kind, page_index, page.is_raster)
        view_crops = segment_views(page, pdf_bytes, page_index=page_index)
        dims = extract_dimensions(page.text_blocks)

        all_view_crops.extend(view_crops)
        all_dims_by_page[page_index] = dims

    # Stage 4: title/revision blocks come from the first page only
    title_block = extract_title_block(pdf_bytes)
    rev_block = extract_revision_block(pdf_bytes)

    log.info(
        "[%s] %s → %d view(s) across %d page(s)",
        job_id, kind, len(all_view_crops), num_pages,
    )

    # Store each view crop image to S3 for later LLM comparison
    view_metadata: list[dict] = []
    for i, vc in enumerate(all_view_crops):
        crop_key = f"crops/{job_id}/{kind}/view_{i:02d}.png"
        _upload_image(crop_key, vc.image)

        # Match dimensions that fall inside this view's bbox on its own page
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
            "crop_s3_key":   crop_key,
            "dimensions":    [{"value": d.value, "numeric": d.numeric,
                               "bbox": [d.bbox_x0, d.bbox_y0, d.bbox_x1, d.bbox_y1]}
                              for d in view_dims],
        })

    return {
        "views":          view_metadata,
        "is_raster":      any_raster,
        "page_width_pt":  page_width_pt,
        "page_height_pt": page_height_pt,
        "page_s3_keys":   page_s3_keys,
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
# RQ entry point
# ---------------------------------------------------------------------------

def run_comparison(job_id: str, part_number: str, notes: str) -> None:
    """Top-level function enqueued by POST /compare and executed by the RQ worker.

    Stages:
      parsing    (25%) — download + render both PDFs
      segmenting (50%) — detect & crop views from both sheets
      extracting (75%) — extract dimensions, title block, revision block
      storing    (90%) — write intermediate JSON artifacts to S3
    Steps 5–9 will be added in subsequent pipeline steps.
    """
    try:
        # --- parsing (0 → 25%) ---
        update_job_stage(job_id, "parsing", 5)
        orig_key = config.s3_artifact_key(job_id, "original")
        rev_key = config.s3_artifact_key(job_id, "revised")

        log.info("[%s] Downloading original from s3://%s/%s", job_id, config.s3_bucket, orig_key)
        orig_bytes = _download(orig_key)

        log.info("[%s] Downloading revised from s3://%s/%s", job_id, config.s3_bucket, rev_key)
        rev_bytes = _download(rev_key)

        update_job_stage(job_id, "parsing", 25)

        # --- segmenting + extracting orignal (25 → 50%) ---
        update_job_stage(job_id, "segmenting", 30)
        orig_data = _process_document(orig_bytes, job_id, "original")
        update_job_stage(job_id, "segmenting", 50)

        # --- segmenting + extracting revised (50 → 75%) ---
        update_job_stage(job_id, "extracting", 55)
        rev_data = _process_document(rev_bytes, job_id, "revised")
        update_job_stage(job_id, "extracting", 75)

        # --- store intermediate artifacts (75 → 90%) ---
        update_job_stage(job_id, "storing", 80)

        metadata_key = f"metadata/{job_id}/extraction.json"
        _upload_json(metadata_key, {
            "job_id":      job_id,
            "part_number": part_number,
            "notes":       notes,
            "original":    orig_data,
            "revised":     rev_data,
        })

        log.info(
            "[%s] Extraction complete — orig views=%d, rev views=%d; metadata at %s",
            job_id,
            len(orig_data["views"]),
            len(rev_data["views"]),
            metadata_key,
        )

        update_job_stage(job_id, "awaiting_match", 90)

        # --- match views (90 → 92%) ---
        matches = match_views(orig_data["views"], rev_data["views"])

        # Build match manifest with crop S3 keys resolved for Step 5 (comparator).
        orig_by_index = {v["index"]: v for v in orig_data["views"]}
        rev_by_index  = {v["index"]: v for v in rev_data["views"]}

        def _match_dict(m: ViewMatch) -> dict:
            d = {
                "orig_index":    m.orig_index,
                "rev_index":     m.rev_index,
                "label":         m.label,
                "score":         m.score,
                "match_type":    m.match_type,
                "orig_crop_s3_key": (
                    orig_by_index[m.orig_index]["crop_s3_key"]
                    if m.orig_index is not None else None
                ),
                "rev_crop_s3_key": (
                    rev_by_index[m.rev_index]["crop_s3_key"]
                    if m.rev_index is not None else None
                ),
            }
            return d

        matches_key = f"metadata/{job_id}/matches.json"
        _upload_json(matches_key, {
            "job_id":  job_id,
            "matches": [_match_dict(m) for m in matches],
        })

        log.info(
            "[%s] Matching complete — %d match(es) written to %s",
            job_id, len(matches), matches_key,
        )

        update_job_stage(job_id, "awaiting_compare", 92)

        # --- compare views (92 → 95%) ---
        match_manifest = [_match_dict(m) for m in matches]
        extraction_payload = {
            "job_id":      job_id,
            "part_number": part_number,
            "notes":       notes,
            "original":    orig_data,
            "revised":     rev_data,
        }

        view_diffs = compare_views(job_id, match_manifest, extraction_payload)

        view_diffs_dicts = [viewdiff_to_dict(vd) for vd in view_diffs]
        # Restore orig_index / rev_index lost when ViewDiff was serialized —
        # the annotator needs them to locate S3 crop keys.
        for vd_dict, m in zip(view_diffs_dicts, match_manifest):
            vd_dict["orig_index"] = m.get("orig_index")
            vd_dict["rev_index"] = m.get("rev_index")
        view_diffs_dicts = aggregate(view_diffs_dicts)

        changeset_key = f"metadata/{job_id}/changeset.json"
        _upload_json(changeset_key, {
            "job_id":     job_id,
            "view_diffs": view_diffs_dicts,
        })

        log.info(
            "[%s] Comparison complete — %d view diff(s) written to %s",
            job_id, len(view_diffs_dicts), changeset_key,
        )

        update_job_stage(job_id, "awaiting_report", 95)

        # --- annotate + report (95 → 100%) ---
        changeset_dict = {
            "job_id":     job_id,
            "view_diffs": view_diffs_dicts,
        }

        # Build annotated side-by-side PNGs per non-text-only view.
        view_images: dict[int, bytes] = {}
        for i, vd in enumerate(changeset_dict["view_diffs"]):
            mt = vd["match_type"]
            if mt in ("title_block", "revision_block"):
                continue

            orig_key = f"crops/{job_id}/original/view_{vd.get('orig_index', i):02d}.png" \
                if vd.get("orig_index") is not None else None
            rev_key = f"crops/{job_id}/revised/view_{vd.get('rev_index', i):02d}.png" \
                if vd.get("rev_index") is not None else None

            orig_png = _download(orig_key) if orig_key else None
            rev_png = _download(rev_key) if rev_key else None

            view_images[i] = build_side_by_side(orig_png, rev_png, vd["label"])

        # Build per-page highlighted renders — one highlight pass per page per side.
        orig_keys = orig_data["page_s3_keys"]
        rev_keys  = rev_data["page_s3_keys"]

        if len(orig_keys) != len(rev_keys):
            log.warning(
                "[%s] Page count mismatch: original=%d revised=%d — "
                "pairing up to min(%d, %d); orphan pages noted in report.",
                job_id, len(orig_keys), len(rev_keys), len(orig_keys), len(rev_keys),
            )

        # Index views by page so highlight_page sees only the relevant subset.
        orig_views_by_page: dict[int, list] = {}
        for v in orig_data["views"]:
            orig_views_by_page.setdefault(v["page_index"], []).append(v)

        rev_views_by_page: dict[int, list] = {}
        for v in rev_data["views"]:
            rev_views_by_page.setdefault(v["page_index"], []).append(v)

        orig_highlighted_pages: list[bytes] = []
        for pi, key in enumerate(orig_keys):
            page_png = _download(key)
            orig_highlighted_pages.append(
                highlight_page(page_png, view_diffs_dicts, orig_views_by_page.get(pi, []), "original")
            )

        rev_highlighted_pages: list[bytes] = []
        for pi, key in enumerate(rev_keys):
            page_png = _download(key)
            rev_highlighted_pages.append(
                highlight_page(page_png, view_diffs_dicts, rev_views_by_page.get(pi, []), "revised")
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

        report_key = config.s3_artifact_key(job_id, "report")
        _upload_bytes(report_key, pdf_bytes, "application/pdf")

        # Canonical changeset location (what GET /compare/{job_id}'s presigned URL points at).
        canonical_changeset_key = config.s3_artifact_key(job_id, "changeset")
        _upload_json(canonical_changeset_key, changeset_dict)

        summary = count_summary(changeset_dict["view_diffs"])
        log.info(
            "[%s] Report uploaded to %s — critical=%d significant=%d minor=%d uncertain=%d views=%d",
            job_id, report_key,
            summary.critical, summary.significant, summary.minor,
            summary.uncertain, summary.total_views_compared,
        )

        complete_job(job_id, summary)

    except Exception as exc:
        log.exception("[%s] Pipeline failed: %s", job_id, exc)
        fail_job(job_id, f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        raise
