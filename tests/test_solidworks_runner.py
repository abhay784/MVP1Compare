"""Tests for `pipeline.solidworks_runner` — uses a mocked HTTP client.

We stub `report.generator` in sys.modules before importing the runner so the
tests don't require weasyprint / cairo / pango to be installed locally.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# --- Stub report.generator so the runner's lazy import resolves to a fake. ---
if "report.generator" not in sys.modules:
    fake_report = types.ModuleType("report.generator")

    class _Sum:
        critical = 0
        significant = 0
        minor = 0
        uncertain = 0

        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    def _count_summary(view_diffs):
        return _Sum(critical=0, significant=0, minor=0, uncertain=0)

    def _generate_report(**kwargs):
        return b"%PDF-stub"

    fake_report.count_summary = _count_summary
    fake_report.generate_report = _generate_report
    if "report" not in sys.modules:
        sys.modules["report"] = types.ModuleType("report")
    sys.modules["report.generator"] = fake_report

# --- Stub pipeline.local_runner so patching it doesn't drag in anthropic. ---
if "pipeline.local_runner" not in sys.modules:
    import pipeline as _pipeline_pkg
    fake_local = types.ModuleType("pipeline.local_runner")
    fake_local.run_comparison_local = lambda **kw: {}
    sys.modules["pipeline.local_runner"] = fake_local
    _pipeline_pkg.local_runner = fake_local

from pipeline import solidworks_runner  # noqa: E402


_FIXTURE = Path(__file__).parent / "fixtures" / "sw_changeset.json"


@pytest.fixture
def sw_response() -> dict:
    return json.loads(_FIXTURE.read_text())


def test_runner_part_path_writes_changeset(tmp_path, sw_response):
    job_dir = tmp_path / "job"
    with patch.object(
        solidworks_runner, "fetch_solidworks_changeset", return_value=sw_response,
    ):
        result = solidworks_runner.run_comparison_solidworks(
            job_id="test-job",
            original_path=Path("C:\\drawings\\RevA.SLDPRT"),
            revised_path=Path("C:\\drawings\\RevB.SLDPRT"),
            part_number="P-001",
            notes="",
            job_dir=job_dir,
        )

    assert result["report_path"].exists()
    assert result["changeset_path"].exists()
    assert result["highlighted_pages"] == {"original": [], "revised": []}

    cs = json.loads(result["changeset_path"].read_text())
    assert cs["source"] == "solidworks"
    assert cs["file_kind"] == "part"
    assert len(cs["view_diffs"]) == 1
    geom = cs["view_diffs"][0]
    assert geom["match_type"] == "geometry_block"
    # Six input changes should round-trip after aggregation (no duplicates here)
    assert len(geom["changes"]) == 6


def test_runner_part_path_does_not_invoke_vision_pipeline(tmp_path, sw_response):
    job_dir = tmp_path / "job"
    with patch.object(
        solidworks_runner, "fetch_solidworks_changeset", return_value=sw_response,
    ), patch("pipeline.local_runner.run_comparison_local") as vision_stub:
        solidworks_runner.run_comparison_solidworks(
            job_id="test-job",
            original_path=Path("C:\\drawings\\RevA.SLDPRT"),
            revised_path=Path("C:\\drawings\\RevB.SLDPRT"),
            part_number="P",
            notes="",
            job_dir=job_dir,
        )
        vision_stub.assert_not_called()


def test_runner_severities_match_rules(tmp_path, sw_response):
    job_dir = tmp_path / "job"
    with patch.object(
        solidworks_runner, "fetch_solidworks_changeset", return_value=sw_response,
    ):
        result = solidworks_runner.run_comparison_solidworks(
            job_id="test-job",
            original_path=Path("C:\\a.SLDPRT"),
            revised_path=Path("C:\\b.SLDPRT"),
            part_number="P",
            notes="",
            job_dir=job_dir,
        )
    cs = json.loads(result["changeset_path"].read_text())
    by_field = {c["field"]: c for c in cs["view_diffs"][0]["changes"]}

    assert by_field["material"]["severity"] == "CRITICAL"
    # The fixture has two dimensions — find by parameter name
    dims = [c for c in cs["view_diffs"][0]["changes"] if (c.get("solidworks") or {}).get("type") == "dimension"]
    assert any(c["severity"] == "CRITICAL" for c in dims)   # 20→25
    assert any(c["severity"] == "MINOR" for c in dims)       # 10→10.05
    # All conf=1.0
    for c in cs["view_diffs"][0]["changes"]:
        assert c["confidence"] == 1.0


def test_runner_drawing_hybrid_merges_with_vision(tmp_path, sw_response, monkeypatch):
    # Promote the fixture to a drawing-kind response with stub PDFs
    import base64
    drawing_resp = dict(sw_response)
    drawing_resp["file_kind"] = "drawing"
    drawing_resp["exported_pdfs"] = {
        "old_b64": base64.b64encode(b"%PDF-fake-old").decode(),
        "new_b64": base64.b64encode(b"%PDF-fake-new").decode(),
    }

    # Stub the vision pipeline: it must produce a changeset.json file the
    # hybrid path will read back and merge.
    def fake_vision(*, job_id, original_pdf, revised_pdf, part_number, notes, job_dir):
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        changeset_path = job_dir / "changeset.json"
        report_path = job_dir / "report.pdf"
        changeset_path.write_text(json.dumps({
            "job_id": job_id,
            "view_diffs": [
                {
                    "label": "SECTION A-A",
                    "match_type": "matched",
                    "changes": [
                        {
                            "field": "annotation",
                            "orig_value": "REF",
                            "revised_value": "TYP",
                            "severity": "MINOR",
                            "confidence": 0.9,
                            "rationale": "annotation text changed",
                            "orig_bbox": None,
                            "rev_bbox": None,
                        }
                    ],
                    "orig_index": 0,
                    "rev_index": 0,
                }
            ],
        }))
        report_path.write_bytes(b"%PDF-fake-vision-report")
        return {
            "job_dir":         job_dir,
            "report_path":     report_path,
            "changeset_path":  changeset_path,
            "highlighted_pages": {"original": [], "revised": []},
            "summary":         None,
        }

    monkeypatch.setattr("pipeline.local_runner.run_comparison_local", fake_vision)

    job_dir = tmp_path / "job"
    with patch.object(
        solidworks_runner, "fetch_solidworks_changeset", return_value=drawing_resp,
    ):
        result = solidworks_runner.run_comparison_solidworks(
            job_id="test-job",
            original_path=Path("C:\\a.SLDDRW"),
            revised_path=Path("C:\\b.SLDDRW"),
            part_number="P",
            notes="",
            job_dir=job_dir,
        )

    cs = json.loads(result["changeset_path"].read_text())
    match_types = {vd["match_type"] for vd in cs["view_diffs"]}
    assert "geometry_block" in match_types       # SW geometry pass
    assert "matched" in match_types              # vision pass
    # Vision sub-report path is exposed for callers that want it
    assert result["vision_subreport"] is not None
