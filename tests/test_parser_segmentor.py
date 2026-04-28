"""Integration smoke test for Step 3: parser → segmentor → extractor.

Creates a minimal synthetic PDF (one bordered view, one changed dimension),
runs the full extraction pipeline, and asserts the expected outputs.

Run locally (no AWS needed — S3 calls are skipped):
    pytest tests/test_parser_segmentor.py -v
"""
from __future__ import annotations

import io

import fitz  # pymupdf
import numpy as np
import pytest

from pipeline.extractor import extract_dimensions, extract_title_block
from pipeline.parser import parse_pdf
from pipeline.segmentor import segment_views


# ---------------------------------------------------------------------------
# Synthetic PDF factory
# ---------------------------------------------------------------------------

def _make_test_pdf(dimension_text: str = "25.4") -> bytes:
    """Return a minimal single-page vector PDF with:
    - One large bordered view rectangle (>5% of page area)
    - A dimension value text inside the view
    - A minimal title block in the bottom-right corner
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 portrait in points

    # Draw a view border: large rectangle occupying ~60% of page
    view_rect = fitz.Rect(50, 80, 450, 600)
    page.draw_rect(view_rect, color=(0, 0, 0), width=1.5)

    # Insert a dimension value inside the view
    page.insert_text(
        (200, 340),
        dimension_text,
        fontsize=12,
        color=(0, 0, 0),
    )

    # Minimal title block text in bottom-right region
    page.insert_text(
        (400, 720),
        "PART NO: TEST-001",
        fontsize=8,
        color=(0, 0, 0),
    )
    page.insert_text(
        (400, 735),
        "REV: A",
        fontsize=8,
        color=(0, 0, 0),
    )
    page.insert_text(
        (400, 750),
        "MATERIAL: AL6061",
        fontsize=8,
        color=(0, 0, 0),
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParser:
    def test_parse_returns_image_and_text(self):
        pdf_bytes = _make_test_pdf("25.4")
        page = parse_pdf(pdf_bytes)

        assert page.image.ndim == 3, "image should be 3-D RGB array"
        assert page.image.shape[2] == 3
        assert page.image.dtype == np.uint8

        assert page.page_width_pt == pytest.approx(595, abs=1)
        assert page.page_height_pt == pytest.approx(842, abs=1)

        # The synthetic PDF is vector — should not be flagged as raster
        assert page.is_raster is False

    def test_parse_extracts_dimension_token(self):
        pdf_bytes = _make_test_pdf("12.7")
        page = parse_pdf(pdf_bytes)

        tokens = [tb.text for tb in page.text_blocks]
        assert "12.7" in tokens, f"Expected '12.7' in text blocks; got: {tokens}"

    def test_parse_raster_flag_false_for_vector(self):
        pdf_bytes = _make_test_pdf()
        page = parse_pdf(pdf_bytes)
        assert page.is_raster is False


class TestSegmentor:
    def test_segment_finds_at_least_one_view(self):
        pdf_bytes = _make_test_pdf()
        page = parse_pdf(pdf_bytes)
        views = segment_views(page, pdf_bytes)

        assert len(views) >= 1, "Expected at least one view to be detected"

    def test_view_crop_is_nonempty_array(self):
        pdf_bytes = _make_test_pdf()
        page = parse_pdf(pdf_bytes)
        views = segment_views(page, pdf_bytes)

        for v in views:
            assert v.image.size > 0, "View crop should not be empty"
            assert v.area_fraction >= 0.05

    def test_view_has_label(self):
        pdf_bytes = _make_test_pdf()
        page = parse_pdf(pdf_bytes)
        views = segment_views(page, pdf_bytes)

        # Every view must have a non-empty label (synthesised if not extracted)
        for v in views:
            assert v.view_label, f"View at ({v.bbox_x0:.0f},{v.bbox_y0:.0f}) has no label"


class TestExtractor:
    def test_extract_dimensions_finds_value(self):
        pdf_bytes = _make_test_pdf("25.4")
        page = parse_pdf(pdf_bytes)
        dims = extract_dimensions(page.text_blocks)

        values = [d.value for d in dims]
        assert "25.4" in values, f"Expected '25.4' in dimensions; got: {values}"

    def test_extract_dimensions_parses_numeric(self):
        pdf_bytes = _make_test_pdf("25.4")
        page = parse_pdf(pdf_bytes)
        dims = extract_dimensions(page.text_blocks)

        matched = [d for d in dims if d.value == "25.4"]
        assert matched, "Dimension '25.4' not found"
        assert matched[0].numeric == pytest.approx(25.4)

    def test_title_block_extracts_part_number(self):
        pdf_bytes = _make_test_pdf()
        tb = extract_title_block(pdf_bytes)
        assert tb.part_number == "TEST-001", f"Got: {tb.part_number!r}"

    def test_title_block_extracts_revision(self):
        pdf_bytes = _make_test_pdf()
        tb = extract_title_block(pdf_bytes)
        assert tb.revision == "A", f"Got: {tb.revision!r}"


class TestEndToEnd:
    """Smoke test: original (25.4) vs revised (26.0) — different dimension, same structure."""

    def test_two_pdfs_produce_different_dimension_sets(self):
        orig_bytes = _make_test_pdf("25.4")
        rev_bytes = _make_test_pdf("26.0")

        orig_page = parse_pdf(orig_bytes)
        rev_page = parse_pdf(rev_bytes)

        orig_dims = {d.value for d in extract_dimensions(orig_page.text_blocks)}
        rev_dims = {d.value for d in extract_dimensions(rev_page.text_blocks)}

        assert "25.4" in orig_dims
        assert "26.0" in rev_dims
        assert orig_dims != rev_dims, "Original and revised should differ in extracted dimensions"

    def test_both_pdfs_segment_same_view_count(self):
        orig_bytes = _make_test_pdf("25.4")
        rev_bytes = _make_test_pdf("26.0")

        orig_page = parse_pdf(orig_bytes)
        rev_page = parse_pdf(rev_bytes)

        orig_views = segment_views(orig_page, orig_bytes)
        rev_views = segment_views(rev_page, rev_bytes)

        assert len(orig_views) == len(rev_views), (
            f"View count mismatch: orig={len(orig_views)}, rev={len(rev_views)}"
        )
