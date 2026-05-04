# DrawDiff

AI-powered diff for mechanical engineering drawings. Give it two PDF revisions of the same drawing and it identifies every engineering-meaningful change, classifies each by severity, and produces an annotated PDF report.

## How it works

1. **Parse** — renders each PDF page at 300 DPI, extracts text layer
2. **Segment** — splits pages into logical views (front, section, detail, title block, etc.)
3. **Extract** — pulls dimensions, tolerances, and title/revision block fields via regex
4. **Match** — pairs corresponding views across revisions using fuzzy label matching
5. **Compare** — sends each matched view pair as images to Claude (vision LLM) and gets back a structured list of changes with severity and confidence
6. **Aggregate** — deduplicates changes within and across views, sorts by severity, soft-promotes uncertain changes when corroborated elsewhere
7. **Annotate + Highlight** — draws side-by-side view strips and severity-coloured overlays on full pages
8. **Report** — generates a PDF report via Jinja2 + WeasyPrint

---

## Prerequisites

- Python 3.13
- `pip install -r requirements.txt`
- An `ANTHROPIC_API_KEY`
- Redis (for the full API stack only — not needed for CLI)
- AWS credentials + S3 bucket (for the full API stack only — not needed for CLI)

Copy `.env.example` to `.env` and fill in your secrets:

```bash
cp .env.example .env
```

```
ANTHROPIC_API_KEY=sk-ant-...
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
S3_BUCKET=your-bucket-name
REDIS_URL=redis://localhost:6379/0
API_KEY=                        # leave empty to disable auth in local dev
```

---

## CLI (no Redis / S3 required)

The fastest way to run a comparison locally. All artifacts are written to disk under `./out/<job-id>/`.

```bash
python cli.py compare original.pdf revised.pdf
```

With optional metadata and flags:

```bash
python cli.py compare original.pdf revised.pdf \
    --part-number 123-456 \
    --notes "ECN #789 — Rev B release for manufacturing" \
    --out-dir ./out \
    --verbose
```

### CLI options

| Option | Default | Description |
|---|---|---|
| `--part-number` | `UNKNOWN` | Label shown in the report header and PDF title |
| `--notes` | _(empty)_ | Free-text callout block rendered near the top of the report |
| `--out-dir` | `./out` | Directory under which `<job-id>/` artifacts are written |
| `--job-id` | _(random UUID)_ | Override the generated job ID (useful for re-running a specific job) |
| `--force` | off | Overwrite an existing non-empty job directory |
| `--verbose` | off | Enable DEBUG-level logging; print full tracebacks to stderr instead of writing to `error.log` |

### CLI output

```
✓ Comparison complete in 42.3s
  Report:    out/abc123/report.pdf
  Changeset: out/abc123/changeset.json
  Highlighted (original):
    out/abc123/highlighted_original_page0.png
  Highlighted (revised):
    out/abc123/highlighted_revised_page0.png
  Summary: critical=2 significant=5 minor=1 uncertain=0 views=12
```

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Pipeline error (LLM failure, parser crash, etc.) — check `error.log` or use `--verbose` |
| `2` | Bad arguments / missing input file / file too large / missing API key |

---

## Full stack (API + worker)

Use this when running the web UI or integrating with the REST API. Requires Redis and AWS S3.

### Terminal 1 — Redis

```bash
brew services start redis
# or
redis-server
```

### Terminal 2 — API server

```bash
python -m uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Health check: `curl http://localhost:8000/health`

### Terminal 3 — RQ worker

```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker
```

> **macOS note:** The `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` env var is required on macOS to prevent fork crashes.

### Submit a job

```bash
curl -X POST http://localhost:8000/compare \
  -F "original=@original.pdf" \
  -F "revised=@revised.pdf" \
  -F "part_number=123-456" \
  -F "notes=ECN #789"
```

Response (`202 Accepted`):

```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status_url": "/compare/550e8400-e29b-41d4-a716-446655440000",
  "estimated_seconds": 120
}
```

### Poll for status

```bash
curl http://localhost:8000/compare/550e8400-e29b-41d4-a716-446655440000
```

While processing:

```json
{
  "job_id": "550e8400-...",
  "status": "processing",
  "stage": "comparing",
  "progress_pct": 92
}
```

When complete:

```json
{
  "job_id": "550e8400-...",
  "status": "complete",
  "report_url": "https://s3.amazonaws.com/your-bucket/reports/....pdf",
  "changeset_url": "https://s3.amazonaws.com/your-bucket/changesets/....json",
  "summary": {
    "critical": 2,
    "significant": 5,
    "minor": 1,
    "uncertain": 0,
    "total_views_compared": 12
  }
}
```

Accepted file types: **PDF, TIFF, PNG** (max 50 MB each). If `API_KEY` is set in `.env`, include `Authorization: Bearer <key>` on all requests.

