"""SOLIDWORKS-source orchestrator (parallel to `pipeline.local_runner`).

Replaces the PDF parse → segment → vision-compare flow with a single HTTP
call to the SWCompare Windows service, which reads .SLDPRT/.SLDASM/.SLDDRW
files via the COM API and returns ground-truth geometry deltas.

For .SLDDRW inputs, runs a hybrid: geometry deltas from the model + the
existing PDF vision pipeline on the drawing sheets the service exports as
PDF. The two changesets merge into one — the existing aggregator naturally
keeps the higher-confidence (=geometry, conf 1.0) entry when both paths
flag the same change.

This module never modifies any file under `pipeline/` or `report/` other
than its own — the legacy PDF path is preserved.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from typing import Any

from config import config
from pipeline.aggregator import aggregate
from pipeline.solidworks_client import (
    SolidWorksError,
    SolidWorksUnavailable,
    fetch_solidworks_changeset,
)
from pipeline.solidworks_severity import assign_severity

log = logging.getLogger(__name__)


_DRAWING_SUFFIXES = {".slddrw"}
_PART_SUFFIXES = {".sldprt", ".sldasm"}


def _is_drawing(path: Path) -> bool:
    return path.suffix.lower() in _DRAWING_SUFFIXES


def _normalize_change(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a service-emitted change into a Change-shaped dict.

    The service may emit severity/confidence/rationale already, but
    `config.solidworks_reclassify` lets us recompute them in Python.
    """
    sev = raw.get("severity")
    conf = raw.get("confidence")
    why = raw.get("rationale")

    if sev is None or conf is None or config.solidworks_reclassify:
        sev, conf, why = assign_severity(raw)

    # Field key disambiguates dim vs tol on the same parameter so the
    # aggregator's intra-view dedup (keyed on `field`) doesn't collapse them.
    field_key = raw.get("parameter") or raw.get("type") or "geometry"
    if raw.get("type") == "tolerance" and raw.get("parameter"):
        field_key = f"{raw.get('parameter')}::tol"

    return {
        "field":         field_key,
        "orig_value":    _stringify(raw.get("old_value")),
        "revised_value": _stringify(raw.get("new_value")),
        "severity":      sev,
        "confidence":    float(conf),
        "rationale":     why or "",
        "orig_bbox":     None,
        "rev_bbox":      None,
        # Pass-through SW-specific metadata (kept under a namespaced key so
        # downstream code that iterates `.keys()` can ignore it.)
        "solidworks": {
            "type":              raw.get("type"),
            "feature_name":      raw.get("feature_name"),
            "parameter":         raw.get("parameter"),
            "units":             raw.get("units"),
            "affected_geometry": raw.get("affected_geometry") or [],
            "drawing_views":     raw.get("drawing_views") or [],
        },
    }


