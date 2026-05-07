"""Tests for pipeline.graph — page-graph builder.

Pass A is deterministic and covered here directly. Pass B is mocked through
build_page_graph(run_llm_pass=False) so tests don't require an API key.
"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline.graph import (
    PageGraph,
    _build_pass_a,
    build_page_graph,
    graph_to_dict,
)
from pipeline.parser import ParsedPage, TextBlock
from pipeline.segmentor import ViewCrop


def _crop(label: str, x0: float, y0: float, x1: float, y1: float, area: float = 0.2) -> ViewCrop:
    return ViewCrop(
        image=np.zeros((4, 4, 3), dtype=np.uint8),
        bbox_x0=x0, bbox_y0=y0, bbox_x1=x1, bbox_y1=y1,
        view_label=label,
        area_fraction=area,
        page_index=0,
    )


def _tb(text: str, cx: float, cy: float) -> TextBlock:
    return TextBlock(text=text, bbox_x0=cx - 1, bbox_y0=cy - 1,
                     bbox_x1=cx + 1, bbox_y1=cy + 1)


def _page(text_blocks, is_raster=False) -> ParsedPage:
    return ParsedPage(
        image=np.zeros((10, 10, 3), dtype=np.uint8),
        text_blocks=text_blocks,
        raw_text="",
        page_width_pt=1000.0,
        page_height_pt=800.0,
        is_raster=is_raster,
    )


# ---------------------------------------------------------------------------
# Pass A — deterministic topology
# ---------------------------------------------------------------------------

class TestPassA:
    def test_pairs_section_with_parent_via_cut_letters(self):
        # Parent rectangle covers the middle of the page; SECTION B-B is a separate
        # crop on the left. Cut-line letter "B" appears twice inside the parent.
        crops = [
            _crop("MAIN_TOP", 200, 100, 800, 500, area=0.4),  # parent
            _crop("SECTION B-B", 50, 100, 180, 700, area=0.15),  # derived
        ]
        text_blocks = [
            _tb("B", 250, 200),  # first cut-marker letter inside parent
            _tb("B", 700, 200),  # second cut-marker letter inside parent
            _tb("SECTION", 100, 750),
        ]
        page = _page(text_blocks)
        pa = _build_pass_a(page, crops)

        derived = pa["derived"]
        assert len(derived) == 1
        assert derived[0]["label"] == "SECTION B-B"
        assert derived[0]["parent_label"] == "MAIN_TOP"
        assert derived[0]["cut_id"] == "B-B"

        parents = {p["label"]: p for p in pa["parents"]}
        assert "MAIN_TOP" in parents
        assert "B-B" in parents["MAIN_TOP"]["cut_ids"]

    def test_three_sections_share_one_parent(self):
        # Page-2-style layout: one parent, three sections (A-A, B-B, C-C).
        crops = [
            _crop("TOP", 200, 100, 800, 500, area=0.4),
            _crop("SECTION A-A", 850, 100, 980, 500),
            _crop("SECTION B-B", 50, 100, 180, 500),
            _crop("SECTION C-C", 200, 600, 800, 750),
        ]
        text_blocks = [
            _tb("A", 250, 150), _tb("A", 700, 150),  # cut A inside TOP
            _tb("B", 300, 200), _tb("B", 650, 200),  # cut B inside TOP
            _tb("C", 400, 300), _tb("C", 600, 300),  # cut C inside TOP
        ]
        page = _page(text_blocks)
        pa = _build_pass_a(page, crops)

        derived_by_id = {d["cut_id"]: d for d in pa["derived"]}
        assert set(derived_by_id) == {"A-A", "B-B", "C-C"}
        assert all(d["parent_label"] == "TOP" for d in derived_by_id.values())

    def test_raster_skips_pass_a(self):
        crops = [_crop("SECTION B-B", 0, 0, 1, 1)]
        page = _page([], is_raster=True)
        pa = _build_pass_a(page, crops)
        assert pa.get("skipped") == "raster"
        assert pa.get("derived") == []


# ---------------------------------------------------------------------------
# build_page_graph — mutates ViewCrops in place
# ---------------------------------------------------------------------------

class TestBuildGraph:
    def test_mutates_view_crops_with_role_and_cut_id(self):
        crops = [
            _crop("TOP", 200, 100, 800, 500, area=0.4),
            _crop("SECTION B-B", 50, 100, 180, 500),
        ]
        text_blocks = [_tb("B", 250, 200), _tb("B", 700, 200)]
        page = _page(text_blocks)

        graph = build_page_graph(page, crops, run_llm_pass=False)

        roles = {c.view_label: c.view_role for c in crops}
        assert roles["TOP"] == "parent"
        assert roles["SECTION B-B"] == "derived"

        section = next(c for c in crops if c.view_label == "SECTION B-B")
        assert section.parent_label == "TOP"
        assert section.cut_id == "B-B"

        assert isinstance(graph, PageGraph)
        assert any(d.cut_id == "B-B" for d in graph.derived)

    def test_graph_to_dict_roundtrip_has_expected_keys(self):
        crops = [_crop("TOP", 0, 0, 100, 100)]
        page = _page([])
        graph = build_page_graph(page, crops, run_llm_pass=False)
        d = graph_to_dict(graph)
        assert set(d) == {"page_index", "parents", "derived", "part_summary"}
