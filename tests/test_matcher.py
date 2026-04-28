"""Tests for pipeline/matcher.py — Step 4: View Matching.

Run locally (no AWS needed):
    pytest tests/test_matcher.py -v
"""
from __future__ import annotations

import pytest

from pipeline.matcher import ViewMatch, match_views


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _view(index: int, label: str) -> dict:
    return {
        "index":         index,
        "view_label":    label,
        "area_fraction": 0.2,
        "bbox":          [0, 0, 100, 100],
        "crop_s3_key":   f"crops/job/original/view_{index:02d}.png",
        "dimensions":    [],
    }


def _rev_view(index: int, label: str) -> dict:
    v = _view(index, label)
    v["crop_s3_key"] = f"crops/job/revised/view_{index:02d}.png"
    return v


def _by_type(matches: list[ViewMatch], match_type: str) -> list[ViewMatch]:
    return [m for m in matches if m.match_type == match_type]


# ---------------------------------------------------------------------------
# Special blocks
# ---------------------------------------------------------------------------

class TestSpecialBlocks:
    def test_title_block_always_present(self):
        matches = match_views([], [])
        assert any(m.match_type == "title_block" for m in matches)

    def test_revision_block_always_present(self):
        matches = match_views([], [])
        assert any(m.match_type == "revision_block" for m in matches)

    def test_special_blocks_score_is_one(self):
        matches = match_views([], [])
        for m in matches:
            assert m.score == 1.0

    def test_special_blocks_present_even_with_views(self):
        orig = [_view(0, "FRONT VIEW")]
        rev  = [_rev_view(0, "FRONT VIEW")]
        matches = match_views(orig, rev)
        types = {m.match_type for m in matches}
        assert "title_block" in types
        assert "revision_block" in types


# ---------------------------------------------------------------------------
# Exact label match
# ---------------------------------------------------------------------------

class TestExactMatch:
    def test_exact_match_score_is_one(self):
        orig = [_view(0, "FRONT VIEW")]
        rev  = [_rev_view(0, "FRONT VIEW")]
        matched = _by_type(match_views(orig, rev), "matched")
        assert len(matched) == 1
        assert matched[0].score == pytest.approx(1.0)

    def test_exact_match_indices(self):
        orig = [_view(0, "FRONT VIEW")]
        rev  = [_rev_view(0, "FRONT VIEW")]
        m = _by_type(match_views(orig, rev), "matched")[0]
        assert m.orig_index == 0
        assert m.rev_index == 0

    def test_exact_match_label_preserved(self):
        orig = [_view(0, "SECTION A-A")]
        rev  = [_rev_view(0, "SECTION A-A")]
        m = _by_type(match_views(orig, rev), "matched")[0]
        assert m.label == "SECTION A-A"

    def test_multiple_exact_matches(self):
        orig = [_view(0, "FRONT VIEW"), _view(1, "SECTION A-A"), _view(2, "DETAIL B")]
        rev  = [_rev_view(0, "FRONT VIEW"), _rev_view(1, "SECTION A-A"), _rev_view(2, "DETAIL B")]
        matched = _by_type(match_views(orig, rev), "matched")
        assert len(matched) == 3


# ---------------------------------------------------------------------------
# Fuzzy match (above threshold)
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    def test_fuzzy_match_above_threshold(self):
        # "SECTION A-A" vs "SECTION AA" — similar but not identical
        orig = [_view(0, "SECTION A-A")]
        rev  = [_rev_view(0, "SECTION AA")]
        matched = _by_type(match_views(orig, rev), "matched")
        assert len(matched) == 1
        assert matched[0].score > 0.80

    def test_fuzzy_match_case_insensitive(self):
        orig = [_view(0, "front view")]
        rev  = [_rev_view(0, "FRONT VIEW")]
        matched = _by_type(match_views(orig, rev), "matched")
        assert len(matched) == 1

    def test_fuzzy_match_minor_typo(self):
        # One extra space — should still match
        orig = [_view(0, "DETAIL  B")]
        rev  = [_rev_view(0, "DETAIL B")]
        matched = _by_type(match_views(orig, rev), "matched")
        assert len(matched) == 1


# ---------------------------------------------------------------------------
# No-match: REMOVED and ADDED
# ---------------------------------------------------------------------------

