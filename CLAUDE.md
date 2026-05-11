# CLAUDE.md — DrawDiff Codebase Guide

This file gives an AI assistant the context needed to work effectively in this repo.

---

## What this project is

**DrawDiff** compares two PDF revisions of a mechanical engineering drawing and produces:
- A **PDF report** with a severity summary, side-by-side view strips, and per-change tables
- A **changeset JSON** with every detected change (field, old/new value, severity, confidence, rationale)
- **Highlighted PNG overlays** on each full page showing changed views in severity colours

---

## Architecture

There are two execution modes that share the same pipeline logic:

### CLI / Local mode (`cli.py` → `pipeline/local_runner.py`)
No Redis, no S3, no FastAPI. Reads two local PDFs, runs the full pipeline, writes all artifacts under `./out/<job-id>/`. Best for development and testing.

### API / Cloud mode (`api/` + Redis/RQ → `pipeline/run.py`)
FastAPI accepts two file uploads → stages them to S3 → enqueues an RQ job → worker runs `pipeline.run.run_comparison`. Status is tracked in Redis. Artifacts land in S3; presigned URLs returned to the client.

The two orchestrators (`run.py` vs `local_runner.py`) are intentional mirrors. `local_runner.py` monkeypatches `pipeline.comparator._download_png` to read from disk instead of S3 — this is the only S3 coupling in the comparator.

---

## Pipeline stages (in order)

| Stage | Module | Entry point | Description |
|---|---|---|---|
| 0 (optional) | `pipeline/template_blueprint.py` | `extract_blueprint(template_pdf_bytes)` | Only runs when the user supplies a `--template` PDF (CLI) or `template` upload (API). Renders the template pages and asks Claude (`prompts/extract_blueprint.txt`) to return a complete `html_shell` HTML document that mimics the template visually, with `[[TOKEN]]` placeholders where dynamic content goes ([[TITLE]], [[PART_NUMBER]], [[JOB_ID]], [[GENERATED_AT]], [[NOTES]], [[SUMMARY]], [[TITLE_BLOCK]], [[REVISION_BLOCK]], [[DRAWINGS]], [[CHANGES]]). The response also describes the change table's columns (header + semantic role) and severity labels. Validated against `api.models.Blueprint`. Any failure (or missing `[[CHANGES]]` token) returns `None` and `generate_report` falls back to the default Jinja layout. Blueprint is persisted to `metadata/<job_id>/blueprint.json`. |
| 1 | `pipeline/parser.py` | `parse_pdf(pdf_bytes, page_index)` | Renders page at 300 DPI (PyMuPDF), extracts word-level text blocks. Sets `is_raster=True` when zero text blocks found (scanned PDF). |
| 2a | `pipeline/llm_segmenter.py` | `segment_and_match(orig_pages, rev_pages)` | **`lseg` branch path.** Single Claude vision call per page-pair. Returns `LLMMatch` list with deliberately generous, often-overlapping normalized bboxes per side, plus a match decision (`matched`/`added`/`removed`). Replaces the geometry segmentor + rapidfuzz matcher. |
| 2b | `pipeline/view_isolator.py` | `isolate_match_crops(matches, pages, page_dims_pt, pdf_bytes, side)` | Crop-time foreign-content erasure. For each view bbox, white-outs the pixel rectangle of every text token / vector drawing primitive owned by another view (greatest-intersection ownership; reuses `bbox_snap._assign_ownership`). The bbox itself is **never moved**. Active when `config.use_view_isolator=True` (default). |
| 2c (legacy) | `pipeline/segmentor.py` | `segment_views(page, pdf_bytes, page_index)` | Geometry-based view detection (PyMuPDF rectangles ≥5% page area, aspect 0.2–5.0; or OpenCV morphological lines on raster). Unused on the `lseg` branch but kept available. |
| 2d (legacy) | `pipeline/bbox_snap.py` | `snap_view_bboxes_to_text(view_bboxes, text_bboxes)` | Edge-snap pass: expands bboxes to contain owned text, then pulls edges in to push out foreign text. Active when `config.use_view_isolator=False`. Its `_assign_ownership` helper is reused by `view_isolator`. |
| 3 | `pipeline/extractor.py` | `extract_dimensions`, `extract_title_block`, `extract_revision_block` | Regex-based dimension extraction from text blocks. Title/revision block heuristics from PyMuPDF. |
| 4 | `pipeline/matcher.py` | `match_views(orig_views, rev_views)` | rapidfuzz label matching (threshold 0.80). **Unused on `lseg` branch** — matching is done by the LLM in stage 2a. Still used in `pipeline/run.py`. |
| 5 | `pipeline/comparator.py` | `compare_views(job_id, matches, extraction)` | Claude vision calls per matched view pair. Base model first; if any change confidence < 0.65, escalates to escalation model. Low-confidence changes after escalation → severity `UNCERTAIN`. Title/revision block diffs are deterministic (no LLM). Added/removed views get a single synthetic `MAJOR` change. |
| 6 | `pipeline/aggregator.py` | `aggregate(view_diffs)` | Four passes: (1) intra-view dedup by field (highest confidence wins), (2) cross-view dedup of identical `(field, orig, revised)` triples, (3) severity sort within each view, (4) UNCERTAIN→UNCERTAIN* soft-promotion when the same field is CRITICAL/MAJOR with conf≥0.80 in another view. |
| 7 | `pipeline/annotator.py` | `build_side_by_side(orig_png, rev_png, label)` | Pillow: produces side-by-side PNG strip per view for the PDF. Handles one-sided (added/removed) views with placeholder. |
| 8 | `pipeline/highlighter.py` | `highlight_page(page_png, view_diffs, views, side)` | Pillow: draws severity-coloured boxes on full-page renders. Uses rapidfuzz to match change text against extracted dimensions; draws tighter ellipses when a dimension is identified. |
| 9 | `report/generator.py` | `generate_report(...)` | Default path: Jinja2 (`report.html.j2`) → WeasyPrint PDF. `count_summary()` builds the `ChangeSummary` counts. When `blueprint` is passed, `_render_blueprint_report` substitutes each `[[TOKEN]]` in `blueprint["html_shell"]` with an HTML fragment generated from the comparison data, then hands the result to WeasyPrint — the template's own HTML/CSS is preserved verbatim, so the final PDF mimics the customer's template practically 1:1. |

