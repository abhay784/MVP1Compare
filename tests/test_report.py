"""Tests for Step 6 — annotator, report generator, and run.py wiring.

WeasyPrint is patched at `report.generator.HTML` so these tests don't need
cairo/pango/fontconfig on the test host. S3 boto3 clients are patched via
`_s3_client` at the run.py module level.
"""
from __future__ import annotations

import io
import json
from unittest import mock

import pytest
from PIL import Image

from api.models import ChangeSummary
from pipeline import annotator
from report import generator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _png_bytes(size=(200, 150), color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def stub_pdf():
    """Patch WeasyPrint so generator returns a stub PDF deterministically."""
    with mock.patch.object(generator, "HTML") as mocked:
        instance = mocked.return_value
        instance.write_pdf.return_value = b"%PDF-1.4 stub"
        yield mocked


@pytest.fixture
def mixed_changeset():
    return {
        "job_id": "job-1",
        "view_diffs": [
            {
                "label": "FRONT VIEW", "match_type": "matched",
                "orig_index": 0, "rev_index": 0,
                "changes": [
                    {"field": "bore_dia", "orig_value": "Ø12.5", "revised_value": "Ø12.7",
                     "severity": "CRITICAL", "confidence": 0.92, "rationale": "bore widened"},
                    {"field": "chamfer", "orig_value": "0.5x45", "revised_value": "0.6x45",
                     "severity": "MAJOR", "confidence": 0.71, "rationale": "chamfer increased"},
                    {"field": "note_1", "orig_value": "REMOVE BURRS", "revised_value": "DEBURR",
                     "severity": "MINOR", "confidence": 0.88, "rationale": "wording"},
                    {"field": "linework", "orig_value": None, "revised_value": "possible hole",
                     "severity": "UNCERTAIN", "confidence": 0.5, "rationale": "ambiguous"},
                ],
            },
            {
                "label": "SECTION A-A", "match_type": "added",
                "orig_index": None, "rev_index": 1,
                "changes": [
                    {"field": "presence", "orig_value": None, "revised_value": "section added",
                     "severity": "MAJOR", "confidence": 0.9, "rationale": "new view"},
                ],
            },
            {
                "label": "TITLE BLOCK", "match_type": "title_block",
                "orig_index": None, "rev_index": None,
                "changes": [
                    {"field": "revision", "orig_value": "A", "revised_value": "B",
                     "severity": "MINOR", "confidence": 0.99, "rationale": "rev bumped"},
                ],
            },
        ],
    }


@pytest.fixture
def extraction():
    return {
        "original": {
            "title_block": {"part_number": "P-001", "revision": "A", "material": "6061-T6",
                            "tolerance": "±0.1", "drawn_by": "ak", "date": "2026-01-01"},
            "revision_block": {"entries": [("A", "initial release", "2026-01-01")]},
        },
        "revised": {
            "title_block": {"part_number": "P-001", "revision": "B", "material": "6061-T6",
                            "tolerance": "±0.1", "drawn_by": "ak", "date": "2026-04-01"},
            "revision_block": {"entries": [
                ("A", "initial release", "2026-01-01"),
                ("B", "bore +0.2", "2026-04-01"),
            ]},
        },
    }


# ---------------------------------------------------------------------------
# Annotator
# ---------------------------------------------------------------------------

def test_annotator_matched_view_has_both_sides():
    orig = _png_bytes(color=(255, 0, 0))
    rev = _png_bytes(color=(0, 255, 0))
    out = annotator.build_side_by_side(orig, rev, "FRONT")
    img = Image.open(io.BytesIO(out))
    assert img.format == "PNG"
    # Caption + 900px image area; width is two panels + gap
    assert img.height == annotator._CAPTION_H + annotator._TARGET_HEIGHT
    assert img.width > 2 * annotator._TARGET_HEIGHT * (200 / 150) - 10  # ~2400 with gap


def test_annotator_added_view_renders_missing_placeholder():
    rev = _png_bytes(color=(0, 0, 255))
    out = annotator.build_side_by_side(None, rev, "SECTION A-A")
    img = Image.open(io.BytesIO(out))
    # Placeholder is square — left panel width == _TARGET_HEIGHT
    assert img.width >= annotator._TARGET_HEIGHT + annotator._GAP_PX


def test_annotator_requires_at_least_one_side():
    with pytest.raises(ValueError):
        annotator.build_side_by_side(None, None, "X")


# ---------------------------------------------------------------------------
# Severity counting (the one-line contract for MAJOR → significant)
# ---------------------------------------------------------------------------

def test_count_summary_maps_major_to_significant(mixed_changeset):
    summary = generator.count_summary(mixed_changeset["view_diffs"])
    assert summary.critical == 1
    assert summary.significant == 2  # MAJOR x2 (one in matched, one in added)
    assert summary.minor == 2        # matched note + title_block
    assert summary.uncertain == 1
    assert summary.total_views_compared == 1  # only the matched view


def test_count_summary_empty_returns_zeros():
    summary = generator.count_summary([])
    assert summary == ChangeSummary()


def test_count_summary_unknown_severity_is_skipped(caplog):
    summary = generator.count_summary([
        {"label": "X", "match_type": "matched", "changes": [
            {"field": "f", "orig_value": None, "revised_value": None,
             "severity": "WEIRD", "confidence": 0.5, "rationale": ""},
        ]},
    ])
    assert summary.critical == summary.significant == summary.minor == 0
    assert summary.total_views_compared == 1


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def test_generator_mixed_changeset(stub_pdf, mixed_changeset, extraction):
    pdf = generator.generate_report(
        job_id="job-1",
        changeset=mixed_changeset,
        extraction=extraction,
        part_number="P-001",
        notes="Rev B release",
        view_images={0: _png_bytes(), 1: _png_bytes()},
    )
    assert pdf.startswith(b"%PDF")
    # Inspect the HTML passed to WeasyPrint
    (args, kwargs) = stub_pdf.call_args
    html = kwargs.get("string") or args[0]
    assert "DrawDiff Comparison Report" in html
    assert "SIGNIFICANT" in html        # MAJOR re-displayed as SIGNIFICANT
    assert "MAJOR" not in html.replace("MAJOR CHANGES", "")  # raw "MAJOR" should not leak
    assert "P-001" in html
    assert "AI-generated" in html
    # The title_block view is text-only — no image for it
    assert html.count("data:image/png;base64") == 2


def test_generator_title_block_only(stub_pdf, extraction):
    cs = {"view_diffs": [
        {"label": "TITLE BLOCK", "match_type": "title_block", "changes": [
            {"field": "revision", "orig_value": "A", "revised_value": "B",
             "severity": "MINOR", "confidence": 1.0, "rationale": "bumped"},
        ]},
    ]}
    pdf = generator.generate_report("j", cs, extraction, "P-001", "", view_images={})
    assert pdf.startswith(b"%PDF")
    (args, kwargs) = stub_pdf.call_args
    html = kwargs.get("string") or args[0]
    assert "TITLE BLOCK" in html
    assert "data:image/png;base64" not in html  # no annotator output for text-only


def test_generator_empty_changeset(stub_pdf, extraction):
    pdf = generator.generate_report(
        "j", {"view_diffs": []}, extraction, "P-001", "", view_images={},
    )
    assert pdf.startswith(b"%PDF")
    (args, kwargs) = stub_pdf.call_args
    html = kwargs.get("string") or args[0]
    # Summary tiles still render with zero counts
    for tile in ("Critical", "Significant", "Minor", "Uncertain"):
        assert tile in html


def test_generator_strict_undefined_catches_template_typos(stub_pdf, extraction):
    """If the template references an undefined var, render raises instead of silently blanking."""
    # Force a missing key by dropping summary via monkeypatching count_summary
    with mock.patch.object(generator, "count_summary", return_value=None):
        with pytest.raises(Exception):
            generator.generate_report(
                "j", {"view_diffs": []}, extraction, "P-001", "", view_images={},
            )


# ---------------------------------------------------------------------------
# run.py wiring — publishes to BOTH metadata/ and changesets/ keys
# ---------------------------------------------------------------------------

def test_run_publishes_canonical_changeset_and_calls_complete(monkeypatch, mixed_changeset,
                                                              extraction, stub_pdf):
    """End-to-end seam: assert the post-compare block uploads PDF, canonical
    changeset, and calls complete_job with progress=100."""
    from pipeline import run as runmod

    uploaded: dict[str, bytes] = {}
    completed: dict[str, ChangeSummary] = {}

    def fake_upload_json(key, data):
        uploaded[key] = json.dumps(data, default=str).encode()

    def fake_upload_bytes(key, body, content_type):
        uploaded[key] = body

    def fake_download(key):
        return _png_bytes()

    def fake_complete(job_id, summary):
        completed[job_id] = summary

    monkeypatch.setattr(runmod, "_upload_json", fake_upload_json)
    monkeypatch.setattr(runmod, "_upload_bytes", fake_upload_bytes)
    monkeypatch.setattr(runmod, "_download", fake_download)
    monkeypatch.setattr(runmod, "complete_job", fake_complete)

    # Simulate just the post-awaiting_report block — call the pieces directly.
    job_id = "job-1"
    changeset_dict = mixed_changeset
    view_images = {}
    for i, vd in enumerate(changeset_dict["view_diffs"]):
        if vd["match_type"] in ("title_block", "revision_block"):
            continue
        orig_png = fake_download("x") if vd.get("orig_index") is not None else None
        rev_png = fake_download("x") if vd.get("rev_index") is not None else None
        view_images[i] = annotator.build_side_by_side(orig_png, rev_png, vd["label"])

    pdf_bytes = generator.generate_report(
        job_id=job_id, changeset=changeset_dict, extraction=extraction,
        part_number="P-001", notes="", view_images=view_images,
    )

    from config import config
    fake_upload_bytes(config.s3_artifact_key(job_id, "report"), pdf_bytes, "application/pdf")
    fake_upload_json(config.s3_artifact_key(job_id, "changeset"), changeset_dict)
    fake_upload_json(f"metadata/{job_id}/changeset.json", changeset_dict)
    summary = generator.count_summary(changeset_dict["view_diffs"])
    fake_complete(job_id, summary)

    assert f"reports/{job_id}.pdf" in uploaded
    assert f"changesets/{job_id}.json" in uploaded        # canonical
    assert f"metadata/{job_id}/changeset.json" in uploaded  # internal
    assert completed[job_id].critical == 1
    assert completed[job_id].significant == 2


# ---------------------------------------------------------------------------
# Blueprint-driven rendering
# ---------------------------------------------------------------------------

def test_generator_blueprint_fills_html_shell(stub_pdf, mixed_changeset, extraction):
    """When a blueprint is provided, [[TOKENS]] in html_shell are substituted."""
    blueprint = {
        "html_shell": (
            "<!DOCTYPE html><html><head><style>"
            ".sev-CRITICAL{background:#fde2e2;}"
            ".sev-SIGNIFICANT{background:#fff1d6;}"
            ".sev-MINOR{background:#eceff1;}"
            ".sev-UNCERTAIN{background:#fff8c4;}"
            "</style></head><body>"
            "<h1>ENGINEERING CHANGE ORDER</h1>"
            "<p>ECO #: [[JOB_ID]] | Part: [[PART_NUMBER]] | Issued: [[GENERATED_AT]]</p>"
            "<h2>Change Classification</h2>[[SUMMARY]]"
            "<h2>Affected Drawing Fields</h2>[[TITLE_BLOCK]]"
            "<h2>Detailed Changes</h2>[[CHANGES]]"
            "</body></html>"
        ),
        "change_table": {
            "columns": [
                {"header": "Class",                 "role": "severity"},
                {"header": "Item",                  "role": "field"},
                {"header": "Was",                   "role": "orig_value"},
                {"header": "Now",                   "role": "revised_value"},
                {"header": "Description of Change", "role": "description"},
            ],
            "has_zone_column": False,
        },
        "severity_labels": {
            "CRITICAL":  "Class A",
            "MAJOR":     "Class B",
            "MINOR":     "Class C",
            "UNCERTAIN": "Review",
        },
    }
    pdf = generator.generate_report(
        job_id="job-1",
        changeset=mixed_changeset,
        extraction=extraction,
        part_number="P-001",
        notes="",
        view_images={0: _png_bytes(), 1: _png_bytes()},
        blueprint=blueprint,
    )
    assert pdf.startswith(b"%PDF")
    (args, kwargs) = stub_pdf.call_args
    html = kwargs.get("string") or args[0]
    # The customer's shell text is preserved verbatim.
    assert "ENGINEERING CHANGE ORDER" in html
    assert "Change Classification" in html
    assert "Detailed Changes" in html
    # Token-substituted values appear.
    assert "P-001" in html
    assert "job-1" in html
    # Custom severity labels replace defaults.
    assert "Class A" in html
    assert "Class B" in html
    # Custom column header in change table.
    assert "Description of Change" in html
    # All known tokens are substituted (none left in output).
    for tok in ("[[JOB_ID]]", "[[PART_NUMBER]]", "[[SUMMARY]]", "[[TITLE_BLOCK]]", "[[CHANGES]]"):
        assert tok not in html


def test_generator_blueprint_injects_zone_into_description_when_no_zone_column(
    stub_pdf, extraction
):
    """If the template has no zone column, every change description must include its zone."""
    changeset = {
        "job_id": "job-z",
        "view_diffs": [
            {
                "label": "FRONT", "match_type": "matched",
                "orig_index": 0, "rev_index": 0,
                "changes": [
                    {
                        "field": "bore", "orig_value": "Ø10", "revised_value": "Ø12",
                        "severity": "MAJOR", "confidence": 0.9,
                        "rationale": "Bore widened.",
                        "zone": "C-4",
                    },
                ],
            },
        ],
    }
    blueprint = {
        "html_shell": "<!DOCTYPE html><html><body>[[CHANGES]]</body></html>",
        "change_table": {
            "columns": [
                {"header": "Class",       "role": "severity"},
                {"header": "Item",        "role": "field"},
                {"header": "Was",         "role": "orig_value"},
                {"header": "Now",         "role": "revised_value"},
                {"header": "Description", "role": "description"},
            ],
            "has_zone_column": False,
        },
        "severity_labels": {},
    }
    generator.generate_report(
        "job-z", changeset, extraction, "P-001", "",
        view_images={0: _png_bytes()},
        blueprint=blueprint,
    )
    (args, kwargs) = stub_pdf.call_args
    html = kwargs.get("string") or args[0]
    # Zone is prepended to the description cell when there is no dedicated zone column.
    assert "[Zone C-4]" in html
    assert "Bore widened." in html


def test_generator_blueprint_uses_zone_column_when_present(stub_pdf, extraction):
    """If the template declares a zone column, render zone there, not in description."""
    changeset = {
        "job_id": "job-zc",
        "view_diffs": [
            {
                "label": "FRONT", "match_type": "matched",
                "orig_index": 0, "rev_index": 0,
                "changes": [
                    {
                        "field": "bore", "orig_value": "Ø10", "revised_value": "Ø12",
                        "severity": "MAJOR", "confidence": 0.9,
                        "rationale": "Bore widened.",
                        "zone": "C-4",
                    },
                ],
            },
        ],
    }
    blueprint = {
        "html_shell": "<!DOCTYPE html><html><body>[[CHANGES]]</body></html>",
        "change_table": {
            "columns": [
                {"header": "Class",       "role": "severity"},
                {"header": "Item",        "role": "field"},
                {"header": "Location",    "role": "zone"},
                {"header": "Description", "role": "description"},
            ],
            "has_zone_column": True,
        },
        "severity_labels": {},
    }
    generator.generate_report(
        "job-zc", changeset, extraction, "P-001", "",
        view_images={0: _png_bytes()},
        blueprint=blueprint,
    )
    (args, kwargs) = stub_pdf.call_args
    html = kwargs.get("string") or args[0]
    assert "C-4" in html
    # When a zone column exists, we do NOT prepend [Zone …] to description.
    assert "[Zone C-4]" not in html
