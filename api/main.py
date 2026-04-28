"""DrawDiff FastAPI application — Step 2 (API layer)."""
from __future__ import annotations

import redis as redis_lib
from rq import Queue
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile

from config import config
from api.jobs import create_job, new_job_id, get_job_status, stage_upload
from api.models import CompareAccepted, JobStatus

app = FastAPI(title="DrawDiff", version="0.1.0")

_MAX_BYTES = config.max_file_size_mb * 1024 * 1024
_ALLOWED_TYPES = {"application/pdf", "image/tiff", "image/png"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_auth(authorization: str | None) -> None:
    """Validate Bearer token. No-op when API_KEY env var is unset (local dev)."""
    if not config.api_key:
        return
    if authorization != f"Bearer {config.api_key}":
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/compare", status_code=202, response_model=CompareAccepted)
async def post_compare(
    original: UploadFile = File(..., description="Original revision PDF"),
    revised: UploadFile = File(..., description="Revised revision PDF"),
    part_number: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)

    for upload in (original, revised):
        if upload.content_type not in _ALLOWED_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported file type '{upload.content_type}'. Accepted: PDF, TIFF, PNG.",
            )

    original_bytes = await original.read()
    revised_bytes = await revised.read()

    for label, data in (("original", original_bytes), ("revised", revised_bytes)):
        if len(data) > _MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"'{label}' exceeds the {config.max_file_size_mb} MB limit.",
            )

    job_id = new_job_id()
    create_job(job_id)
    stage_upload(job_id, "original", original_bytes)
    stage_upload(job_id, "revised", revised_bytes)

    rq = Queue(connection=redis_lib.from_url(config.redis_url))
    rq.enqueue(
        "pipeline.run.run_comparison",  # implemented in Step 6
        job_id,
        part_number or "",
        notes or "",
        job_id=job_id,
    )

    return CompareAccepted(
        job_id=job_id,
        status_url=f"/compare/{job_id}",
        estimated_seconds=120,
    )


@app.get("/compare/{job_id}", response_model=JobStatus)
def get_compare_status(
    job_id: str,
    authorization: str | None = Header(default=None),
):
    _check_auth(authorization)
    job = get_job_status(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
