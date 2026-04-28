"""Change Aggregator — Step 7 of the DrawDiff pipeline.

Pure-Python transformer on the changeset's view_diffs list.  No S3, no config,
no Anthropic.  Input and output share the same list[dict] shape produced by
viewdiff_to_dict() in comparator.py.

Responsibilities (applied in order):
  1. Intra-view dedup  — merge duplicate field entries within the same view.
  2. Cross-view dedup  — collapse the same (field, orig, revised) triple that
                         appears in more than one *matched* view.
  3. Severity sort     — CRITICAL → MAJOR → MINOR → UNCERTAIN within each view.
  4. UNCERTAIN*        — soft-promote UNCERTAIN changes whose field appears as
                         CRITICAL/MAJOR in another view with conf ≥ 0.80.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

_SEVERITY_RANK: dict[str, int] = {
    "CRITICAL":  0,
    "MAJOR":     1,
    "MINOR":     2,
    "UNCERTAIN": 3,
}

# Strip trailing asterisk so promotion bookkeeping doesn't compound.
def _base_severity(sev: str) -> str:
    return sev.rstrip("*")


def _rank(sev: str) -> int:
    """Lower rank = higher severity.  Unknown severities sort last."""
    return _SEVERITY_RANK.get(_base_severity(sev), 99)


# ---------------------------------------------------------------------------
# Intra-view deduplication
# ---------------------------------------------------------------------------

def _dedup_within_view(changes: list[dict]) -> list[dict]:
    """Merge entries that share the same `field` string within one view.

    Tie-breaking rules (applied in order):
      1. Highest confidence wins.
      2. If confidence equal, highest severity (lowest rank) wins.
    """
    seen: dict[str, dict] = {}
    for ch in changes:
        key = ch["field"]
        if key not in seen:
            seen[key] = ch
        else:
            incumbent = seen[key]
            ch_conf = ch.get("confidence", 0.0)
            inc_conf = incumbent.get("confidence", 0.0)
            if ch_conf > inc_conf:
                seen[key] = ch
            elif ch_conf == inc_conf and _rank(ch.get("severity", "UNCERTAIN")) < _rank(incumbent.get("severity", "UNCERTAIN")):
                seen[key] = ch
    return list(seen.values())


# ---------------------------------------------------------------------------
# Cross-view deduplication (matched views only)
# ---------------------------------------------------------------------------

def _cross_view_dedup(view_diffs: list[dict]) -> list[dict]:
    """Remove duplicate (field, orig_value, revised_value) triples across matched views.

    Only views with match_type == "matched" participate.  When a triple appears
    in multiple matched views, the highest-confidence instance is kept; its
    rationale is prefixed with "[deduped across views] ".  The losing views
    have that change entry removed entirely.
    """
    # Map (field, orig, revised) → best candidate seen so far
    # value: (view_index, change_index, confidence)
    best: dict = {}

    for vi, vd in enumerate(view_diffs):
        if vd.get("match_type") != "matched":
            continue
        for ci, ch in enumerate(vd.get("changes") or []):
            triple = (ch.get("field"), ch.get("orig_value"), ch.get("revised_value"))
            conf = ch.get("confidence", 0.0)
            if triple not in best or conf > best[triple][2]:
                best[triple] = (vi, ci, conf)

    # Second pass: mark non-winning duplicates for removal and prefix winners.
    winner_triples = {t: (vi, ci) for t, (vi, ci, _) in best.items()}

    # Track which triples appear in more than one matched view.
    triple_views: dict = {}
    for vi, vd in enumerate(view_diffs):
        if vd.get("match_type") != "matched":
            continue
        for ch in vd.get("changes") or []:
            triple = (ch.get("field"), ch.get("orig_value"), ch.get("revised_value"))
            triple_views.setdefault(triple, []).append(vi)

    duplicated = {t for t, views in triple_views.items() if len(views) > 1}

    result: list[dict] = []
    for vi, vd in enumerate(view_diffs):
        new_changes: list[dict] = []
        for ci, ch in enumerate(vd.get("changes") or []):
            triple = (ch.get("field"), ch.get("orig_value"), ch.get("revised_value"))
            if triple not in duplicated or vd.get("match_type") != "matched":
                new_changes.append(ch)
            else:
                win_vi, win_ci = winner_triples[triple]
                if vi == win_vi and ci == win_ci:
                    # This is the winner — prefix its rationale.
                    ch = dict(ch)
                    ch["rationale"] = "[deduped across views] " + ch.get("rationale", "")
                    new_changes.append(ch)
                # else: discard the duplicate
        result.append({**vd, "changes": new_changes})

    return result


# ---------------------------------------------------------------------------
# Severity sort
# ---------------------------------------------------------------------------

def _sort_by_severity(changes: list[dict]) -> list[dict]:
    """CRITICAL first, UNCERTAIN last; ties broken by descending confidence."""
    return sorted(changes, key=lambda c: (_rank(c.get("severity", "UNCERTAIN")), -c.get("confidence", 0.0)))


# ---------------------------------------------------------------------------
# UNCERTAIN* promotion
# ---------------------------------------------------------------------------

def _promote_uncertain(view_diffs: list[dict]) -> list[dict]:
    """Soft-promote UNCERTAIN changes whose field matches a high-confidence CRITICAL/MAJOR.

    Conditions:
      - The promoting view must be a different view than the UNCERTAIN one.
      - The other view must have a change for the same `field` with severity
        CRITICAL or MAJOR and confidence ≥ 0.80.
    Promotion adds '*' to the severity and appends " [possible match: {label}]"
    to the rationale.  It is NOT a hard reclassification.
    """
    # Build index: field → list of (label, severity, confidence) from other views.
    PROMO_THRESHOLD = 0.80
    # field → [(label, severity, confidence), ...]
    strong_by_field: dict[str, list[tuple[str, str, float]]] = {}
    for vd in view_diffs:
        label = vd.get("label", "")
        for ch in vd.get("changes") or []:
            base = _base_severity(ch.get("severity", ""))
            if base in ("CRITICAL", "MAJOR") and ch.get("confidence", 0.0) >= PROMO_THRESHOLD:
                strong_by_field.setdefault(ch["field"], []).append((label, base, ch["confidence"]))

    result: list[dict] = []
    for vd in view_diffs:
        label = vd.get("label", "")
        new_changes: list[dict] = []
        for ch in vd.get("changes") or []:
            ch = dict(ch)
            base = _base_severity(ch.get("severity", ""))
            if base == "UNCERTAIN":
                field = ch.get("field", "")
                # Find any strong match from a *different* view.
                others = [
                    (other_label, sev, conf)
                    for (other_label, sev, conf) in strong_by_field.get(field, [])
                    if other_label != label
                ]
                if others:
                    # Pick the strongest (highest confidence) promoter.
                    other_label, _, _ = max(others, key=lambda x: x[2])
                    ch["severity"] = "UNCERTAIN*"
                    ch["rationale"] = ch.get("rationale", "") + f" [possible match: {other_label}]"
            new_changes.append(ch)
        result.append({**vd, "changes": new_changes})

    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def aggregate(view_diffs: list[dict]) -> list[dict]:
    """Deduplicate, sort, and soft-promote changes across all views.

    Args:
        view_diffs: The view_diffs list from a changeset dict — plain dicts in
                    the shape produced by viewdiff_to_dict().

    Returns:
        Transformed list[dict] with the same outer structure.
    """
    if not view_diffs:
        return []

    # Step 1: intra-view dedup.
    result = [{**vd, "changes": _dedup_within_view(vd.get("changes") or [])} for vd in view_diffs]

    # Step 2: cross-view dedup (matched views only).
    result = _cross_view_dedup(result)

    # Step 3: sort within each view.
    result = [{**vd, "changes": _sort_by_severity(vd.get("changes") or [])} for vd in result]

    # Step 4: UNCERTAIN* promotion.
    result = _promote_uncertain(result)

    return result