---

## Key data contracts

### `ParsedPage` (parser → segmentor, extractor)
```python
image: np.ndarray          # RGB, shape (h, w, 3), at render_dpi
text_blocks: list[TextBlock]  # word-level, bbox in PDF points
page_width_pt / page_height_pt: float
is_raster: bool            # True when len(text_blocks) == 0
```

### `ViewCrop` (segmentor → orchestrator)
```python
image: np.ndarray          # RGB crop
bbox_x0/y0/x1/y1: float   # in PDF points
view_label: str            # e.g. "SECTION A-A", or "VIEW_1" if not found
area_fraction: float       # fraction of page area
page_index: int
```

### `view_metadata` dict (orchestrator → matcher, comparator)
```python
{
  "index": int,
  "page_index": int,
  "view_label": str,
  "area_fraction": float,
  "bbox": [x0, y0, x1, y1],          # PDF points
  "crop_s3_key": str,                  # path to PNG (S3 key or local path)
  "dimensions": [{"value", "numeric", "bbox"}, ...]
}
```

### `match_manifest` dict (matcher → comparator, annotator, highlighter)
```python
{
  "orig_index": int | None,
  "rev_index": int | None,
  "label": str,
  "score": float,
  "match_type": "matched" | "added" | "removed" | "title_block" | "revision_block",
  "orig_crop_s3_key": str | None,
  "rev_crop_s3_key": str | None,
}
```

### `ViewDiff` / `Change` (comparator → aggregator → annotator, report)
```python
@dataclass
class Change:
    field: str               # "dimension" | "tolerance" | "note" | "feature" | "geometry" |
                             # "annotation" | "callout" | "material" | "finish" | "hole" |
                             # "thread" | "other" | "view" | "revision_entry"
    orig_value: str | None
    revised_value: str | None
    severity: str            # "CRITICAL" | "MAJOR" | "MINOR" | "UNCERTAIN" | "UNCERTAIN*"
    confidence: float        # 0.0–1.0
    rationale: str

@dataclass
class ViewDiff:
    label: str
    match_type: str
    changes: list[Change]
```

The aggregator works on plain `dict` representations (via `viewdiff_to_dict()`), not on the dataclass instances directly.

### `ChangeSummary` (report → API / CLI output)
```python
class ChangeSummary(BaseModel):
    critical: int
    significant: int         # maps from MAJOR
    minor: int
    uncertain: int           # includes UNCERTAIN*
    total_views_compared: int
```
Note: the internal severity string is `MAJOR` but the public API/report uses `significant`. The mapping happens in `report/generator.py:count_summary()`.

### `JobStatus` (API response)
```python
class JobStatus(BaseModel):
    job_id: str
    status: "queued" | "processing" | "complete" | "failed"
    report_url: str | None       # presigned S3 URL
    changeset_url: str | None    # presigned S3 URL
    summary: ChangeSummary | None
    error: str | None
    stage: str | None            # e.g. "comparing"
    progress_pct: int | None     # 0–100
```

---

## Configuration (`config.py`)

All settings in one frozen dataclass. Never mutate; always read from `config` singleton.

