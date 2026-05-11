"""Tests for pipeline/template_blueprint.py.

The Anthropic call is patched; PDF rendering uses a real minimal PDF via PyMuPDF
to exercise _render_template_pages without requiring a fixture file.
"""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import fitz
import pytest

from pipeline import template_blueprint


def _minimal_pdf_bytes() -> bytes:
    """Generate a single-page PDF with a tiny bit of text via PyMuPDF."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "ECO Template")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class _FakeContent:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeContent(text)]


class _FakeAnthropic:
    """Captures the create() args and returns a canned response."""
    last_kwargs: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        _FakeAnthropic.last_kwargs = kwargs
        return _FakeResponse(_FakeAnthropic.next_response)


def _install_fake(monkeypatch, response_text: str) -> None:
    _FakeAnthropic.next_response = response_text
    monkeypatch.setattr(template_blueprint.anthropic, "Anthropic", _FakeAnthropic)


_VALID_SHELL = (
    "<!DOCTYPE html><html><body>"
    "<h1>ECO</h1>[[SUMMARY]][[TITLE_BLOCK]][[CHANGES]]"
    "</body></html>"
)


def test_extract_blueprint_happy_path(monkeypatch):
    payload = {
        "html_shell": _VALID_SHELL,
        "change_table": {
            "columns": [
                {"header": "Class", "role": "severity"},
                {"header": "Item", "role": "field"},
                {"header": "Was", "role": "orig_value"},
                {"header": "Now", "role": "revised_value"},
                {"header": "Description", "role": "description"},
            ],
            "has_zone_column": False,
        },
        "severity_labels": {"CRITICAL": "A", "MAJOR": "B", "MINOR": "C", "UNCERTAIN": "Review"},
    }
    _install_fake(monkeypatch, json.dumps(payload))
    bp = template_blueprint.extract_blueprint(_minimal_pdf_bytes())
    assert bp is not None
    assert "[[CHANGES]]" in bp.html_shell
    assert bp.severity_labels["CRITICAL"] == "A"
    assert len(bp.change_table.columns) == 5
    assert bp.change_table.has_zone_column is False


def test_extract_blueprint_strips_markdown_fence(monkeypatch):
    payload = {"html_shell": _VALID_SHELL}
    _install_fake(monkeypatch, "```json\n" + json.dumps(payload) + "\n```")
    bp = template_blueprint.extract_blueprint(_minimal_pdf_bytes())
    assert bp is not None
    assert "[[CHANGES]]" in bp.html_shell


def test_extract_blueprint_malformed_json_returns_none(monkeypatch, caplog):
    _install_fake(monkeypatch, "this is not json")
    bp = template_blueprint.extract_blueprint(_minimal_pdf_bytes())
    assert bp is None


def test_extract_blueprint_missing_changes_token_returns_none(monkeypatch):
    """A shell that lacks [[CHANGES]] is rejected so callers fall back to default."""
    payload = {"html_shell": "<!DOCTYPE html><html><body>[[SUMMARY]]</body></html>"}
    _install_fake(monkeypatch, json.dumps(payload))
    bp = template_blueprint.extract_blueprint(_minimal_pdf_bytes())
    assert bp is None


def test_extract_blueprint_schema_mismatch_returns_none(monkeypatch):
    # `html_shell` is required; here we omit it.
    bad = {"change_table": {"columns": [], "has_zone_column": False}}
    _install_fake(monkeypatch, json.dumps(bad))
    bp = template_blueprint.extract_blueprint(_minimal_pdf_bytes())
    assert bp is None


def test_extract_blueprint_invalid_pdf_returns_none():
    bp = template_blueprint.extract_blueprint(b"not a pdf")
    assert bp is None


def test_extract_blueprint_passes_cache_control(monkeypatch):
    payload = {"html_shell": _VALID_SHELL}
    _install_fake(monkeypatch, json.dumps(payload))
    bp = template_blueprint.extract_blueprint(_minimal_pdf_bytes())
    assert bp is not None
    sys_block = _FakeAnthropic.last_kwargs["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral"}
