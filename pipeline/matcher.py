"""View matcher — Step 4 of the DrawDiff pipeline.

Matches original-vs-revised views by label using rapidfuzz, with special
handling for title_block and revision_block which always pair regardless of score.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

from config import config


@dataclass
class ViewMatch:
    orig_index: int | None   # None for ADDED views
    rev_index: int | None    # None for REMOVED views
    label: str
    score: float             # 0.0–1.0; 1.0 for special blocks
    match_type: str          # "matched" | "added" | "removed" | "title_block" | "revision_block"


def match_views(
    orig_views: list[dict[str, Any]],
    rev_views: list[dict[str, Any]],
) -> list[ViewMatch]:
    """Match original and revised view dicts by label.

    Args:
        orig_views: List of view metadata dicts from extraction.json (original side).
        rev_views:  List of view metadata dicts from extraction.json (revised side).

    Returns:
        List of ViewMatch objects covering all views plus the two special blocks.
        Special blocks (title_block, revision_block) are always present with score=1.0.
        Regular views score > threshold → "matched"; remainder → "removed" or "added".
    """
    results: list[ViewMatch] = []

    # Special structural blocks always match unconditionally.
    results.append(ViewMatch(orig_index=None, rev_index=None,
                             label="title_block", score=1.0, match_type="title_block"))
    results.append(ViewMatch(orig_index=None, rev_index=None,
                             label="revision_block", score=1.0, match_type="revision_block"))

    if not orig_views and not rev_views:
        return results

    # rapidfuzz.fuzz.ratio returns 0–100; config threshold is 0–1.
    threshold = config.view_match_threshold * 100

    candidates: list[tuple[float, int, int]] = []
    for i, ov in enumerate(orig_views):
        orig_label = (ov.get("view_label") or "").upper().strip()
        for j, rv in enumerate(rev_views):
            rev_label = (rv.get("view_label") or "").upper().strip()
            score = fuzz.ratio(orig_label, rev_label)
            if score >= threshold:
                candidates.append((score, i, j))

    # Greedy assignment: highest score first, each view used at most once.
    candidates.sort(key=lambda x: -x[0])
    matched_orig: set[int] = set()
    matched_rev: set[int] = set()

    for score, i, j in candidates:
        if i in matched_orig or j in matched_rev:
            continue
        matched_orig.add(i)
        matched_rev.add(j)
        label = orig_views[i].get("view_label") or f"view_{i:02d}"
        results.append(ViewMatch(
            orig_index=orig_views[i]["index"],
            rev_index=rev_views[j]["index"],
            label=label,
            score=round(score / 100, 4),
            match_type="matched",
        ))

    for i, ov in enumerate(orig_views):
        if i not in matched_orig:
            results.append(ViewMatch(
                orig_index=ov["index"],
                rev_index=None,
                label=ov.get("view_label") or f"view_{i:02d}",
                score=0.0,
                match_type="removed",
            ))

    for j, rv in enumerate(rev_views):
        if j not in matched_rev:
            results.append(ViewMatch(
                orig_index=None,
                rev_index=rv["index"],
                label=rv.get("view_label") or f"view_{j:02d}",
                score=0.0,
                match_type="added",
            ))

    return results