| Field | Default | Notes |
|---|---|---|
| `base_model` | `claude-sonnet-4-6` | First model used for vision comparisons |
| `escalation_model` | `claude-opus-4-6` | Used when any change confidence < threshold |
| `confidence_threshold` | `0.65` | Below this → UNCERTAIN (after escalation); also triggers escalation |
| `view_match_threshold` | `0.80` | rapidfuzz ratio floor (as 0–1 float; multiplied by 100 before comparing) |
| `render_dpi` | `300` | PDF render resolution; changing affects all pixel-space math |
| `max_image_dimension` | `4096` | Downscale guard before LLM calls |
| `max_tokens_per_view` | `2000` | Per-call token budget |
| `max_file_size_mb` | `50` | API and CLI upload limit |
| `artifact_ttl_hours` | `24` | Redis job TTL + presigned URL expiry |
| `use_view_isolator` | `True` | When True, `local_runner` calls `view_isolator.isolate_match_crops` to white-out foreign content from each crop. When False, `bbox_snap.snap_view_bboxes_to_text` runs instead and moves bbox edges. |
| `blueprint_model` | `claude-sonnet-4-6` | Model used by `pipeline.template_blueprint.extract_blueprint` when a template PDF is supplied. |

S3 key format: `{kind}s/{job_id}.{ext}` — e.g. `reports/abc123.pdf`, `changesets/abc123.json`, `templates/abc123.pdf` (optional, only when a template was uploaded).

---

## LLM prompt (`prompts/compare_view.txt`)

Loaded once at module import into `_SYSTEM_PROMPT`. Sent with `cache_control: ephemeral` so Anthropic's prompt cache deduplicates it across the ~N view comparisons per job.

The prompt enforces **JSON-only output** with this schema:
```json
{
  "changes": [
    {
      "field": "dimension | tolerance | note | feature | geometry | annotation | callout | material | finish | hole | thread | other",
      "orig_value": "string or null",
      "revised_value": "string or null",
      "severity": "CRITICAL | MAJOR | MINOR",
      "confidence": 0.0,
      "rationale": "one sentence"
    }
  ]
}
```

The parser in `comparator._parse_llm_json` strips accidental markdown fences (```` ```json ``` ````). Note: the prompt uses `CRITICAL/MAJOR/MINOR`; `UNCERTAIN` is never emitted by the LLM — it is assigned by the post-processing logic in the orchestrator.

---

## Title block diff (no LLM)

For `title_block` matches, `_diff_title_block` does a deterministic field-by-field diff. Fixed severity mapping:

| Field | Severity |
|---|---|
| `part_number` | CRITICAL |
| `material` | CRITICAL |
| `tolerance` | CRITICAL |
| `revision` | MAJOR |
| `drawn_by` | MINOR |
| `date` | MINOR |

All title block changes get `confidence=1.0`. `raw_text` is deliberately excluded — it's the noisy OCR concatenation that the structured fields already distil.

---

## Local runner monkeypatch

`local_runner._install_local_crop_loader(job_dir)` replaces `comparator._download_png` with a closure that reads from `job_dir / key`. The original is restored in a `finally` block after the run. This is the **only** coupling between the local runner and the comparator's S3 logic. Do not add more S3 calls to the comparator without updating this patch.

---

## File layout under a local job directory

```
out/<job-id>/
├── originals/<job-id>.pdf          # input PDF (canonical S3 key shape)
├── reviseds/<job-id>.pdf
├── pages/<job-id>/original_p00.png # full-page renders
├── pages/<job-id>/revised_p00.png
├── crops/<job-id>/original/view_00.png  # per-view crops
├── crops/<job-id>/revised/view_00.png
├── metadata/<job-id>/extraction.json
├── metadata/<job-id>/matches.json
├── metadata/<job-id>/changeset.json
├── highlighted/original_p00.png
├── highlighted/revised_p00.png
├── report.pdf
├── changeset.json                  # canonical output (copy of metadata/.../changeset.json)
└── error.log                       # only on failure without --verbose
```

---

## API endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Returns `{"status": "ok"}` |
| `POST` | `/compare` | Bearer (optional) | Multipart: `original` (file), `revised` (file), `template` (file, optional PDF — drives blueprint-style report), `part_number` (str, optional), `notes` (str, optional). Accepted types for original/revised: `application/pdf`, `image/tiff`, `image/png`. Returns `202` with `job_id`. |
| `GET` | `/compare/{job_id}` | Bearer (optional) | Returns `JobStatus`. Presigned URLs only present when `status == "complete"`. |

Auth is a no-op when `API_KEY` env var is empty (default for local dev).

---

## Test suite (`tests/`)

```bash
pytest
```

