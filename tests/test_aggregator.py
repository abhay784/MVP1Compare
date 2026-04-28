"""Tests for pipeline/aggregator.py — Step 7."""
from __future__ import annotations

import pytest

from pipeline.aggregator import aggregate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _view(label: str, match_type: str, changes: list[dict]) -> dict:
    return {"label": label, "match_type": match_type, "changes": changes}


def _change(field: str, orig: str | None, revised: str | None, severity: str, confidence: float, rationale: str = "") -> dict:
    return {
        "field": field,
        "orig_value": orig,
        "revised_value": revised,
        "severity": severity,
        "confidence": confidence,
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# Intra-view deduplication
# ---------------------------------------------------------------------------

def test_intra_view_dedup_highest_confidence_wins():
    """Same field twice → one entry, highest confidence kept."""
    ch_low  = _change("bore_diameter", "12.50", "12.70", "MAJOR", 0.70, "low conf")
    ch_high = _change("bore_diameter", "12.50", "12.70", "MAJOR", 0.92, "high conf")
    result = aggregate([_view("FRONT", "matched", [ch_low, ch_high])])
    changes = result[0]["changes"]
    assert len(changes) == 1
    assert changes[0]["confidence"] == pytest.approx(0.92)
    assert changes[0]["rationale"] == "high conf"


def test_intra_view_dedup_severity_tiebreak():
    """Same field, same confidence → higher severity (CRITICAL > MAJOR) wins."""
    ch_major    = _change("bore_diameter", "12.50", "12.70", "MAJOR",    0.80)
    ch_critical = _change("bore_diameter", "12.50", "12.70", "CRITICAL", 0.80)
    result = aggregate([_view("FRONT", "matched", [ch_major, ch_critical])])
    changes = result[0]["changes"]
    assert len(changes) == 1
    assert changes[0]["severity"] == "CRITICAL"


# ---------------------------------------------------------------------------
# Cross-view deduplication
# ---------------------------------------------------------------------------

def test_cross_view_dedup_keeps_highest_confidence():
    """Same triple in two matched views → one remains, highest confidence wins."""
    ch_a = _change("bore_diameter", "12.50", "12.70", "MAJOR", 0.90, "front rationale")
    ch_b = _change("bore_diameter", "12.50", "12.70", "MAJOR", 0.75, "side rationale")
    result = aggregate([
        _view("FRONT", "matched", [ch_a]),
        _view("SIDE",  "matched", [ch_b]),
    ])
    all_changes = [ch for vd in result for ch in vd["changes"]]
    assert len(all_changes) == 1
    assert all_changes[0]["confidence"] == pytest.approx(0.90)
    assert all_changes[0]["rationale"].startswith("[deduped across views]")


def test_cross_view_dedup_does_not_merge_added_removed():
    """added/removed views are exempt from cross-view dedup."""
    ch_a = _change("view", None, "BOTTOM", "MAJOR", 1.0, "added")
    ch_b = _change("view", None, "BOTTOM", "MAJOR", 1.0, "also added")
    result = aggregate([
        _view("BOTTOM", "added",   [ch_a]),
        _view("BOTTOM", "removed", [ch_b]),
    ])
    all_changes = [ch for vd in result for ch in vd["changes"]]
    assert len(all_changes) == 2


# ---------------------------------------------------------------------------
# Severity sort
# ---------------------------------------------------------------------------

def test_severity_sort_order():
    """Mixed input → CRITICAL first, UNCERTAIN last, ties by descending confidence."""
    changes = [
        _change("f1", None, None, "UNCERTAIN", 0.60),
        _change("f2", None, None, "MINOR",     0.80),
        _change("f3", None, None, "CRITICAL",  0.95),
        _change("f4", None, None, "MAJOR",     0.85),
        _change("f5", None, None, "MINOR",     0.70),
    ]
    result = aggregate([_view("FRONT", "matched", changes)])
    sevs = [ch["severity"] for ch in result[0]["changes"]]
    assert sevs == ["CRITICAL", "MAJOR", "MINOR", "MINOR", "UNCERTAIN"]
    # Within MINOR, higher confidence comes first
    minor_confs = [ch["confidence"] for ch in result[0]["changes"] if ch["severity"] == "MINOR"]
    assert minor_confs == sorted(minor_confs, reverse=True)


# ---------------------------------------------------------------------------
# UNCERTAIN* promotion
# ---------------------------------------------------------------------------

def test_uncertain_promotion_above_threshold():
    """UNCERTAIN field matches CRITICAL at conf ≥ 0.80 in another view → UNCERTAIN*."""
    ch_strong   = _change("bore_diameter", "12.50", "12.70", "CRITICAL",  0.90, "strong")
    ch_uncertain = _change("bore_diameter", "12.50", "12.70", "UNCERTAIN", 0.55, "weak")
    result = aggregate([
        _view("FRONT", "matched", [ch_strong]),
        _view("SIDE",  "matched", [ch_uncertain]),
    ])
    # The strong one is the cross-view winner; the weak one is eliminated.
    # So we test promotion on distinct triples to avoid cross-view dedup removing it.
    ch_strong2    = _change("wall_thickness", "2.00", "2.10", "CRITICAL",  0.85, "strong2")
    ch_uncertain2 = _change("wall_thickness", "1.90", "2.10", "UNCERTAIN", 0.55, "weak2")
    result2 = aggregate([
        _view("FRONT", "matched", [ch_strong2]),
        _view("SIDE",  "matched", [ch_uncertain2]),
    ])
    side_changes = result2[1]["changes"]
    assert len(side_changes) == 1
    assert side_changes[0]["severity"] == "UNCERTAIN*"
    assert "[possible match: FRONT]" in side_changes[0]["rationale"]


def test_uncertain_promotion_below_threshold():
    """conf 0.79 → no promotion, severity stays UNCERTAIN."""
    ch_strong    = _change("wall_thickness", "2.00", "2.10", "CRITICAL",  0.79, "below")
    ch_uncertain = _change("wall_thickness", "1.90", "2.10", "UNCERTAIN", 0.55, "weak")
    result = aggregate([
        _view("FRONT", "matched", [ch_strong]),
        _view("SIDE",  "matched", [ch_uncertain]),
    ])
    side_changes = result[1]["changes"]
    assert side_changes[0]["severity"] == "UNCERTAIN"


# ---------------------------------------------------------------------------
# Edge case
# ---------------------------------------------------------------------------

def test_empty_changeset_returns_empty_list():
    assert aggregate([]) == []
