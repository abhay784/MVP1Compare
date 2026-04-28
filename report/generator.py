"""PDF report generator — Step 6 of the DrawDiff pipeline.

Takes the comparator changeset + extraction metadata plus a list of pre-built
annotated side-by-side PNGs (base64-encoded) and renders the final PDF report
via a Jinja2 template + WeasyPrint.

Public contract: generate_report() accepts plain dicts (NOT the comparator's
dataclasses) so future callers (aggregator, retry, synthesized reports) can
drive this module without importing pipeline internals.
"""
from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from weasyprint import HTML  # noqa: F401 — imported at module scope so tests can patch

from api.models import ChangeSummary

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Severity mapping — SINGLE source of truth for the MAJOR→significant rename.
# Changing the public label (e.g. renaming "SIGNIFICANT" → "MAJOR" later) is a
# one-line edit here + in the template's {{ sev|display }} filter.
# ---------------------------------------------------------------------------

# Comparator severity → ChangeSummary field name
_SEVERITY_TO_SUMMARY_FIELD = {
    "CRITICAL":  "critical",
    "MAJOR":     "significant",
    "MINOR":     "minor",
    "UNCERTAIN": "uncertain",
}

# Comparator severity → label shown to the user in the PDF
_SEVERITY_DISPLAY = {
    "CRITICAL":  "CRITICAL",
    "MAJOR":     "SIGNIFICANT",
    "MINOR":     "MINOR",
    "UNCERTAIN": "UNCERTAIN",
}

# Severity sort order (CRITICAL changes first, UNCERTAIN last) for per-view tables.
_SEVERITY_ORDER = {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2, "UNCERTAIN": 3}

# View sections to SKIP annotator — rendered as text tables only.
_TEXT_ONLY_MATCH_TYPES = {"title_block", "revision_block"}


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        undefined=StrictUndefined,   # catch template typos at render time
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def count_summary(view_diffs: list[dict]) -> ChangeSummary:
    """Tally severity counts across all view_diffs into a ChangeSummary.

    TODO (learning task — ~8 lines):
    Implement the counter that turns a list of view_diffs (shape defined by
    Step 5's changeset.json) into a ChangeSummary.

    Rules:
      - For each change in each view_diff, increment the ChangeSummary field
        named by _SEVERITY_TO_SUMMARY_FIELD[change["severity"]].
      - Unknown severities should be skipped with a log.warning (defensive —
        the comparator contract guarantees the four values above, but a
        prompt regression could leak a stray string).
      - total_views_compared = number of view_diffs with match_type == "matched".
        (added/removed/title_block/revision_block don't count as "compared".)

    Why this matters: this function is the only place severity counts get
    bound to the public API schema. If _SEVERITY_TO_SUMMARY_FIELD changes,
    this function keeps working — but if you hard-code field names here, a
    future rename has to touch two places.

    Return: a populated ChangeSummary (critical/significant/minor/uncertain/
    total_views_compared).
    """
    counts: dict[str, int] = {f: 0 for f in _SEVERITY_TO_SUMMARY_FIELD.values()}
    total_views_compared = 0
    for vd in view_diffs:
        if vd.get("match_type") == "matched":
            total_views_compared += 1
        for change in vd.get("changes", []):
            field = _SEVERITY_TO_SUMMARY_FIELD.get(change["severity"])
            if field is None:
                log.warning("count_summary: unknown severity %r — skipping", change["severity"])
                continue
            counts[field] += 1
    return ChangeSummary(**counts, total_views_compared=total_views_compared)