| File | What it tests |
|---|---|
| `test_parser_segmentor.py` | Parse + segment on fixture PDFs; title block field extraction |
| `test_matcher.py` | View matching logic, greedy assignment, added/removed handling |
| `test_comparator.py` | Title/revision block diffs, `_parse_llm_json` tolerance |
| `test_aggregator.py` | Dedup, sort, UNCERTAIN* promotion |
| `test_report.py` | Report generation, `count_summary` mapping |

Fixture files live in `tests/fixtures/`. The comparator tests mock the Anthropic client — do not make real API calls in tests.

---

## Gotchas and sharp edges

- **`render_dpi / 72.0`** is the pt→px scale factor used everywhere. If you add pixel-space math, derive scale from this — don't hardcode it.
- **Vector fallback**: if `_find_borders_vector` returns an empty list, `segment_views` falls through to the raster OpenCV path on the same rendered image. This is intentional and silent (`log.info` only).
- **Aspect ratio filter in vector segmentor**: `0.2 < aspect < 5.0` — very tall or very wide thin rectangles (leader lines, arrows) are deliberately excluded. Adjust if drawings have extreme-format views.
- **Aggregator works on dicts**, not `ViewDiff` dataclasses. Always serialise with `viewdiff_to_dict()` before passing to `aggregate()`.
- **MAJOR vs significant**: internally the severity string is `MAJOR`; the public `ChangeSummary` field is `significant`. The mapping lives in `report/generator.py:count_summary()`. If you add new severity levels, update that function.
- **UNCERTAIN\* is not a hard reclassification** — it adds an asterisk and a rationale suffix. Do not treat it as a confirmed change in any downstream logic.
- **pydyf pinned to 0.10.0** in `requirements.txt` — WeasyPrint breaks on newer versions. Do not upgrade without testing report generation.
- **macOS fork safety**: RQ worker must be started with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`. Without it the worker silently kills horse processes.
- **Textract is not wired in**: `requirements.txt` includes `boto3` and the PRD mentions AWS Textract for raster OCR, but `parser.py` only sets the `is_raster` flag — there is no OCR pipeline yet. The raster segmentation path (OpenCV) works on pixel geometry only, not text.
- **`part_number` and `notes`** are report metadata only. They do not influence LLM calls or pipeline logic.
- **Aggregator preserves rationale**: as of the `boilerplate` branch, the aggregator no longer mutates the visible `rationale` string. Dedup and UNCERTAIN* promotion both write to side-channel fields (`dedup_note`, `uncertain_note`) on the change dict. The default template ignores them; the blueprint template surfaces them as small footnotes below the rationale when present.
- **One-sentence rationales**: `prompts/compare_view.txt` requires ≤20 words / one sentence. `pipeline.comparator._normalise_rationale` truncates at the first sentence boundary as a defensive guard. Title/revision block diffs already emit single-sentence rationales deterministically.
- **Template feature**: optional. Pass `--template path/to/eco.pdf` to the CLI or include a `template` upload field on `POST /compare`. The pipeline calls `pipeline.template_blueprint.extract_blueprint` once at the end (just before `generate_report`). If extraction fails for any reason — invalid PDF, bad JSON, schema mismatch, missing `[[CHANGES]]` token, model error — the function returns `None` and the report falls back to `report.html.j2`. Blueprint extraction is best-effort and never aborts the job.
- **Strict template fidelity**: the LLM returns a full `html_shell` document (its own HTML and CSS, mimicking the customer template). The renderer in `_render_blueprint_report` only substitutes the dozen `[[TOKEN]]` placeholders — it never reorders sections, restyles cells, or imposes DrawDiff CSS on top of the template. Add new tokens (and corresponding fragment generators) carefully; the prompt and the renderer must agree on the exact token set.
- **Zone is mandatory in change rows**: the default Jinja template already has a dedicated `Zone` column. The blueprint path enforces the same invariant differently: if the customer template has a column with `role: "zone"` we render the zone there; otherwise `_render_changes_fragment` prepends `<strong>[Zone X]</strong>` to the description cell (when a real zone is known). Never drop the zone from the rendered output.
- **Overlap-first segmentation (`lseg` branch)**: the LLM prompt explicitly asks for *generous, possibly overlapping* view bboxes — no longer a partition of the page. Foreign content is removed at crop time by `view_isolator` (white-out by ownership), not by moving bbox edges. The bbox stored in `view_metadata.bbox` is the LLM's original bbox; only the on-disk crop image bytes have foreign content erased. Downstream code that works in PDF-point space (highlighter, change_locator) sees unchanged geometry.
- **Ownership granularity**: `view_isolator` uses text tokens + (vector PDFs only) PyMuPDF `get_drawings()` primitives, filtered to ≥0.01% of page area to avoid noise. Greatest-intersection-area assignment, ties broken by closer center — same `_assign_ownership` helper as `bbox_snap`.
