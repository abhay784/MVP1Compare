# DrawDiff Startup Instructions

This document contains all commands needed to start the DrawDiff pipeline, API, and worker.

## Prerequisites

- Python 3.13 with all dependencies installed (`pip install -r requirements.txt`)
- Redis running locally (default: `redis://localhost:6379/0`)
- AWS credentials configured (S3 bucket access for artifact storage)
- `.env` file with API keys and AWS credentials (see `.env.example`)

## Quick Start (3 terminals)

### Terminal 1: Redis (if not already running)

```bash
redis-server
```

Or if installed via Homebrew:
```bash
brew services start redis
```

### Terminal 2: API Server (FastAPI + Uvicorn)

```bash
cd /Users/abhaykorlapati/MVP1Compare
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 -m uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

The API will be available at `http://localhost:8000`. Health check: `curl http://localhost:8000/health`

### Terminal 3: RQ Worker (processes comparison jobs)

```bash
cd /Users/abhaykorlapati/MVP1Compare
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES /Library/Frameworks/Python.framework/Versions/3.13/bin/rq worker
```

**Important:** The `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` env var is required on macOS to prevent fork crashes when the worker spawns child processes.

The worker will print:
```
18:25:08 Worker rq:worker:... started with PID ..., version 1.16.2
18:25:08 Listening on default...
```

## Submitting a Comparison

Once all three services are running, submit a job via the API:

```bash
curl -X POST http://localhost:8000/compare \
  -F "original=@path/to/original.pdf" \
  -F "revised=@path/to/revised.pdf" \
  -F "part_number=123-456" \
  -F "notes=Engineering change notice"
```

Response:
```json
{
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "status_url": "/compare/550e8400-e29b-41d4-a716-446655440000",
  "estimated_seconds": 120
}
```

## Polling Job Status

```bash
JOB_ID="550e8400-e29b-41d4-a716-446655440000"
curl http://localhost:8000/compare/$JOB_ID
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
  "report_url": "https://s3.us-west-1.amazonaws.com/drawdiff/reports/...",
  "changeset_url": "https://s3.us-west-1.amazonaws.com/drawdiff/changesets/...",
  "highlighted_original_url": "https://s3.us-west-1.amazonaws.com/drawdiff/highlighted/.../original.png",
  "highlighted_revised_url": "https://s3.us-west-1.amazonaws.com/drawdiff/highlighted/.../revised.png",
  "summary": {
    "critical": 2,
    "significant": 5,
    "minor": 1,
    "uncertain": 0,
    "total_views_compared": 12
  }
}
```

## Output Artifacts

All artifacts are stored in S3 and accessible via presigned URLs (valid for 24 hours by default):

- **`report_url`** — PDF report with side-by-side view crops and per-change details
- **`changeset_url`** — JSON with all detected changes (severity, field, confidence, rationale)
- **`highlighted_original_url`** — Full-page original drawing with coloured boxes marking changed views
- **`highlighted_revised_url`** — Full-page revised drawing with coloured boxes marking changed views

**Colour legend on highlighted pages:**
- 🟥 **Red** — Critical (structural, safety, compliance impact)
- 🟧 **Orange** — Significant (manufacturing scope/cost/lead time impact)
- 🟨 **Gold** — Minor (documentation/clarity)
- ⬜ **Grey** — Uncertain (low confidence; requires human review)

## Stopping Services

### Stop Worker
Press `Ctrl+C` in Terminal 3.

### Stop API
Press `Ctrl+C` in Terminal 2.

### Stop Redis
```bash
redis-cli shutdown
```

Or if using Homebrew:
```bash
brew services stop redis
```

## Troubleshooting

### Worker prints "Killed horse pid XXXXX"
This is the fork safety crash on macOS. Make sure you're using:
```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES rq worker
```

### Job stays in "processing" / "queued"
Check that the worker is actually running and listening on the default queue:
```bash
redis-cli
> KEYS job:*
> HGETALL job:YOUR_JOB_ID
```

If the job key exists but the worker isn't processing, restart the worker.

### "S3_BUCKET" empty string error
Make sure `.env` is loaded before config.py evaluates. The code calls `load_dotenv()` at module import time, so if `.env` has `S3_BUCKET=drawdiff`, it should work. If not, set it explicitly:
```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-west-1
export S3_BUCKET=drawdiff
export ANTHROPIC_API_KEY=...
```

### PDF report generation fails ("transform" error)
This is a weasyprint/pydyf version mismatch. The fix is already pinned in `requirements.txt`:
```bash
pip install pydyf==0.10.0
```

### Vision API errors / timeout
Check `ANTHROPIC_API_KEY` in `.env`. If correct, the job will fail and the error will be logged. Check the worker terminal and the job status at:
```bash
redis-cli HGETALL job:YOUR_JOB_ID
```

## Configuration

See `config.py` for tunable parameters:
- `render_dpi` — resolution at which PDFs are rendered (default: 300)
- `confidence_threshold` — changes below this LLM confidence are marked UNCERTAIN (default: 0.65)
- `max_file_size_mb` — upload size limit (default: 50 MB)
- `artifact_ttl_hours` — presigned URL lifetime and Redis expiry (default: 24 hours)

## Next Steps

1. Submit a test comparison job.
2. Monitor the worker terminal to see the pipeline stages.
3. Once complete, download the highlighted PNGs and PDF report from the presigned URLs.
4. Verify that view boxes are drawn in the correct severity colours and dimension circles appear where expected.
python3 cli.py compare "DA1840189_NC (1).pdf" "DA1840189_A (1).pdf" --part-number P-001 --out-dir ./out
