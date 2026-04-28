"""LLM View Comparator — Step 5 of the DrawDiff pipeline.

Takes the match manifest from Step 4 and produces a list of ViewDiff objects
describing every engineering-meaningful difference between the original and
revised drawings. Uses Anthropic's vision API with prompt caching, and
escalates low-confidence calls from the base model to the escalation model.

The output (list[ViewDiff]) is the canonical changeset consumed by Step 6
(annotator + report) — each ViewDiff carries a per-view list of Changes with
severity badges the report can render directly.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import boto3
from PIL import Image

from config import config

log = logging.getLogger(__name__)

# System prompt is loaded once at module import and reused for every call.
# It is sent with cache_control: ephemeral so the Anthropic prompt cache can
# deduplicate it across the ~N view comparisons in a single job.
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "compare_view.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text()


# ---------------------------------------------------------------------------
# Public data contract — consumed by Step 6 (annotator + report)
# ---------------------------------------------------------------------------

@dataclass
class Change:
    """One engineering-meaningful difference between original and revised."""
    field: str
    orig_value: str | None
    revised_value: str | None
    severity: str           # "CRITICAL" | "MAJOR" | "MINOR" | "UNCERTAIN"
    confidence: float       # 0.0–1.0
    rationale: str = ""


@dataclass
class ViewDiff:
    """All changes detected within a single matched view (or block)."""
    label: str
    match_type: str         # "matched" | "added" | "removed" | "title_block" | "revision_block"
    changes: list[Change] = field(default_factory=list)


# ---------------------------------------------------------------------------
# S3 + image helpers
# ---------------------------------------------------------------------------

def _s3_client():
    return boto3.client(
        "s3",
        aws_access_key_id=config.aws_access_key_id,
        aws_secret_access_key=config.aws_secret_access_key,
        region_name=config.aws_region,
    )


def _download_png(key: str) -> bytes:
    obj = _s3_client().get_object(Bucket=config.s3_bucket, Key=key)
    return obj["Body"].read()


def _downscale_if_needed(png_bytes: bytes, max_dim: int) -> bytes:
    """Return PNG bytes whose largest side is ≤ max_dim (aspect preserved)."""
    img = Image.open(io.BytesIO(png_bytes))
    w, h = img.size
    if max(w, h) <= max_dim:
        return png_bytes
    scale = max_dim / float(max(w, h))
    new_size = (int(w * scale), int(h * scale))
    resized = img.resize(new_size, Image.LANCZOS)
    out = io.BytesIO()
    resized.save(out, format="PNG")
    log.info("Downscaled image from %sx%s → %s", (w, h), (w, h), new_size)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Anthropic call — single view, single model
# ---------------------------------------------------------------------------

def _build_messages(orig_png: bytes, rev_png: bytes) -> list[dict]:
    """User-turn content: labelled pair of images for the comparator."""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": "ORIGINAL VIEW (prior revision):"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(orig_png).decode(),
                },
            },
            {"type": "text", "text": "REVISED VIEW (proposed revision):"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.standard_b64encode(rev_png).decode(),
                },
            },
            {"type": "text", "text": "Return JSON per the contract."},
        ],
    }]


def _call_vision(orig_png: bytes, rev_png: bytes, model: str) -> list[Change]:
    """Send one matched view to the given model and parse the JSON response."""
    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    resp = client.messages.create(
        model=model,
        max_tokens=config.max_tokens_per_view,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=_build_messages(orig_png, rev_png),
    )
    raw = "".join(
        block.text for block in resp.content if getattr(block, "type", None) == "text"
    )
    return _parse_llm_json(raw)


def _parse_llm_json(raw: str) -> list[Change]:
    """Parse JSON from model output, tolerating accidental markdown fences."""
    text = raw.strip()
    # Strip ```json ... ``` fences if the model slips.
    fence = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    data = json.loads(text)
    out: list[Change] = []
    for item in data.get("changes", []):
        out.append(Change(
            field=str(item.get("field", "other")),
            orig_value=item.get("orig_value"),
            revised_value=item.get("revised_value"),
            severity=str(item.get("severity", "MINOR")).upper(),
            confidence=float(item.get("confidence", 0.0)),
            rationale=str(item.get("rationale", "")),
        ))
    return out


# ---------------------------------------------------------------------------
# Structured-block diff (title_block / revision_block) — no vision call
# ---------------------------------------------------------------------------

# Severity for title_block field changes. These are engineering judgments:
# a material change is a real CRITICAL manufacturing signal, a drawn_by change
# is just documentation. This mapping is consulted by _diff_title_block below.
_TITLE_FIELD_SEVERITY: dict[str, str] = {
    "part_number": "CRITICAL",
    "revision":    "MAJOR",
    "material":    "CRITICAL",
    "tolerance":   "CRITICAL",
    "drawn_by":    "MINOR",
    "date":        "MINOR",
}


def _diff_title_block(orig: dict, rev: dict) -> list[Change]:
    """Emit one Change per differing field in the title_block dicts.

    We compare every field named in _TITLE_FIELD_SEVERITY (the structured fields
    Step 3 extracts). `raw_text` is deliberately ignored — it is the noisy OCR
    concatenation that the structured fields already distil.
    """
    changes: list[Change] = []
    for k, severity in _TITLE_FIELD_SEVERITY.items():
        ov = orig.get(k)
        rv = rev.get(k)
        if ov == rv:
            continue
        changes.append(Change(
            field=k,
            orig_value=None if ov is None else str(ov),
            revised_value=None if rv is None else str(rv),
            severity=severity,
            confidence=1.0,  # deterministic structural diff
            rationale=f"title_block.{k} changed from {ov!r} to {rv!r}",
        ))
    return changes


def _diff_revision_block(orig: dict, rev: dict) -> list[Change]:
    """Emit a Change per revision_block entry that was added or removed.

    revision_block.entries is a list of dicts (rev, description, date, …).
    We key entries by their `rev` field (or the full dict str if absent) and
    flag any entry present on one side but not the other.
    """
    def _key(entry: dict) -> str:
        return str(entry.get("rev") or entry.get("revision") or json.dumps(entry, sort_keys=True))

    orig_entries = {_key(e): e for e in (orig.get("entries") or [])}
    rev_entries  = {_key(e): e for e in (rev.get("entries")  or [])}

    changes: list[Change] = []
    for k in sorted(rev_entries.keys() - orig_entries.keys()):
        changes.append(Change(
            field="revision_entry",
            orig_value=None,
            revised_value=json.dumps(rev_entries[k], sort_keys=True),
            severity="MAJOR",
            confidence=1.0,
            rationale=f"revision_block gained entry {k!r}",
        ))
    for k in sorted(orig_entries.keys() - rev_entries.keys()):
        changes.append(Change(
            field="revision_entry",
            orig_value=json.dumps(orig_entries[k], sort_keys=True),
            revised_value=None,
            severity="MAJOR",
            confidence=1.0,
            rationale=f"revision_block lost entry {k!r}",
        ))
    return changes


# ---------------------------------------------------------------------------
# Per-match dispatch
# ---------------------------------------------------------------------------

def _compare_matched_view(match: dict) -> list[Change]:
    """Vision-path: download both crops, call base model, escalate if uncertain."""
    orig_png = _downscale_if_needed(
        _download_png(match["orig_crop_s3_key"]), config.max_image_dimension,
    )
    rev_png = _downscale_if_needed(
        _download_png(match["rev_crop_s3_key"]), config.max_image_dimension,
    )

    changes = _call_vision(orig_png, rev_png, config.base_model)

    # Escalation trigger: any change below the confidence floor.
    if changes and min(c.confidence for c in changes) < config.confidence_threshold:
        log.info(
            "View %r base-model confidence below %.2f — escalating to %s",
            match.get("label"), config.confidence_threshold, config.escalation_model,
        )
        changes = _call_vision(orig_png, rev_png, config.escalation_model)
        # After escalation, any residual low-confidence change is marked UNCERTAIN
        # so the report renderer can flag it for human review.
        for c in changes:
            if c.confidence < config.confidence_threshold:
                c.severity = "UNCERTAIN"

    return changes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compare_views(
    job_id: str,
    matches: list[dict],
    extraction: dict,
) -> list[ViewDiff]:
    """Produce a ViewDiff for every match in the manifest.

    Args:
        job_id:     Job identifier (used only for logging).
        matches:    List of match dicts (shape: matches.json from Step 4).
        extraction: Full extraction.json dict from Step 3 (has original/revised
                    title_block and revision_block under those keys).

    Returns:
        One ViewDiff per input match, in input order.
    """
    orig_tb = extraction.get("original",  {}).get("title_block",    {}) or {}
    rev_tb  = extraction.get("revised",   {}).get("title_block",    {}) or {}
    orig_rb = extraction.get("original",  {}).get("revision_block", {}) or {}
    rev_rb  = extraction.get("revised",   {}).get("revision_block", {}) or {}

    diffs: list[ViewDiff] = []
    for m in matches:
        mtype = m["match_type"]
        label = m.get("label", "")

        if mtype == "matched":
            changes = _compare_matched_view(m)

        elif mtype == "added":
            changes = [Change(
                field="view",
                orig_value=None,
                revised_value=label,
                severity="MAJOR",
                confidence=1.0,
                rationale=f"View {label!r} appears only in the revised drawing.",
            )]

        elif mtype == "removed":
            changes = [Change(
                field="view",
                orig_value=label,
                revised_value=None,
                severity="MAJOR",
                confidence=1.0,
                rationale=f"View {label!r} was present in the original but not the revised drawing.",
            )]

        elif mtype == "title_block":
            changes = _diff_title_block(orig_tb, rev_tb)

        elif mtype == "revision_block":
            changes = _diff_revision_block(orig_rb, rev_rb)

        else:
            log.warning("[%s] Unknown match_type %r — skipping", job_id, mtype)
            changes = []

        diffs.append(ViewDiff(label=label, match_type=mtype, changes=changes))

    log.info("[%s] Comparator produced %d view diff(s)", job_id, len(diffs))
    return diffs


def viewdiff_to_dict(vd: ViewDiff) -> dict[str, Any]:
    """Serialization helper used by run.py when writing changeset.json."""
    return asdict(vd)
