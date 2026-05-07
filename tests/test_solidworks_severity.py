"""Severity rule tests for SOLIDWORKS-sourced changes."""
from __future__ import annotations

import pytest

from pipeline.solidworks_severity import assign_severity


def _sev(change: dict) -> str:
    return assign_severity(change)[0]


def test_material_change_is_critical():
    sev, conf, _ = assign_severity({
        "type": "material",
        "old_value": "Steel",
        "new_value": "Aluminum",
    })
    assert sev == "CRITICAL"
    assert conf == 1.0


def test_large_dimension_change_is_critical():
    # 20 → 25 = 5mm delta, 25% — definitely critical
    assert _sev({"type": "dimension", "old_value": 20.0, "new_value": 25.0}) == "CRITICAL"


def test_dimension_change_above_threshold_is_major():
    # default threshold 0.1mm; 0.5mm delta on 100mm nominal = 0.5%, sub-1mm
    assert _sev({"type": "dimension", "old_value": 100.0, "new_value": 100.5}) == "MAJOR"


def test_dimension_change_below_threshold_is_minor():
    # 0.05mm delta, below default 0.1mm threshold
    assert _sev({"type": "dimension", "old_value": 10.0, "new_value": 10.05}) == "MINOR"


def test_dimension_zero_delta_is_minor():
    assert _sev({"type": "dimension", "old_value": 5.0, "new_value": 5.0}) == "MINOR"


def test_dimension_added_is_major():
    # Old missing → can't measure delta, but the dim itself appearing is significant
    assert _sev({"type": "dimension", "old_value": None, "new_value": 12.0}) == "MAJOR"


def test_structural_feature_added_is_critical():
    assert _sev({"type": "feature_added", "feature_name": "Boss-Extrude5"}) == "CRITICAL"
    assert _sev({"type": "feature_removed", "feature_name": "Cut-Extrude2"}) == "CRITICAL"
    assert _sev({"type": "feature_added", "feature_name": "Hole1"}) == "CRITICAL"


def test_cosmetic_feature_added_is_minor():
    assert _sev({"type": "feature_added", "feature_name": "Sketch12"}) == "MINOR"
    assert _sev({"type": "feature_removed", "feature_name": "Reference Plane"}) == "MINOR"


def test_suppression_structural_is_major():
    assert _sev({
        "type": "suppression",
        "feature_name": "Boss-Extrude1",
        "old_value": False,
        "new_value": True,
    }) == "MAJOR"


def test_suppression_cosmetic_is_minor():
    assert _sev({
        "type": "suppression",
        "feature_name": "Sketch3",
        "old_value": False,
        "new_value": True,
    }) == "MINOR"


def test_tolerance_tightened_is_critical():
    sev, _, why = assign_severity({
        "type": "tolerance",
        "tolerance_old": [-0.1, 0.1],
        "tolerance_new": [-0.05, 0.05],
    })
    assert sev == "CRITICAL"
    assert "tightened" in why.lower()


def test_tolerance_shifted_is_major():
    sev, _, _ = assign_severity({
        "type": "tolerance",
        "tolerance_old": [-0.1, 0.1],
        "tolerance_new": [-0.2, 0.2],   # loosened, not tightened
    })
    assert sev == "MAJOR"


def test_mass_volume_bbox_are_minor():
    for kind in ("mass", "volume", "bbox"):
        assert _sev({"type": kind, "old_value": 1.0, "new_value": 1.1}) == "MINOR"


def test_unknown_type_defaults_to_major():
    assert _sev({"type": "totally_made_up", "old_value": "a", "new_value": "b"}) == "MAJOR"


def test_confidence_is_always_one():
    for change in [
        {"type": "material", "old_value": "x", "new_value": "y"},
        {"type": "dimension", "old_value": 1.0, "new_value": 2.0},
        {"type": "feature_added", "feature_name": "Boss1"},
        {"type": "suppression", "feature_name": "X", "old_value": False, "new_value": True},
    ]:
        _, conf, _ = assign_severity(change)
        assert conf == 1.0