def _stringify(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _build_geometry_view_diff(sw_response: dict[str, Any]) -> dict[str, Any]:
    """Wrap the flat SW change list as a single synthetic ViewDiff dict.

    Mirrors the existing synthetic title_block / revision_block ViewDiffs in
    `pipeline.comparator` — `match_type: "geometry_block"` is the new
    text-only block type, rendered the same way as title_block in the report
    (no view image, just a change table).
    """
    return {
        "label":      "Model Geometry (SOLIDWORKS)",
        "match_type": "geometry_block",
        "changes":    [_normalize_change(c) for c in (sw_response.get("changes") or [])],
        "orig_index": None,
        "rev_index":  None,
    }


def _write_bytes(job_dir: Path, key: str, data: bytes) -> Path:
    path = job_dir / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _write_json(job_dir: Path, key: str, data: Any) -> Path:
    return _write_bytes(job_dir, key, json.dumps(data, default=str, indent=2).encode())


def _stage(job_id: str, name: str, pct: int) -> None:
    log.info("[%s] %s %d%%", job_id, name, pct)


def _run_hybrid_drawing(
    job_id: str,
    job_dir: Path,
    sw_response: dict[str, Any],
    geometry_view_diff: dict[str, Any],
    part_number: str,
    notes: str,
) -> dict[str, Any]:
    """For .SLDDRW: combine geometry deltas with PDF vision pipeline output.

    Service is expected to have populated `exported_pdfs.{old_b64,new_b64}`
    for drawings when export_drawing_pdf=true.
    """
    # Imported lazily so the part/assembly path doesn't pay the cost of
    # importing the full vision pipeline (anthropic, opencv, weasyprint, …).
    from pipeline.local_runner import run_comparison_local

    exported = sw_response.get("exported_pdfs") or {}
    old_b64 = exported.get("old_b64")
    new_b64 = exported.get("new_b64")

    if not (old_b64 and new_b64):
        log.warning("[%s] Drawing source but no exported PDFs returned — falling back to geometry-only", job_id)
        return _finalize_geometry_only(
            job_id, job_dir, sw_response, geometry_view_diff, part_number, notes,
        )

    old_pdf = base64.b64decode(old_b64)
    new_pdf = base64.b64decode(new_b64)

    # Run vision pipeline in a child job_dir to keep its artifacts separate.
    vision_dir = job_dir / "vision_subjob"
    vision_dir.mkdir(parents=True, exist_ok=True)
    vision_result = run_comparison_local(
        job_id=f"{job_id}-vision",
        original_pdf=old_pdf,
        revised_pdf=new_pdf,
        part_number=part_number,
        notes=notes + " (vision pass on exported drawing sheets)" if notes else "(vision pass on exported drawing sheets)",
        job_dir=vision_dir,
    )

    # Pull the vision changeset back in
    vision_changeset = json.loads(Path(vision_result["changeset_path"]).read_text())
    vision_view_diffs = vision_changeset.get("view_diffs", [])

    # Geometry first (so dedup keeps the conf=1.0 entries), then vision.
    merged = [geometry_view_diff] + vision_view_diffs
    merged = aggregate(merged)

    return _finalize(
        job_id=job_id,
        job_dir=job_dir,
        view_diffs=merged,
        sw_response=sw_response,
        part_number=part_number,
        notes=notes,
        vision_summary_path=Path(vision_result["report_path"]),
    )


def _finalize_geometry_only(
    job_id: str,
    job_dir: Path,
    sw_response: dict[str, Any],
    geometry_view_diff: dict[str, Any],
    part_number: str,
    notes: str,
) -> dict[str, Any]:
    view_diffs = aggregate([geometry_view_diff])
    return _finalize(
        job_id=job_id,
        job_dir=job_dir,
        view_diffs=view_diffs,
        sw_response=sw_response,
        part_number=part_number,
        notes=notes,
        vision_summary_path=None,
    )


def _finalize(
    job_id: str,
    job_dir: Path,
    view_diffs: list[dict[str, Any]],
    sw_response: dict[str, Any],
    part_number: str,
    notes: str,
    vision_summary_path: Path | None,
) -> dict[str, Any]:
    """Write changeset.json and a text-only report PDF; return summary dict."""
    changeset_dict = {
        "job_id":    job_id,
        "source":    "solidworks",
        "old_file":  sw_response.get("old_file"),
        "new_file":  sw_response.get("new_file"),
        "file_kind": sw_response.get("file_kind"),
        "warnings":  sw_response.get("warnings") or [],
        "view_diffs": view_diffs,
    }

    # Mirror the legacy artifact layout
    _write_json(job_dir, f"metadata/{job_id}/changeset.json", changeset_dict)
    changeset_path = _write_json(job_dir, "changeset.json", changeset_dict)

    # Text-only PDF report. The existing template already renders a
    # geometry_block / title_block-style section as text-only when no
    # `view_images` is supplied for that index — see report/generator.py
    # `_TEXT_ONLY_MATCH_TYPES` and the template's image_b64 conditional.
    extraction_payload = {
        "job_id":      job_id,
        "part_number": part_number,
        "notes":       notes + (
            f"\nSOLIDWORKS source: old={sw_response.get('old_file')} "
            f"new={sw_response.get('new_file')} kind={sw_response.get('file_kind')}"
        ),
        "original":    {"title_block": {}, "revision_block": {"entries": []}},
        "revised":     {"title_block": {}, "revision_block": {"entries": []}},
    }

    # Lazy import: weasyprint pulls in cairo/pango; only load when actually rendering.
    from report.generator import count_summary, generate_report

    pdf_bytes = generate_report(
        job_id=job_id,
        changeset=changeset_dict,
        extraction=extraction_payload,
        part_number=part_number,
        notes=extraction_payload["notes"],
        view_images=None,
        highlighted_pages=None,
    )
    report_path = _write_bytes(job_dir, "report.pdf", pdf_bytes)

    summary = count_summary(changeset_dict["view_diffs"])
    log.info(
        "[%s] SOLIDWORKS report — critical=%d significant=%d minor=%d uncertain=%d",
        job_id, summary.critical, summary.significant, summary.minor, summary.uncertain,
    )

    return {
        "job_dir":         job_dir,
        "report_path":     report_path,
        "changeset_path":  changeset_path,
        "highlighted_pages": {"original": [], "revised": []},
        "summary":         summary,
        "vision_subreport": vision_summary_path,
    }


def run_comparison_solidworks(
    job_id: str,
    original_path: Path,
    revised_path: Path,
    part_number: str,
    notes: str,
    job_dir: Path,
) -> dict[str, Any]:
    """Run a SOLIDWORKS-sourced comparison.

    Args:
        original_path: Path on the SOLIDWORKS service host to Rev A.
        revised_path:  Path on the SOLIDWORKS service host to Rev B.
        job_dir:       Local directory for artifacts (matches local_runner contract).

    Note: the *paths* are forwarded as-is to the Windows service. This
    presumes the service can read the files at those paths (e.g. a UNC share
    or a path that means the same thing on both hosts). For a same-host
    deployment the user just passes a normal Windows path.
    """
    job_dir = Path(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)

    started = time.time()
    _stage(job_id, "calling SOLIDWORKS service", 10)

    try:
        sw_response = fetch_solidworks_changeset(
            old_path=str(original_path),
            new_path=str(revised_path),
        )
    except SolidWorksUnavailable as exc:
        log.error("[%s] SOLIDWORKS service unavailable: %s", job_id, exc)
        raise
    except SolidWorksError as exc:
        log.error("[%s] SOLIDWORKS service error: %s", job_id, exc)
        raise

    _stage(job_id, "received SW response", 60)
    _write_json(job_dir, f"metadata/{job_id}/solidworks_raw.json", sw_response)

    file_kind = sw_response.get("file_kind", "part")
    geometry_view_diff = _build_geometry_view_diff(sw_response)

    _stage(job_id, "merging + report", 80)
    if file_kind == "drawing" and config.solidworks_export_drawings:
        result = _run_hybrid_drawing(
            job_id, job_dir, sw_response, geometry_view_diff, part_number, notes,
        )
    else:
        result = _finalize_geometry_only(
            job_id, job_dir, sw_response, geometry_view_diff, part_number, notes,
        )

    _stage(job_id, "complete", 100)
    log.info("[%s] SW comparison complete in %.1fs", job_id, time.time() - started)
    return result
