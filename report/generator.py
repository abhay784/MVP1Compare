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


# ---------------------------------------------------------------------------
# Blueprint rendering — fills the customer-supplied template `html_shell` by
# substituting [[TOKEN]] placeholders with comparison-data fragments.
# ---------------------------------------------------------------------------

import html as _html


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _fmt_zone(zone: Any) -> str:
    """Render a zone string for display. Empty → '—'."""
    z = (zone or "").strip() if isinstance(zone, str) else ""
    return z or "—"


def _render_summary_fragment(summary: dict, severity_labels: dict[str, str]) -> str:
    cells = [
        ("CRITICAL",   "sev-CRITICAL",   summary["critical"]),
        ("MAJOR",      "sev-SIGNIFICANT", summary["significant"]),
        ("MINOR",      "sev-MINOR",      summary["minor"]),
        ("UNCERTAIN",  "sev-UNCERTAIN",  summary["uncertain"]),
    ]
    parts = ['<div class="drawdiff-summary" style="display:table;width:100%;margin:6pt 0 12pt 0;">']
    for sev, klass, n in cells:
        label = severity_labels.get(sev, _SEVERITY_DISPLAY.get(sev, sev).title())
        parts.append(
            f'<div class="cell {klass}" style="display:table-cell;padding:6pt;'
            f'text-align:center;border:1px solid #ccc;">'
            f'<span style="font-size:22pt;font-weight:bold;display:block;">{n}</span>'
            f'{_esc(label)}</div>'
        )
    parts.append(
        f'<div class="cell" style="display:table-cell;padding:6pt;text-align:center;'
        f'border:1px solid #ccc;">'
        f'<span style="font-size:22pt;font-weight:bold;display:block;">'
        f'{summary["total_views_compared"]}</span>Views compared</div>'
    )
    parts.append("</div>")
    return "".join(parts)