class TestNoMatch:
    def test_orig_only_view_is_removed(self):
        orig = [_view(0, "ISOMETRIC VIEW")]
        rev  = []
        removed = _by_type(match_views(orig, rev), "removed")
        assert len(removed) == 1
        assert removed[0].orig_index == 0
        assert removed[0].rev_index is None

    def test_rev_only_view_is_added(self):
        orig = []
        rev  = [_rev_view(0, "ISOMETRIC VIEW")]
        added = _by_type(match_views(orig, rev), "added")
        assert len(added) == 1
        assert added[0].rev_index == 0
        assert added[0].orig_index is None

    def test_below_threshold_not_matched(self):
        # "FRONT VIEW" vs "REAR SECTION" — very different, should not match
        orig = [_view(0, "FRONT VIEW")]
        rev  = [_rev_view(0, "REAR SECTION")]
        matched = _by_type(match_views(orig, rev), "matched")
        assert len(matched) == 0

    def test_below_threshold_yields_removed_and_added(self):
        orig = [_view(0, "FRONT VIEW")]
        rev  = [_rev_view(0, "REAR SECTION")]
        matches = match_views(orig, rev)
        assert any(m.match_type == "removed" for m in matches)
        assert any(m.match_type == "added" for m in matches)

    def test_mixed_matched_and_unmatched(self):
        orig = [_view(0, "FRONT VIEW"), _view(1, "DELETED VIEW")]
        rev  = [_rev_view(0, "FRONT VIEW"), _rev_view(1, "NEW VIEW")]
        matches = match_views(orig, rev)
        assert len(_by_type(matches, "matched")) == 1
        assert len(_by_type(matches, "removed")) == 1
        assert len(_by_type(matches, "added")) == 1


# ---------------------------------------------------------------------------
# Title block always matches regardless of score (structural invariant)
# ---------------------------------------------------------------------------

class TestTitleBlockAlwaysMatches:
    def test_title_block_present_with_no_views(self):
        matches = match_views([], [])
        tb = [m for m in matches if m.match_type == "title_block"]
        assert len(tb) == 1

    def test_title_block_index_is_none(self):
        # title_block is a synthetic entry, not tied to a view index
        matches = match_views([], [])
        tb = [m for m in matches if m.match_type == "title_block"][0]
        assert tb.orig_index is None
        assert tb.rev_index is None

    def test_title_block_score_is_one_regardless_of_views(self):
        orig = [_view(0, "ZZZZZ")]
        rev  = [_rev_view(0, "AAAAA")]
        tb = [m for m in match_views(orig, rev) if m.match_type == "title_block"][0]
        assert tb.score == 1.0


# ---------------------------------------------------------------------------
# Real extraction.json-shaped structure
# ---------------------------------------------------------------------------

class TestExtractionJsonShape:
    """Verify matcher works with the exact dict shape written by pipeline/run.py."""

    def _make_extraction_view(self, index: int, label: str, kind: str) -> dict:
        return {
            "index":         index,
            "view_label":    label,
            "area_fraction": 0.15,
            "bbox":          [10, 10, 200, 200],
            "crop_s3_key":   f"crops/job123/{kind}/view_{index:02d}.png",
            "dimensions": [
                {"value": "25.4", "numeric": 25.4, "bbox": [50, 50, 80, 60]},
            ],
        }

    def test_full_extraction_shape_matches(self):
        orig_views = [
            self._make_extraction_view(0, "FRONT VIEW", "original"),
            self._make_extraction_view(1, "SECTION A-A", "original"),
        ]
        rev_views = [
            self._make_extraction_view(0, "FRONT VIEW", "revised"),
            self._make_extraction_view(1, "SECTION A-A", "revised"),
        ]
        matches = match_views(orig_views, rev_views)
        matched = _by_type(matches, "matched")
        assert len(matched) == 2

    def test_crop_keys_accessible_via_view_dict(self):
        orig_views = [self._make_extraction_view(0, "FRONT VIEW", "original")]
        rev_views  = [self._make_extraction_view(0, "FRONT VIEW", "revised")]
        matches = match_views(orig_views, rev_views)
        m = _by_type(matches, "matched")[0]
        # The comparator (Step 5) resolves crop keys from the view dicts via orig_index/rev_index.
        assert orig_views[m.orig_index]["crop_s3_key"].startswith("crops/")
        assert rev_views[m.rev_index]["crop_s3_key"].startswith("crops/")

    def test_each_view_matched_at_most_once(self):
        orig_views = [self._make_extraction_view(0, "FRONT VIEW", "original")]
        rev_views  = [
            self._make_extraction_view(0, "FRONT VIEW", "revised"),
            self._make_extraction_view(1, "FRONT VIEW", "revised"),  # duplicate label
        ]
        matches = match_views(orig_views, rev_views)
        matched = _by_type(matches, "matched")
        orig_indices = [m.orig_index for m in matched]
        rev_indices  = [m.rev_index  for m in matched]
        assert len(set(orig_indices)) == len(orig_indices), "orig views matched more than once"
        assert len(set(rev_indices))  == len(rev_indices),  "rev views matched more than once"
