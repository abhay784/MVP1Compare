"""Template Blueprint Extractor — optional Stage 0 of the DrawDiff pipeline.

When a user supplies a "change order" template PDF, this module sends the
rendered pages to Claude and parses the response into a `Blueprint` describing
which sections to emit, in what order, with which headings, column wording,
and severity labels. The report generator then mimics that template.

The module is best-effort: any failure (invalid PDF, malformed JSON, schema
mismatch) returns None with a log.warning, so the caller can fall back to the
default report layout.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from pathlib import Path
from typing import Optional

import anthropic
import fitz  # pymupdf
from PIL import Image
from pydantic import ValidationError

from api.models import Blueprint
from config import config

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extract_blueprint.txt"
_SYSTEM_PROMPT = _PROMPT_PATH.read_text()

_MAX_TEMPLATE_PAGES = 6  # cap to keep token + image cost bounded


def _render_template_pages(pdf_bytes: bytes) -> list[bytes]:
    """Render up to _MAX_TEMPLATE_PAGES of the template PDF as PNG bytes."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages: list[bytes] = []
    scale = config.render_dpi / 72.0
    mat = fitz.Matrix(scale, scale)
    for i in range(min(len(doc), _MAX_TEMPLATE_PAGES)):
        pix = doc[i].get_pixmap(matrix=mat, alpha=False)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        # Downscale to the comparator's max dim to keep payload small.
        max_dim = config.max_image_dimension
        if max(img.size) > max_dim:
            scale_down = max_dim / float(max(img.size))
            img = img.resize(
                (int(img.size[0] * scale_down), int(img.size[1] * scale_down)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pages.append(buf.getvalue())
    return pages


def _build_messages(page_pngs: list[bytes]) -> list[dict]:
    content: list[dict] = [
        {"type": "text", "text": "TEMPLATE DOCUMENT — extract the blueprint per the contract."},
    ]
    for i, png in enumerate(page_pngs):
        content.append({"type": "text", "text": f"Page {i + 1}:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.standard_b64encode(png).decode(),
            },
        })
    content.append({"type": "text", "text": "Return the JSON blueprint and nothing else."})
    return [{"role": "user", "content": content}]


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    return text


def extract_blueprint(pdf_bytes: bytes) -> Optional[Blueprint]:
    """Render the template PDF, ask Claude for a blueprint, validate and return.

    Returns None on any failure — the caller falls back to the default report
    layout. Failures are logged at WARNING level.
    """
    try:
        page_pngs = _render_template_pages(pdf_bytes)
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("Template render failed: %s — falling back to default layout", exc)
        return None

    if not page_pngs:
        log.warning("Template PDF has no pages — falling back to default layout")
        return None

    try:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)
        resp = client.messages.create(
            model=config.blueprint_model,
            max_tokens=8000,
            system=[{
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=_build_messages(page_pngs),
        )
        raw = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Blueprint LLM call failed: %s — falling back to default layout", exc)
        return None

    try:
        data = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        log.warning("Blueprint JSON parse failed: %s — falling back to default layout", exc)
        return None

    try:
        bp = Blueprint(**data)
    except ValidationError as exc:
        log.warning("Blueprint schema mismatch: %s — falling back to default layout", exc)
        return None

    # Minimum viability: the changes-table slot must be present, otherwise the
    # report would render without its primary payload.
    if "[[CHANGES]]" not in bp.html_shell:
        log.warning("Blueprint html_shell missing [[CHANGES]] token — falling back to default layout")
        return None

    return bp