---

## Web UI

```bash
cd ui
npm install
npm run dev
```

The Vite dev server runs on `http://localhost:5173` and proxies `/compare` and `/health` to the API on port 8000. See [`ui/README.md`](ui/README.md) for more detail.

---

## Output artifacts

| Artifact | Description |
|---|---|
| `report.pdf` | Full report: severity summary, side-by-side view strips, per-change table, highlighted full pages |
| `changeset.json` | Raw change data — field, old/new value, severity, confidence, rationale for every change |
| `highlighted_original_page*.png` | Original drawing pages with severity-coloured boxes over changed views |
| `highlighted_revised_page*.png` | Revised drawing pages with severity-coloured boxes over changed views |

### Severity colour legend

| Colour | Severity | Meaning |
|---|---|---|
| Red | **Critical** | Load-bearing dimensions, fit/function tolerances, material, thread specs, removed features |
| Orange | **Significant** | Dimensional changes that don't cross a function boundary, new features, changed hole sizes |
| Gold | **Minor** | Cosmetic/documentation edits — view labels, repositioned callouts, drafting cleanup |
| Grey | **Uncertain** | LLM confidence below threshold (0.65); requires human review |

`UNCERTAIN*` (asterisk) means the change was uncertain in one view but corroborated by a high-confidence Critical/Significant finding in another view — still flagged for review but more likely real.

---

## Configuration

All tunables live in `config.py` and are overridable via environment variables.

| Parameter | Default | Description |
|---|---|---|
| `render_dpi` | `300` | DPI at which PDF pages are rendered to images |
| `confidence_threshold` | `0.65` | LLM confidence below this → severity rewritten to UNCERTAIN |
| `view_match_threshold` | `0.80` | Minimum fuzzy-match score to pair views across revisions |
| `max_image_dimension` | `4096` | Downscale guard before sending images to the LLM |
| `max_tokens_per_view` | `2000` | Token budget per LLM call |
| `max_file_size_mb` | `50` | Upload size limit |
| `artifact_ttl_hours` | `24` | Presigned S3 URL lifetime and Redis job expiry |
| `base_model` | `claude-sonnet-4-6` | Default Claude model for comparisons |
| `escalation_model` | `claude-opus-4-6` | Model used when escalation is triggered |

---

## Project structure

```
├── api/                    # FastAPI application
│   ├── main.py             #   Routes: POST /compare, GET /compare/{id}, GET /health
│   ├── jobs.py             #   Redis job state management + S3 staging
│   └── models.py           #   Pydantic models (JobStatus, ChangeSummary, etc.)
├── pipeline/               # Core processing engine
│   ├── run.py              #   Cloud orchestrator (downloads from S3, uploads artifacts)
│   ├── local_runner.py     #   Local orchestrator (filesystem paths, no S3)
│   ├── parser.py           #   PDF → page images + text blocks (PyMuPDF)
│   ├── segmentor.py        #   Page → view crops (vector: rect detection; raster: OpenCV)
│   ├── extractor.py        #   Dimensions, title block, revision block extraction
│   ├── matcher.py          #   Fuzzy view matching across revisions (rapidfuzz)
│   ├── comparator.py       #   LLM vision calls per view pair (Anthropic Claude)
│   ├── aggregator.py       #   Dedup, sort, and soft-promote changes
│   ├── annotator.py        #   Side-by-side view strip PNGs (Pillow)
│   └── highlighter.py      #   Full-page severity overlay PNGs (Pillow)
├── prompts/
│   └── compare_view.txt    # System prompt for the comparator LLM call
├── report/
│   ├── generator.py        # PDF generation (Jinja2 + WeasyPrint)
│   └── templates/
│       └── report.html.j2  # Report HTML template
├── ui/                     # React + Tailwind SPA (see ui/README.md)
├── tests/                  # pytest test suite
├── cli.py                  # Local CLI entrypoint
├── config.py               # Central config + env resolution
├── .env.example            # Environment variable template
└── requirements.txt        # Python dependencies
```

---

## Tests

```bash
pytest
```

---

## Troubleshooting

**Worker prints "Killed horse pid XXXXX"**
Fork safety crash on macOS. Use `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker`.

**Job stays in "queued" forever**
Check the worker is running and listening on the default queue:
```bash
redis-cli KEYS "job:*"
redis-cli HGETALL job:<your-job-id>
```

**`S3_BUCKET` empty string error**
Set it in `.env` or export it directly before starting the server/worker.

**PDF report generation fails ("transform" error)**
WeasyPrint/pydyf version mismatch. Already pinned in `requirements.txt`:
```bash
pip install pydyf==0.10.0
```

**Vision API errors / timeout**
Check `ANTHROPIC_API_KEY` in `.env`. Error details are stored in Redis and returned by `GET /compare/<job-id>` as `"status": "failed"` with an `"error"` field.