def _render_title_block_fragment(orig: dict, rev: dict) -> str:
    fields = ["part_number", "revision", "material", "tolerance", "drawn_by", "date"]
    rows = []
    for f in fields:
        ov = orig.get(f, "") or ""
        rv = rev.get(f, "") or ""
        diff = ' class="diff"' if ov != rv else ""
        rows.append(
            f'<tr{diff}><td><strong>{_esc(f)}</strong></td>'
            f'<td>{_esc(ov)}</td><td>{_esc(rv)}</td></tr>'
        )
    return (
        '<table class="drawdiff-title-block">'
        '<thead><tr><th>Field</th><th>Original</th><th>Revised</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _render_revision_block_fragment(orig_entries: list, rev_entries: list) -> str:
    added = [e for e in rev_entries if e not in orig_entries]
    removed = [e for e in orig_entries if e not in rev_entries]
    parts = ['<div class="drawdiff-revision-block">']
    parts.append("<h4>Added in revised</h4>")
    if added:
        parts.append("<ul>" + "".join(f"<li>{_esc(e)}</li>" for e in added) + "</ul>")
    else:
        parts.append('<p><em>No revision entries added.</em></p>')
    parts.append("<h4>Removed from original</h4>")
    if removed:
        parts.append("<ul>" + "".join(f"<li>{_esc(e)}</li>" for e in removed) + "</ul>")
    else:
        parts.append('<p><em>No revision entries removed.</em></p>')
    parts.append("</div>")
    return "".join(parts)


def _render_drawings_fragment(page_pairs: list[dict],
                              orphan_orig: list[dict],
                              orphan_rev: list[dict]) -> str:
    if not (page_pairs or orphan_orig or orphan_rev):
        return '<p><em>No annotated drawings available.</em></p>'
    parts = ['<div class="drawdiff-drawings">']
    for pair in page_pairs:
        parts.append(f'<p><strong>Sheet {pair["page_num"]}</strong></p>')
        parts.append('<div style="display:table;width:100%;margin-bottom:14pt;">')
        for side, b64 in (("Original", pair["orig_b64"]), ("Revised", pair["rev_b64"])):
            parts.append('<div style="display:table-cell;width:50%;padding:0 6pt;vertical-align:top;">')
            parts.append(f'<div style="font-size:9.5pt;color:#555;">{side}</div>')
            if b64:
                parts.append(
                    f'<img style="width:100%;max-width:100%;border:1px solid #ccc;" '
                    f'src="{b64}" alt="{side} sheet {pair["page_num"]}">'
                )
            else:
                parts.append('<p><em>Not available.</em></p>')
            parts.append('</div>')
        parts.append('</div>')
    for orphan_list, label in ((orphan_orig, "original"), (orphan_rev, "revised")):
        if not orphan_list:
            continue
        parts.append(f'<p><strong>Sheets present in {label} only</strong></p>')
        for p in orphan_list:
            parts.append(
                f'<img style="width:100%;border:1px solid #ccc;margin:4pt 0;" '
                f'src="{p["b64"]}" alt="{label} sheet {p["page_num"]}">'
            )
    parts.append("</div>")
    return "".join(parts)


def _render_changes_fragment(
    view_sections: list[dict],
    columns: list[dict],
    has_zone_column: bool,
    severity_labels: dict[str, str],
) -> str:
    """Render the per-view changes tables using the blueprint's column spec.

    Zone-handling rule (mandatory): if the template has no dedicated zone column,
    every change description must mention its zone. We prepend "[Zone X] " to
    the description cell when has_zone_column is False.
    """
    if not view_sections:
        return '<p><em>No per-view differences detected.</em></p>'

    # Find which column index, if any, carries the description role (for zone-injection).
    desc_index = next(
        (i for i, c in enumerate(columns) if c["role"] == "description"),
        None,
    )

    parts: list[str] = []
    for view in view_sections:
        parts.append('<div class="drawdiff-view-section">')
        parts.append(
            f'<h3>{_esc(view["label"])} '
            f'<span style="color:#666;font-size:9pt;text-transform:uppercase;">'
            f'[{_esc(view["match_type"])}]</span></h3>'
        )
        if view["image_b64"]:
            parts.append(
                f'<img style="width:100%;margin:6pt 0;" src="{view["image_b64"]}" '
                f'alt="{_esc(view["label"])}">'
            )

        if not view["changes"]:
            parts.append('<p><em>No textual changes recorded for this view.</em></p>')
            parts.append('</div>')
            continue

        parts.append('<table class="drawdiff-changes" style="width:100%;border-collapse:collapse;">')
        parts.append('<thead><tr>')
        for col in columns:
            parts.append(f'<th>{_esc(col["header"])}</th>')
        parts.append('</tr></thead><tbody>')

        for c in view["changes"]:
            sev_base = str(c.get("severity", "")).rstrip("*")
            sev_label = severity_labels.get(sev_base, _SEVERITY_DISPLAY.get(sev_base, sev_base))
            sev_class = "sev-" + _SEVERITY_DISPLAY.get(sev_base, "MINOR").upper()
            uncertain_star = "*" if str(c.get("severity", "")).endswith("*") else ""
            zone_text = _fmt_zone(c.get("zone"))
            description_html = _esc(c.get("rationale", ""))

            # Mandatory zone display: if there's no zone column, prepend it to
            # the description cell. We only do this when a real zone is known.
            inject_zone = (
                not has_zone_column
                and zone_text != "—"
                and desc_index is not None
            )
            if inject_zone:
                description_html = (
                    f'<strong>[Zone {_esc(zone_text)}]</strong> ' + description_html
                )

            # Footnotes carried as side-channel by the aggregator.
            for note_field in ("uncertain_note", "dedup_note"):
                note_val = c.get(note_field)
                if note_val:
                    description_html += (
                        f'<div style="font-size:8.5pt;color:#777;font-style:italic;">'
                        f'({_esc(note_val)})</div>'
                    )

            parts.append(f'<tr class="{sev_class}">')
            for i, col in enumerate(columns):
                role = col["role"]
                if role == "severity":
                    cell = f"<strong>{_esc(sev_label)}{uncertain_star}</strong>"
                elif role == "field":
                    cell = _esc(c.get("field", ""))
                elif role == "orig_value":
                    cell = _esc(c.get("orig_value") if c.get("orig_value") is not None else "—")
                elif role == "revised_value":
                    cell = _esc(c.get("revised_value") if c.get("revised_value") is not None else "—")
                elif role == "zone":
                    cell = (
                        f'<span style="font-family:monospace;font-weight:bold;">'
                        f'{_esc(zone_text)}</span>'
                    )
                elif role == "confidence":
                    cell = f'{float(c.get("confidence", 0.0)):.2f}'
                elif role == "description":
                    cell = description_html
                else:
                    cell = ""
                parts.append(f"<td>{cell}</td>")
            parts.append("</tr>")
        parts.append("</tbody></table></div>")
    return "".join(parts)


def _render_blueprint_report(
    *,
    job_id: str,
    part_number: str,
    notes: str,
    generated_at: str,
    summary_dict: dict,
    orig_title_block: dict,
    rev_title_block: dict,
    orig_revision_entries: list,
    rev_revision_entries: list,
    view_sections: list[dict],
    page_pairs: list[dict],
    orphan_original_pages: list[dict],
    orphan_revised_pages: list[dict],
    blueprint: dict,
) -> str:
    """Substitute every [[TOKEN]] in blueprint['html_shell'] with a fragment."""
    severity_labels = {
        **{"CRITICAL": "Critical", "MAJOR": "Significant", "MINOR": "Minor", "UNCERTAIN": "Review"},
        **(blueprint.get("severity_labels") or {}),
    }
    change_table = blueprint.get("change_table") or {}
    columns = change_table.get("columns") or [
        {"header": "Class", "role": "severity"},
        {"header": "Item", "role": "field"},
        {"header": "Was", "role": "orig_value"},
        {"header": "Now", "role": "revised_value"},
        {"header": "Description", "role": "description"},
    ]
    has_zone_column = bool(change_table.get("has_zone_column")) or any(
        c["role"] == "zone" for c in columns
    )

    substitutions = {
        "[[TITLE]]":        _esc(part_number) or "Engineering Change Order",
        "[[PART_NUMBER]]":  _esc(part_number),
        "[[JOB_ID]]":       _esc(job_id),
        "[[GENERATED_AT]]": _esc(generated_at),
        "[[NOTES]]":        _esc(notes),
        "[[SUMMARY]]":      _render_summary_fragment(summary_dict, severity_labels),
        "[[TITLE_BLOCK]]":  _render_title_block_fragment(orig_title_block, rev_title_block),
        "[[REVISION_BLOCK]]": _render_revision_block_fragment(
            orig_revision_entries, rev_revision_entries
        ),
        "[[DRAWINGS]]": _render_drawings_fragment(
            page_pairs, orphan_original_pages, orphan_revised_pages
        ),
        "[[CHANGES]]": _render_changes_fragment(
            view_sections, columns, has_zone_column, severity_labels
        ),
    }

    html_out = blueprint["html_shell"]
    for token, fragment in substitutions.items():
        html_out = html_out.replace(token, fragment)
    return html_out


def generate_report(
    job_id: str,
    changeset: dict,
    extraction: dict,
    part_number: str,
    notes: str,
    view_images: Optional[dict[int, bytes]] = None,
    highlighted_pages: Optional[dict[str, bytes]] = None,
    blueprint: Optional[dict] = None,
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

    if blueprint is not None and blueprint.get("html_shell"):
        html_str = _render_blueprint_report(
            job_id=job_id,
            part_number=part_number,
            notes=notes,
            generated_at=ctx["generated_at"],
            summary_dict=ctx["summary"],
            orig_title_block=orig_tb,
            rev_title_block=rev_tb,
            orig_revision_entries=ctx["orig_revision_entries"],
            rev_revision_entries=ctx["rev_revision_entries"],
            view_sections=view_sections,
            page_pairs=page_pairs,
            orphan_original_pages=orphan_original_pages,
            orphan_revised_pages=orphan_revised_pages,
            blueprint=blueprint,
        )
    else:
        env = _env()
        env.filters["sev_display"] = lambda s: _SEVERITY_DISPLAY.get(str(s).rstrip("*"), s)
        template = env.get_template("report.html.j2")
        html_str = template.render(**ctx)

    pdf_bytes: bytes = HTML(string=html_str).write_pdf()
    log.info("[%s] Generated PDF report (%d bytes)", job_id, len(pdf_bytes))
    return pdf_bytes
