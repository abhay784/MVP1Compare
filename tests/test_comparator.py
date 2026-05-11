"""Tests for pipeline/comparator.py — Step 5: LLM View Comparator.

Anthropic calls are mocked via monkeypatch on `anthropic.Anthropic`; S3 downloads
are mocked via monkeypatch on `pipeline.comparator._download_png`. No network,
no AWS, no API key required.

    pytest tests/test_comparator.py -v
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from config import config
from pipeline import comparator
from pipeline.comparator import (
    Change,
    ViewDiff,
    _diff_revision_block,
    _diff_title_block,
    compare_views,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png_bytes(w: int = 200, h: int = 200, color: tuple = (255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _matched(label: str = "FRONT VIEW") -> dict:
    return {
        "orig_index": 0,
        "rev_index": 0,
        "label": label,
        "score": 0.95,
        "match_type": "matched",
        "orig_crop_s3_key": "crops/job/original/view_00.png",
        "rev_crop_s3_key":  "crops/job/revised/view_00.png",
    }


def _title_block_match() -> dict:
    return {
        "orig_index": None, "rev_index": None,
        "label": "title_block", "score": 1.0,
        "match_type": "title_block",
        "orig_crop_s3_key": None, "rev_crop_s3_key": None,
    }


def _revision_block_match() -> dict:
    return {
        "orig_index": None, "rev_index": None,
        "label": "revision_block", "score": 1.0,
        "match_type": "revision_block",
        "orig_crop_s3_key": None, "rev_crop_s3_key": None,
    }


class _FakeAnthropic:
    """Drop-in replacement for anthropic.Anthropic — records calls, returns canned JSON."""
    # One _FakeAnthropic instance per test, mutated via a module-level registry.
    calls: list[dict] = []
    responses: list[str] = []  # popped in order

    def __init__(self, *_, **__):
        pass

    class _Messages:
        @staticmethod
        def create(*, model, max_tokens, system, messages):
            _FakeAnthropic.calls.append({
                "model": model, "max_tokens": max_tokens,
                "system": system, "messages": messages,
            })
            if not _FakeAnthropic.responses:
                raise RuntimeError("FakeAnthropic: no canned response left")
            text = _FakeAnthropic.responses.pop(0)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=text)]
            )

    @property
    def messages(self):
        return self._Messages()


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeAnthropic.calls = []
    _FakeAnthropic.responses = []
    yield
    _FakeAnthropic.calls = []
    _FakeAnthropic.responses = []


@pytest.fixture
def patched_comparator(monkeypatch):
    """Patch Anthropic + S3 download on the comparator module."""
    monkeypatch.setattr(comparator.anthropic, "Anthropic", _FakeAnthropic)
    monkeypatch.setattr(comparator, "_download_png", lambda key: _png_bytes())
    return comparator


# ---------------------------------------------------------------------------
# Matched view: happy path
# ---------------------------------------------------------------------------

class TestMatchedHappyPath:
    def test_single_change_returned(self, patched_comparator):
        _FakeAnthropic.responses = [json.dumps({
            "changes": [{
                "field": "dimension",
                "orig_value": "Ø12.50",
                "revised_value": "Ø12.70",
                "severity": "MAJOR",
                "confidence": 0.9,
                "rationale": "bore diameter widened",
            }],
        })]
        diffs = compare_views("job", [_matched()], extraction={})
        assert len(diffs) == 1
        vd = diffs[0]
        assert vd.match_type == "matched"
        assert len(vd.changes) == 1
        c = vd.changes[0]
        assert c.field == "dimension"
        assert c.severity == "MAJOR"
        assert c.confidence == pytest.approx(0.9)

    def test_base_model_used_when_confident(self, patched_comparator):
        _FakeAnthropic.responses = [json.dumps({
            "changes": [{"field": "note", "orig_value": "A", "revised_value": "B",
                         "severity": "MINOR", "confidence": 0.95, "rationale": "x"}],
        })]
        compare_views("job", [_matched()], extraction={})
        assert len(_FakeAnthropic.calls) == 1
        assert _FakeAnthropic.calls[0]["model"] == config.base_model

    def test_system_prompt_cache_control(self, patched_comparator):
        _FakeAnthropic.responses = [json.dumps({"changes": []})]
        compare_views("job", [_matched()], extraction={})
        sys_block = _FakeAnthropic.calls[0]["system"][0]
        assert sys_block["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# Empty-changes response
# ---------------------------------------------------------------------------

class TestEmptyChanges:
    def test_empty_changes_yields_empty_viewdiff(self, patched_comparator):
        _FakeAnthropic.responses = [json.dumps({"changes": []})]
        diffs = compare_views("job", [_matched()], extraction={})
        assert len(diffs) == 1
        assert diffs[0].changes == []


# ---------------------------------------------------------------------------
# Escalation + UNCERTAIN rewrite
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_low_confidence_triggers_escalation(self, patched_comparator):
        low = json.dumps({"changes": [{
            "field": "dimension", "orig_value": "A", "revised_value": "B",
            "severity": "MAJOR", "confidence": 0.3, "rationale": "unclear",
        }]})
        # Escalation also low → severity gets rewritten to UNCERTAIN
        _FakeAnthropic.responses = [low, low]

        diffs = compare_views("job", [_matched()], extraction={})
        assert len(_FakeAnthropic.calls) == 2
        assert _FakeAnthropic.calls[0]["model"] == config.base_model
        assert _FakeAnthropic.calls[1]["model"] == config.escalation_model

        assert diffs[0].changes[0].severity == "UNCERTAIN"

    def test_escalation_confident_keeps_original_severity(self, patched_comparator):
        low  = json.dumps({"changes": [{"field": "dim", "orig_value": "A",
                                        "revised_value": "B", "severity": "MAJOR",
                                        "confidence": 0.3, "rationale": ""}]})
        high = json.dumps({"changes": [{"field": "dim", "orig_value": "A",
                                        "revised_value": "B", "severity": "MAJOR",
                                        "confidence": 0.9, "rationale": ""}]})
        _FakeAnthropic.responses = [low, high]

        diffs = compare_views("job", [_matched()], extraction={})
        assert len(_FakeAnthropic.calls) == 2  # escalated
        assert diffs[0].changes[0].severity == "MAJOR"  # not rewritten
        assert diffs[0].changes[0].confidence == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# added / removed — no API call
# ---------------------------------------------------------------------------

class TestAddedRemoved:
    def test_added_view_no_api_call(self, patched_comparator):
        match = {
            "orig_index": None, "rev_index": 3,
            "label": "NEW SECTION", "score": 0.0,
            "match_type": "added",
            "orig_crop_s3_key": None,
            "rev_crop_s3_key":  "crops/job/revised/view_03.png",
        }
        diffs = compare_views("job", [match], extraction={})
        assert _FakeAnthropic.calls == []
        assert len(diffs[0].changes) == 1
        c = diffs[0].changes[0]
        assert c.severity == "MAJOR"
        assert c.orig_value is None
        assert c.revised_value == "NEW SECTION"

    def test_removed_view_no_api_call(self, patched_comparator):
        match = {
            "orig_index": 2, "rev_index": None,
            "label": "OLD VIEW", "score": 0.0,
            "match_type": "removed",
            "orig_crop_s3_key": "crops/job/original/view_02.png",
            "rev_crop_s3_key":  None,
        }
        diffs = compare_views("job", [match], extraction={})
        assert _FakeAnthropic.calls == []
        assert len(diffs[0].changes) == 1
        c = diffs[0].changes[0]
        assert c.severity == "MAJOR"
        assert c.orig_value == "OLD VIEW"
        assert c.revised_value is None


# ---------------------------------------------------------------------------
# Structured block diffs — no API call
# ---------------------------------------------------------------------------

class TestTitleBlockDiff:
    def test_title_block_diff_emits_change_per_field(self, patched_comparator):
        extraction = {
            "original": {"title_block": {
                "part_number": "P-001", "revision": "A", "material": "STEEL",
                "tolerance": "±0.1", "drawn_by": "JD", "date": "2026-01-01",
            }},
            "revised":  {"title_block": {
                "part_number": "P-001", "revision": "B", "material": "ALUMINUM",
                "tolerance": "±0.1", "drawn_by": "JD", "date": "2026-01-01",
            }},
        }
        diffs = compare_views("job", [_title_block_match()], extraction=extraction)
        assert _FakeAnthropic.calls == []
        changed_fields = {c.field for c in diffs[0].changes}
        assert changed_fields == {"revision", "material"}

    def test_title_block_severity_mapping(self):
        orig = {"part_number": "P-001", "revision": "A"}
        rev  = {"part_number": "P-002", "revision": "A"}
        changes = _diff_title_block(orig, rev)
        assert len(changes) == 1
        assert changes[0].field == "part_number"
        assert changes[0].severity == "CRITICAL"

    def test_title_block_no_diff(self):
        same = {"part_number": "P-001", "revision": "A", "material": "STEEL"}
        assert _diff_title_block(same, same) == []


class TestRevisionBlockDiff:
    def test_revision_block_added_entry(self, patched_comparator):
        extraction = {
            "original": {"revision_block": {"entries": [{"rev": "A", "desc": "init"}]}},
            "revised":  {"revision_block": {"entries": [
                {"rev": "A", "desc": "init"},
                {"rev": "B", "desc": "bore change"},
            ]}},
        }
        diffs = compare_views("job", [_revision_block_match()], extraction=extraction)
        assert _FakeAnthropic.calls == []
        assert len(diffs[0].changes) == 1
        c = diffs[0].changes[0]
        assert c.severity == "MAJOR"
        assert c.orig_value is None
        assert "B" in c.revised_value

    def test_revision_block_unchanged(self):
        same = {"entries": [{"rev": "A"}, {"rev": "B"}]}
        assert _diff_revision_block(same, same) == []


# ---------------------------------------------------------------------------
# Image downscale
# ---------------------------------------------------------------------------

class TestDownscale:
    def test_oversize_image_is_downscaled(self, monkeypatch):
        big = _png_bytes(w=config.max_image_dimension + 1000, h=1000)
        monkeypatch.setattr(comparator.anthropic, "Anthropic", _FakeAnthropic)
        monkeypatch.setattr(comparator, "_download_png", lambda key: big)

        _FakeAnthropic.responses = [json.dumps({"changes": []})]
        compare_views("job", [_matched()], extraction={})

        # The image block is the second content item (idx 1); decode and measure.
        import base64
        user_content = _FakeAnthropic.calls[0]["messages"][0]["content"]
        img_block = next(b for b in user_content if b.get("type") == "image")
        raw = base64.standard_b64decode(img_block["source"]["data"])
        img = Image.open(io.BytesIO(raw))
        assert max(img.size) <= config.max_image_dimension

    def test_small_image_passes_through(self, monkeypatch):
        small = _png_bytes(w=500, h=500)
        monkeypatch.setattr(comparator.anthropic, "Anthropic", _FakeAnthropic)
        monkeypatch.setattr(comparator, "_download_png", lambda key: small)

        _FakeAnthropic.responses = [json.dumps({"changes": []})]
        compare_views("job", [_matched()], extraction={})

        import base64
        user_content = _FakeAnthropic.calls[0]["messages"][0]["content"]
        img_block = next(b for b in user_content if b.get("type") == "image")
        raw = base64.standard_b64decode(img_block["source"]["data"])
        img = Image.open(io.BytesIO(raw))
        assert img.size == (500, 500)


# ---------------------------------------------------------------------------
# Rationale normaliser
# ---------------------------------------------------------------------------

class TestRationaleNormalisation:
    def test_multi_sentence_truncated_to_first(self):
        from pipeline.comparator import _normalise_rationale
        out = _normalise_rationale(
            "Hole diameter increased from Ø10 to Ø12. This affects the fit."
        )
        assert out == "Hole diameter increased from Ø10 to Ø12."

    def test_whitespace_collapsed(self):
        from pipeline.comparator import _normalise_rationale
        assert _normalise_rationale("  multi   space   no   period ") == "multi space no period."

    def test_empty_and_none(self):
        from pipeline.comparator import _normalise_rationale
        assert _normalise_rationale("") == ""
        assert _normalise_rationale(None) == ""

    def test_parse_llm_json_truncates_rationale(self, patched_comparator):
        _FakeAnthropic.responses = [json.dumps({
            "changes": [{
                "field": "dimension",
                "orig_value": "X",
                "revised_value": "Y",
                "severity": "MAJOR",
                "confidence": 0.9,
                "rationale": "First sentence. Second sentence which we do not want.",
            }],
        })]
        diffs = compare_views("job", [_matched()], extraction={})
        assert diffs[0].changes[0].rationale == "First sentence."