def _sorted_changes(changes: list[dict]) -> list[dict]:
    return sorted(changes, key=lambda c: (_SEVERITY_ORDER.get(c["severity"], 99),
                                          -float(c.get("confidence", 0))))


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _encode_image(png_bytes: Optional[bytes]) -> Optional[str]:
    if png_bytes is None:
        return None
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def generate_report(
    job_id: str,
    changeset: dict,
    extraction: dict,
    part_number: str,
    notes: str,
    view_images: Optional[dict[int, bytes]] = None,
    highlighted_pages: Optional[dict[str, bytes]] = None,
) -> bytes:
    """Render the final PDF report.

    Args:
        job_id:       Job UUID (appears in the PDF footer).
        changeset:    dict with key "view_diffs" (list per Step 5 contract).
        extraction:   dict with keys "original"/"revised", each containing
                      title_block and revision_block sub-dicts.
        part_number:  User-supplied part number (header).
        notes:        User-supplied free-text notes (header).
        view_images:  Map from view_diffs index → annotated PNG bytes. Missing
                      entries render as text-only sections.

    Returns: PDF bytes.
    """
    view_images = view_images or {}
    highlighted_pages = highlighted_pages or {}
    summary = count_summary(changeset["view_diffs"])

    view_sections = []
    for i, vd in enumerate(changeset["view_diffs"]):
        image_b64 = None
        if vd["match_type"] not in _TEXT_ONLY_MATCH_TYPES:
            image_b64 = _encode_image(view_images.get(i))

        view_sections.append({
            "label":      vd["label"],
            "match_type": vd["match_type"],
            "image_b64":  image_b64,
            "changes":    _sorted_changes(vd.get("changes", [])),
        })

    orig_tb = extraction.get("original", {}).get("title_block", {}) or {}
    rev_tb  = extraction.get("revised", {}).get("title_block", {}) or {}

    # Build paired page rows (Option Y) up to min(N, M); orphan pages listed separately (Option Q).
    orig_pages: list[Optional[bytes]] = highlighted_pages.get("original") or []
    rev_pages:  list[Optional[bytes]] = highlighted_pages.get("revised")  or []
    n_pairs = min(len(orig_pages), len(rev_pages))

    page_pairs = [
        {
            "page_num":   i + 1,
            "orig_b64":   _encode_image(orig_pages[i]),
            "rev_b64":    _encode_image(rev_pages[i]),
        }
        for i in range(n_pairs)
    ]
    orphan_original_pages = [
        {"page_num": i + 1, "b64": _encode_image(orig_pages[i])}
        for i in range(n_pairs, len(orig_pages))
    ]
    orphan_revised_pages = [
        {"page_num": i + 1, "b64": _encode_image(rev_pages[i])}
        for i in range(n_pairs, len(rev_pages))
    ]

    ctx = {
        "job_id":         job_id,
        "part_number":    part_number,
        "notes":          notes,
        "generated_at":   _iso_utc_now(),
        "summary":        summary.model_dump(),
        "orig_title_block": orig_tb,
        "rev_title_block":  rev_tb,
        "orig_revision_entries": (extraction.get("original", {})
                                  .get("revision_block", {}).get("entries", []) or []),
        "rev_revision_entries":  (extraction.get("revised", {})
                                  .get("revision_block", {}).get("entries", []) or []),
        "view_sections":           view_sections,
        "page_pairs":              page_pairs,
        "orphan_original_pages":   orphan_original_pages,
        "orphan_revised_pages":    orphan_revised_pages,
        "severity_display": _SEVERITY_DISPLAY,
        "legend": [
            ("CRITICAL",  "Affects structural integrity, safety, interchangeability, "
                          "or regulatory compliance."),
            ("SIGNIFICANT", "Affects manufacturing scope, cost, or lead time. No safety "
                            "risk but requires re-planning."),
            ("MINOR",     "Clarity, formatting, or documentation change. No manufacturing impact."),
            ("UNCERTAIN", "Confidence below 0.65. Requires human review."),
        ],
    }

    env = _env()
    env.filters["sev_display"] = lambda s: _SEVERITY_DISPLAY.get(s, s)
    template = env.get_template("report.html.j2")
    html_str = template.render(**ctx)

    pdf_bytes: bytes = HTML(string=html_str).write_pdf()
    log.info("[%s] Generated PDF report (%d bytes)", job_id, len(pdf_bytes))
    return pdf_bytes
