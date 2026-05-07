"""Synthesis post-pass — last-mile prose layer over the aggregated changeset.

Single LLM call per job. Input: deduped changeset + part_summary from the
page-graph pre-pass. Output: a 1-paragraph executive summary plus a one-line
plain-English impact note per finding.

The structured changeset rendering done by report/generator.py is unaffected;
this layer purely adds prose. If the LLM fails or no API key is configured,
the run continues with no synthesis (report renders the structured findings
exactly as before).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import anthropic

from config import config

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "synthesize.txt"


def _load_prompt() -> str:
    return _PROMPT_PATH.read_text()


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def synthesize_report_summary(
    *,
    job_id: str,
    changeset: dict[str, Any],
    part_summary: str | None,
    part_number: str | None,
    notes: str | None,
) -> dict[str, Any]:
    """Return a dict with `executive_summary` (str) and `impact_lines` (dict[finding_key, str]).

    On any failure (no API key, malformed JSON, network error) returns
    {"executive_summary": "", "impact_lines": {}} — never raises.
    """
    fallback = {"executive_summary": "", "impact_lines": {}}
    if not config.anthropic_api_key:
        log.info("[%s] Synthesis skipped: ANTHROPIC_API_KEY not set", job_id)
        return fallback

    # Compact view of changeset for the prompt: only the fields the synthesizer needs.
    findings_for_prompt: list[dict[str, Any]] = []
    for vd in changeset.get("view_diffs", []):
        for ch in vd.get("changes", []):
            findings_for_prompt.append({
                "key":          _finding_key(vd.get("label"), ch),
                "view":         vd.get("label"),
                "match_type":   vd.get("match_type"),
                "field":        ch.get("field"),
                "orig_value":   ch.get("orig_value"),
                "revised_value": ch.get("revised_value"),
                "severity":     ch.get("severity"),
                "rationale":    ch.get("rationale"),
                "confirmed_by": ch.get("confirmed_by") or [],
            })

    if not findings_for_prompt:
        return {"executive_summary": "No engineering-meaningful changes detected.", "impact_lines": {}}

    user_text = (
        "PART SUMMARY (from page-graph pre-pass):\n"
        + (part_summary or "(none)")
        + "\n\nPART NUMBER: " + (part_number or "(unspecified)")
        + "\nNOTES: " + (notes or "(none)")
        + "\n\nFINDINGS (already deduplicated):\n"
        + json.dumps(findings_for_prompt, indent=2)
        + "\n\nReturn JSON per the contract."
    )

    try:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        resp = client.messages.create(
            model=config.synthesis_model,
            max_tokens=config.max_synthesis_tokens,
            temperature=config.temperature,
            system=[{"type": "text", "text": _load_prompt(),
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": [{"type": "text", "text": user_text}]}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        data = json.loads(_strip_fence(raw))
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] Synthesis call failed (%s) — continuing without prose", job_id, e)
        return fallback

    out = {
        "executive_summary": str(data.get("executive_summary") or ""),
        "impact_lines":      {str(k): str(v) for k, v in (data.get("impact_lines") or {}).items()},
    }
    log.info("[%s] Synthesis produced %d impact lines", job_id, len(out["impact_lines"]))
    return out


def _finding_key(view_label: str | None, change: dict[str, Any]) -> str:
    """Stable key used to thread synthesis impact lines back onto findings."""
    return f"{view_label or '?'}::{change.get('field')}::{change.get('orig_value')}::{change.get('revised_value')}"


def attach_impact_lines(changeset: dict[str, Any], synthesis: dict[str, Any]) -> dict[str, Any]:
    """Mutate changeset's view_diffs to add `impact` strings on each change.

    Returns the same changeset dict (mutated in place) for caller convenience.
    """
    impact_lines = synthesis.get("impact_lines") or {}
    if not impact_lines:
        return changeset
    for vd in changeset.get("view_diffs", []):
        for ch in vd.get("changes", []):
            key = _finding_key(vd.get("label"), ch)
            line = impact_lines.get(key)
            if line:
                ch["impact"] = line
    changeset["executive_summary"] = synthesis.get("executive_summary") or ""
    return changeset
