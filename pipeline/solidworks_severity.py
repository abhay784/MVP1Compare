"""Deterministic severity rules for SOLIDWORKS-sourced changes.

When the upstream source is SOLIDWORKS COM API output, we have ground-truth
geometry and don't need a vision LLM to estimate severity / confidence. This
module mirrors the precedent set by `pipeline.comparator._diff_title_block`
(see _TITLE_FIELD_SEVERITY there): hardcoded rules, confidence=1.0.

The C# service applies the same rules at extraction time. The Python mirror
exists so callers can re-classify without redeploying the service (gated by
`config.solidworks_reclassify`).
"""
from __future__ import annotations

from typing import Any

from config import config


_STRUCTURAL_FEATURE_KEYWORDS = (
    "Boss", "Cut", "Hole", "Pattern", "Fillet", "Chamfer", "Shell",
    "Rib", "Loft", "Sweep", "Revolve", "Extrude", "Mirror",
)
_COSMETIC_FEATURE_KEYWORDS = (
    "Sketch", "Reference", "Plane", "Axis", "Point", "CosmeticThread",
    "Annotation", "DimXpert",
)


def _is_structural(feature_name: str) -> bool:
    name = feature_name or ""
    if any(k.lower() in name.lower() for k in _COSMETIC_FEATURE_KEYWORDS):
        return False
    if any(k.lower() in name.lower() for k in _STRUCTURAL_FEATURE_KEYWORDS):
        return True
    # Default: treat unknown features as structural (safer — escalates rather than hides).
    return True


def _classify_dimension(old: float | None, new: float | None) -> tuple[str, str]:
    if old is None or new is None:
        return "MAJOR", "Dimension added or removed"
    delta = abs(new - old)
    if delta == 0:
        return "MINOR", "No numeric change"
    pct = (delta / abs(old)) * 100 if old != 0 else float("inf")
    if delta >= 1.0 or pct >= 10.0:
        return "CRITICAL", f"Δ={delta:.3f}mm ({pct:.1f}% of original)"
    if delta >= config.solidworks_dimension_threshold_mm:
        return "MAJOR", f"Δ={delta:.3f}mm above threshold {config.solidworks_dimension_threshold_mm}mm"
    return "MINOR", f"Δ={delta:.3f}mm below threshold"


def _classify_tolerance(old_band: Any, new_band: Any, nominal: float | None) -> tuple[str, str]:
    """Tolerance bands shape `[lower, upper]` in mm relative to nominal."""
    if not old_band or not new_band:
        return "MAJOR", "Tolerance added or removed"
    try:
        ol, ou = float(old_band[0]), float(old_band[1])
        nl, nu = float(new_band[0]), float(new_band[1])
    except (TypeError, ValueError, IndexError):
        return "MAJOR", "Unparseable tolerance band"
    # CRITICAL if the new band excludes any value the old band allowed
    # AND nominal is known (so we can frame it as "old value out of new band").
    if nl > ol or nu < ou:
        return "CRITICAL", f"Tolerance tightened: was [{ol},{ou}], now [{nl},{nu}]"
    return "MAJOR", f"Tolerance shifted: was [{ol},{ou}], now [{nl},{nu}]"


def assign_severity(change: dict) -> tuple[str, float, str]:
    """Return (severity, confidence, rationale) for a SOLIDWORKS-sourced change.

    Confidence is always 1.0 — we have CAD ground truth.

    Expected `change` keys (per the SW service contract):
        type:           "dimension" | "tolerance" | "material" |
                        "feature_added" | "feature_removed" | "suppression" |
                        "mass" | "volume" | "bbox"
        feature_name:   str (optional)
        old_value:      number | str | None
        new_value:      number | str | None
        tolerance_old:  [low, high] | None
        tolerance_new:  [low, high] | None
    """
    kind = change.get("type", "")
    feat = change.get("feature_name", "")

    if kind == "material":
        return "CRITICAL", 1.0, f"Material changed: {change.get('old_value')} → {change.get('new_value')}"

    if kind in ("feature_added", "feature_removed"):
        sev = "CRITICAL" if _is_structural(feat) else "MINOR"
        verb = "added" if kind == "feature_added" else "removed"
        return sev, 1.0, f"{('Structural' if sev == 'CRITICAL' else 'Cosmetic')} feature {verb}: {feat}"

    if kind == "suppression":
        sev = "MAJOR" if _is_structural(feat) else "MINOR"
        return sev, 1.0, f"Suppression toggled on {feat} ({change.get('old_value')} → {change.get('new_value')})"

    if kind == "dimension":
        sev, why = _classify_dimension(
            _to_float(change.get("old_value")),
            _to_float(change.get("new_value")),
        )
        return sev, 1.0, why

    if kind == "tolerance":
        sev, why = _classify_tolerance(
            change.get("tolerance_old"),
            change.get("tolerance_new"),
            _to_float(change.get("nominal")),
        )
        return sev, 1.0, why

    if kind in ("mass", "volume", "bbox"):
        return "MINOR", 1.0, f"{kind} delta (informational)"

    # Unknown change type — defensive default
    return "MAJOR", 1.0, f"Unclassified change type: {kind}"


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
